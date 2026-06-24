"""
wiki-agent / scripts / run_update_cycle.py

피드백 파이프라인 1사이클 오케스트레이션:
ingest -> mine_gaps -> (각 gap) curate -> gate -> shadow 반영 -> reindex(no-op)
-> promote_if_better.

core/pipeline/*는 core/ 또는 eval/을 직접 import하지 않는 순수 단계들이고,
이 스크립트가 둘을 묶는다(eval.run_eval.evaluate를 promote의 evaluate_fn으로 주입).

실행: python scripts/run_update_cycle.py
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store
from core.pipeline import curate, gate, ingest, mine, promote, reindex


def _daily_cap_from_feedback(down_rate: float, base_cap: int = 20) -> int:
    """feedback 👎 비율이 높을수록 더 보수적으로(상한을 낮춰서) 승격을 제한."""
    if down_rate > 0.5:
        return max(1, base_cap // 4)
    if down_rate > 0.2:
        return max(1, base_cap // 2)
    return base_cap


def summary_notifications(summary: Dict[str, Any]) -> list:
    """run_cycle()의 결과 dict -> [(level, title, message), ...]. 로그 텍스트를
    나중에 파싱하지 않고 이미 구조화된 summary에서 직접 판정한다 — LLM 호출 없이
    단위 테스트 가능. 사이클마다 항상 요약 1건(info) + 조건에 따라 경고 0~2건."""
    promote = summary["promote"]
    feedback = summary["feedback"]
    mined, shadow_written = summary["mined"], summary["shadow_written"]

    notes = [(
        "info",
        "갱신 사이클 완료",
        f"gap {mined}개 발견, shadow {len(shadow_written)}개 반영, "
        f"{'승격됨' if promote['promoted'] else '승격 안 됨'}"
        + (f" ({len(promote['activated_entry_ids'])}개 active)" if promote["promoted"] else ""),
    )]

    if feedback["n"] >= 5 and feedback["down_rate"] > 0.5:
        notes.append((
            "warning", "피드백 부정 비율이 높음",
            f"최근 피드백 {feedback['n']}건 중 {feedback['down_rate']:.0%}가 👎입니다 — "
            "daily_cap이 보수적으로 낮춰졌습니다.",
        ))

    # shadow로 새로 쓴 게 있는데 승격이 안 됐다 = 회귀가 감지돼 막혔다는 뜻
    # (예: escalation_correctness가 떨어지는 걸 promote.py가 잡아낸 실제 사례).
    if shadow_written and not promote["promoted"]:
        notes.append((
            "warning", "회귀로 승격 차단됨",
            f"새로 만든 shadow 항목 {len(shadow_written)}개가 골드셋 회귀(recall@k/"
            "correctness 하락) 때문에 active로 승격되지 못했습니다. shadow 상태로만 남음.",
        ))

    # --check-agentic-regression(옵트인)일 때만 summary에 이 키가 채워짐. 승격을
    # 막지 않는 진단 전용 경고 — agentic_gold_set이 6개뿐이라 참고용으로만 본다.
    if summary.get("agentic_regressed"):
        notes.append((
            "warning", "에이전틱(멀티홉) 평가 회귀 감지",
            "이전 사이클보다 task_success_rate 또는 multihop_recall이 낮아졌습니다 — "
            "승격은 막지 않았습니다(진단 전용, 골드셋이 작아 참고용).",
        ))

    return notes


def run_cycle(
    *,
    gold_path: Optional[str] = None,
    k: int = 5,
    min_freq: int = 3,
    score_threshold: float = 0.0,
    window_days: Optional[float] = 14,
    llm_fn: Optional[Callable] = None,
    judge_fn: Optional[Callable] = None,
    evaluate_fn: Optional[Callable] = None,
    use_web_search: bool = False,
    web_search_daily_cap: int = 5,
    web_search_llm_fn: Optional[Callable] = None,
    cluster_paraphrases: bool = False,
    cluster_embed_fn: Optional[Callable] = None,
    cluster_similarity_threshold: float = 0.85,
    check_agentic_regression: bool = False,
    agentic_eval_fn: Optional[Callable] = None,
    agentic_gold_path: Optional[str] = None,
) -> Dict[str, Any]:
    from eval.run_eval import GOLD_PATH, evaluate, load_gold

    evaluate_fn = evaluate_fn or evaluate

    # window_days=None(또는 0)이면 전체 히스토리를 본다. 기본 14일 — retrieval_log/
    # feedback에 retention이 없어 테이블이 무한히 쌓이는데, 윈도잉이 없으면 매
    # 사이클이 전체 히스토리를 다시 스캔하고 한 번 오염된 쿼리가 영원히 gap으로
    # 재탐지된다(README "위키 자가 갱신 확인하기" 참고).
    mining_since_ts = time.time() - window_days * 86400 if window_days else None

    feedback_agg = ingest.ingest_feedback(wiki_store.list_feedback(since_ts=mining_since_ts))
    ingested_retrieval = ingest.ingest_retrieval_log(
        wiki_store.list_retrieval_log(since_ts=mining_since_ts))
    # 옵트인(기본 off) — 같은 의미를 다르게 표현한 질문이 mine.py의 정확매칭
    # 그룹핑 때문에 각각 min_freq 미달로 영원히 gap을 못 넘는 문제를 완화한다.
    # mine.py는 무수정(여전히 exact-match), 여기서 미리 norm_query를 통일해 넘긴다.
    if cluster_paraphrases:
        ingested_retrieval = ingest.cluster_paraphrased_queries(
            ingested_retrieval, embed_fn=cluster_embed_fn,
            similarity_threshold=cluster_similarity_threshold)
    gaps = mine.mine_gaps(ingested_retrieval, min_freq=min_freq, score_threshold=score_threshold)

    daily_cap = _daily_cap_from_feedback(feedback_agg["down_rate"])
    existing_entries = wiki_store.list_active_entries()
    # 이전 사이클들이 쌓아둔 shadow 후보 — gate의 자카드 중복 체크가 active뿐 아니라
    # 기존 shadow와도 비교해야 같은 gap이 사이클마다 비슷한 shadow를 계속 쌓는 걸
    # 막을 수 있다. 아래 루프에서 새로 쓸 때마다 이 리스트에 더해 같은 사이클의
    # 후속 gap들도 보게 한다.
    pending_shadow_entries = wiki_store.list_shadow_entries()
    since_ts = time.time() - 86400

    # norm_query 단위로 "이전에 게이트가 거부한 gap"을 기억해 다음 사이클에 같은
    # 질문이 다시 잡혀도 curate/judge LLM 호출을 반복하지 않는다(dedupe.py의
    # skip_rejected와 동일 목적, 여긴 norm_query로 콘텐츠 불변을 판단). 답이 될
    # 콘텐츠가 나중에 생기면 검색 점수가 양수로 돌아서 mine_gaps가 더는 이
    # 질문을 gap으로 뽑지 않으므로 별도 만료 처리가 필요 없다.
    rejected_gap_ids = {e["entry_id"] for e in wiki_store.list_rejected_entries()}

    summary: Dict[str, Any] = {
        "mined": len(gaps),
        "feedback": feedback_agg,
        "daily_cap": daily_cap,
        "shadow_written": [],
        "rejected": [],
        "skipped_rejected_gaps": [],
        "web_curated": [],
        "promote": None,
    }

    for gap in gaps:
        rej_id = curate.rejected_gap_entry_id(gap["norm_query"])
        if rej_id in rejected_gap_ids:
            summary["skipped_rejected_gaps"].append(gap["norm_query"])
            continue

        # 웹 검색은 비용이 더 크므로 사이클당 별도 상한(web_search_daily_cap)을
        # 두고, 근거를 못 찾거나 호출이 실패하면 기존 로그 전용 경로로 조용히
        # 폴백한다 — README "외부 검색을 하지 않는다" 한계의 opt-in 확장.
        patch = None
        if use_web_search and len(summary["web_curated"]) < web_search_daily_cap:
            try:
                patch = curate.curate_from_web(gap, llm_fn=web_search_llm_fn)
                summary["web_curated"].append(patch["entry_id"])
            except Exception:
                patch = None

        if patch is None:
            try:
                patch = curate.curate(gap, llm_fn=llm_fn)
            except Exception as e:
                summary["rejected"].append({"gap": gap["norm_query"], "reason": f"curate failed: {e}"})
                continue

        today_writes = wiki_store.count_entries("shadow", since_ts=since_ts)
        ok, reason = gate.passes_gate(
            patch, today_writes,
            existing_entries=existing_entries,
            pending_shadow_entries=pending_shadow_entries,
            daily_cap=daily_cap,
            judge_fn=judge_fn,
        )
        if not ok:
            summary["rejected"].append({"entry_id": patch["entry_id"], "reason": reason})
            wiki_store.add_entry(
                rej_id, patch["topic"], patch["canonical"], patch["body_md"],
                status="rejected", provenance=patch["provenance"], confidence=0.0,
                sources=patch["sources"], tier=patch.get("tier"),
            )
            rejected_gap_ids.add(rej_id)
            continue

        wiki_store.add_entry(
            patch["entry_id"], patch["topic"], patch["canonical"], patch["body_md"],
            status="shadow", provenance=patch["provenance"],
            confidence=patch["confidence"], sources=patch["sources"],
            tier=patch.get("tier"),
        )
        summary["shadow_written"].append(patch["entry_id"])
        pending_shadow_entries.append(patch)
        reindex.reindex_changed([patch["entry_id"]])

    gold = load_gold(gold_path or GOLD_PATH)
    summary["promote"] = promote.promote_if_better(gold, k=k, evaluate_fn=evaluate_fn)

    # 옵트인(기본 off) — 멀티홉 진단(eval/agentic_eval.py)을 알림 전용으로 돌린다.
    # 그 파일의 HARD CONSTRAINT(승격 게이트 미연결)를 지켜 promote 결과에는
    # 영향을 주지 않는다 — 골드셋이 6개뿐이라 false positive 위험이 크다.
    agentic_result = None
    if check_agentic_regression:
        from eval.agentic_eval import GOLD_PATH as AGENTIC_GOLD_PATH
        from eval.agentic_eval import load_agentic_gold, run_agentic_eval

        # 영속 임베딩 캐시 재사용 — 안 주면 멀티홉 루프의 검색마다 활성 엔트리
        # 전체를 재인코딩하게 된다.
        agentic_cache = wiki_store.PersistentEmbeddingCache()
        agentic_fn = agentic_eval_fn or run_agentic_eval
        agentic_tasks = load_agentic_gold(agentic_gold_path or AGENTIC_GOLD_PATH)
        agentic_result = agentic_fn(
            agentic_tasks, k=k,
            search_fn=lambda query, k=5: wiki_store.search_wiki(query, k, cache=agentic_cache),
        )
        summary["agentic"] = agentic_result

        previous_agentic = next(
            (row for row in reversed(wiki_store.list_cycle_history())
             if row.get("agentic_task_success_rate") is not None), None)
        summary["agentic_regressed"] = bool(previous_agentic) and (
            agentic_result["task_success_rate"] < previous_agentic["agentic_task_success_rate"]
            or agentic_result["multihop_recall"] < previous_agentic["agentic_multihop_recall"]
        )

    # cycle_history에 "지금 active 상태"의 골드셋 지표 1행을 남긴다 — 승격됐으면
    # candidate, 안 됐으면 base가 곧 지금의 실제 품질이다. 여러 사이클의 추이를
    # 보려면 이 구조화된 값들이 쌓여야 한다(notifications는 텍스트 요약일 뿐).
    promote_result = summary["promote"]
    metrics_src = (
        promote_result["candidate"] if promote_result["promoted"] else promote_result["base"]
    )
    wiki_store.add_cycle_history(
        mined=summary["mined"],
        shadow_count=len(summary["shadow_written"]),
        promoted=promote_result["promoted"],
        activated_count=len(promote_result["activated_entry_ids"]),
        recall_at_k=metrics_src["recall@k"],
        mrr=metrics_src["mrr"],
        correctness=metrics_src["correctness"],
        escalation_correctness=metrics_src.get("escalation_correctness"),
        agentic_task_success_rate=agentic_result["task_success_rate"] if agentic_result else None,
        agentic_multihop_recall=agentic_result["multihop_recall"] if agentic_result else None,
        agentic_avg_tool_calls=agentic_result["avg_tool_calls"] if agentic_result else None,
    )

    # 구조화된 summary에서 바로 알림을 만든다 — 데모의 종모양 알림 UI
    # (GET /notifications)가 읽음.
    notifications = summary_notifications(summary)
    for level, title, message in notifications:
        wiki_store.add_notification(level, title, message)
    summary["notifications"] = notifications

    return summary


def main():
    parser = argparse.ArgumentParser(description="피드백 파이프라인 1사이클 실행")
    parser.add_argument("--gold", default=None)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--window-days", type=float, default=14,
        help="mine_gaps가 보는 retrieval_log/feedback 윈도우(일). 0이면 전체 히스토리(과거 동작)")
    parser.add_argument(
        "--use-web-search", action="store_true",
        help="gap 큐레이션에 Anthropic web_search 도구를 써서 실제 웹 근거(verified source)로 "
             "초안을 채운다(기본 off — 검색 호출 자체가 추가 비용). 근거를 못 찾으면 기존 "
             "로그 전용 경로(curated_from_logs)로 폴백한다.")
    parser.add_argument(
        "--web-search-daily-cap", type=int, default=5,
        help="사이클당 web_search 경로를 시도할 gap 수 상한(기본 5) — --use-web-search일 때만 적용")
    parser.add_argument(
        "--cluster-paraphrases", action="store_true",
        help="mine_gaps 전에 norm_query를 임베딩 유사도로 묶어 paraphrase를 하나의 gap으로 "
             "본다(기본 off — 임베딩 모델을 추가로 로딩/호출). mine.py 자체는 무수정.")
    parser.add_argument(
        "--check-agentic-regression", action="store_true",
        help="eval/agentic_eval.py(멀티홉 진단)를 돌려 이전 사이클보다 떨어졌으면 알림만 "
             "띄운다(기본 off — LLM 호출 추가 비용). 승격 게이트는 절대 안 막음(agentic_eval.py "
             "자체 HARD CONSTRAINT, 골드셋이 6개뿐이라 false positive 위험이 큼).")
    args = parser.parse_args()
    window_days = args.window_days or None

    wiki_store.init_db(seed=True)
    try:
        result = run_cycle(
            gold_path=args.gold, k=args.k, window_days=window_days,
            use_web_search=args.use_web_search,
            web_search_daily_cap=args.web_search_daily_cap,
            cluster_paraphrases=args.cluster_paraphrases,
            check_agentic_regression=args.check_agentic_regression,
        )
    except Exception:
        # 사이클이 죽어도 종모양 알림으로 남겨야 한다 — 예외는 삼키지 않고
        # 재raise해 hermes cron의 실패 상태와 traceback이 그대로 cron 로그에
        # 남게 한다. str(e)를 notifications에 저장하면 인증 없는 GET
        # /notifications를 통해 내부 에러 메시지가 노출되므로 제네릭한 문구만 남긴다.
        wiki_store.add_notification(
            "error", "갱신 사이클 실패",
            "갱신 사이클이 예외로 중단되었습니다. 자세한 내용은 cron 로그를 확인하세요.")
        raise

    print(f"mined gaps: {result['mined']}")
    print(f"skipped (already-rejected gaps): {result['skipped_rejected_gaps']}")
    print(f"feedback: {result['feedback']}")
    print(f"daily_cap: {result['daily_cap']}")
    print(f"shadow written: {result['shadow_written']}")
    print(f"web curated: {result['web_curated']}")
    print(f"rejected: {result['rejected']}")
    promote_result = result["promote"]
    print(f"promote: promoted={promote_result['promoted']} "
          f"activated={promote_result['activated_entry_ids']} "
          f"skipped={promote_result['skipped_entry_ids']}")
    print(f"  base:      {promote_result['base']}")
    print(f"  candidate: {promote_result['candidate']}")
    print(f"  gap_recall: {promote_result['gap_recall']}")
    if "agentic" in result:
        print(f"agentic: {result['agentic']}")
    for level, title, _ in result["notifications"]:
        print(f"notification[{level}]: {title}")


if __name__ == "__main__":
    main()
