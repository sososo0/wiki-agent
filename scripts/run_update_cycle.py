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
) -> Dict[str, Any]:
    from eval.run_eval import GOLD_PATH, evaluate, load_gold

    evaluate_fn = evaluate_fn or evaluate

    # window_days=None(또는 0)이면 기존 동작과 동일하게 전체 히스토리를 본다.
    # 기본은 14일 — retrieval_log/feedback에 retention이 없어 테이블이 무한히
    # 쌓이는데, 윈도잉이 없으면 매 사이클이 점점 커지는 전체 히스토리를 다시
    # 스캔하고, 한 번 우연히 오염된(예: 평가 질문이 3번 반복된) 쿼리가 영원히
    # gap으로 재탐지된다 — 실제로 골드셋 unanswerable 문항이 이렇게 재탐지된
    # 사례가 있었다(README "위키 자가 갱신 확인하기" 참고).
    mining_since_ts = time.time() - window_days * 86400 if window_days else None

    feedback_agg = ingest.ingest_feedback(wiki_store.list_feedback(since_ts=mining_since_ts))
    ingested_retrieval = ingest.ingest_retrieval_log(
        wiki_store.list_retrieval_log(since_ts=mining_since_ts))
    gaps = mine.mine_gaps(ingested_retrieval, min_freq=min_freq, score_threshold=score_threshold)

    daily_cap = _daily_cap_from_feedback(feedback_agg["down_rate"])
    existing_entries = wiki_store.list_active_entries()
    since_ts = time.time() - 86400

    # norm_query 단위로 "이전에 게이트가 거부한 gap"을 기억해 같은 질문이 다음
    # 사이클에 다시 mine_gaps에 잡혀도 curate/judge LLM 호출을 반복하지 않는다
    # (core/pipeline/dedupe.py의 skip_rejected와 동일한 목적 — 문서 ingestion은
    # chunk_hash로, 여긴 norm_query로 "콘텐츠 불변"을 판단). 진짜 답이 될 콘텐츠가
    # 나중에 생기면(문서 ingestion 등) 검색 점수가 양수로 돌아서 mine_gaps 자체가
    # 더는 이 질문을 gap으로 뽑지 않으므로, 이 기억은 따로 만료시킬 필요가 없다.
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

        # 웹 검색 경로는 비용이 더 크므로(검색 호출 자체가 추가 과금) 사이클당
        # 별도 상한(web_search_daily_cap)을 두고, 그 안에서만 시도한다. 근거를
        # 못 찾거나(ValueError) 호출 자체가 실패하면 기존 로그 전용 경로로
        # 조용히 폴백한다 — README "외부 검색을 하지 않는다" 한계의 opt-in 확장.
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
        reindex.reindex_changed([patch["entry_id"]])

    gold = load_gold(gold_path or GOLD_PATH)
    summary["promote"] = promote.promote_if_better(gold, k=k, evaluate_fn=evaluate_fn)

    # cycle_history에 "지금 active 상태"의 골드셋 지표 1행을 남긴다 — 승격됐으면
    # candidate(=새로 active가 된 상태), 안 됐으면 base(=그대로인 현재 active
    # 상태)가 곧 지금의 실제 품질이다. 여러 사이클에 걸친 추이를 보려면 이 값들이
    # 쌓여야 하는데, 지금까지는 notifications에 텍스트 요약만 남고 구조화된
    # 히스토리가 없었다.
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
    )

    # 로그 텍스트를 나중에 파싱하는 대신, 이미 들고 있는 구조화된 summary에서
    # 바로 알림을 만든다 — 데모의 종모양 알림 UI(GET /notifications)가 읽음.
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
    args = parser.parse_args()
    window_days = args.window_days or None

    wiki_store.init_db(seed=True)
    try:
        result = run_cycle(
            gold_path=args.gold, k=args.k, window_days=window_days,
            use_web_search=args.use_web_search,
            web_search_daily_cap=args.web_search_daily_cap,
        )
    except Exception:
        # 사이클이 죽어도 종모양 알림으로 보여야 한다 — hermes cron 로그만 보는
        # 사람은 거의 없으니 데모 UI에도 남긴다. 삼키지 않고 그대로 재raise해
        # hermes cron 자체의 실패 상태(`hermes cron list`)도 정상적으로 남게
        # 하고, 그 재raise된 예외의 전체 traceback이 cron 로그에 남으므로 거기서
        # 상세를 본다 — str(e)를 notifications에 그대로 저장하면 인증 없는
        # GET /notifications(demo/app.py)를 통해 누구나 내부 에러 메시지(경로,
        # 내부 상태 등)를 볼 수 있어 일부러 제네릭한 문구만 남긴다.
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
          f"activated={promote_result['activated_entry_ids']}")
    print(f"  base:      {promote_result['base']}")
    print(f"  candidate: {promote_result['candidate']}")
    print(f"  gap_recall: {promote_result['gap_recall']}")
    for level, title, _ in result["notifications"]:
        print(f"notification[{level}]: {title}")


if __name__ == "__main__":
    main()
