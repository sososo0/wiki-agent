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
    llm_fn: Optional[Callable] = None,
    judge_fn: Optional[Callable] = None,
    evaluate_fn: Optional[Callable] = None,
) -> Dict[str, Any]:
    from eval.run_eval import GOLD_PATH, evaluate, load_gold

    evaluate_fn = evaluate_fn or evaluate

    feedback_agg = ingest.ingest_feedback(wiki_store.list_feedback())
    ingested_retrieval = ingest.ingest_retrieval_log(wiki_store.list_retrieval_log())
    gaps = mine.mine_gaps(ingested_retrieval, min_freq=min_freq, score_threshold=score_threshold)

    daily_cap = _daily_cap_from_feedback(feedback_agg["down_rate"])
    existing_entries = wiki_store.list_active_entries()
    since_ts = time.time() - 86400

    summary: Dict[str, Any] = {
        "mined": len(gaps),
        "feedback": feedback_agg,
        "daily_cap": daily_cap,
        "shadow_written": [],
        "rejected": [],
        "promote": None,
    }

    for gap in gaps:
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
    args = parser.parse_args()

    wiki_store.init_db(seed=True)
    try:
        result = run_cycle(gold_path=args.gold, k=args.k)
    except Exception as e:
        # 사이클이 죽어도 종모양 알림으로 보여야 한다 — hermes cron 로그만 보는
        # 사람은 거의 없으니 데모 UI에도 남긴다. 삼키지 않고 그대로 재raise해
        # hermes cron 자체의 실패 상태(`hermes cron list`)도 정상적으로 남게 한다.
        wiki_store.add_notification("error", "갱신 사이클 실패", str(e))
        raise

    print(f"mined gaps: {result['mined']}")
    print(f"feedback: {result['feedback']}")
    print(f"daily_cap: {result['daily_cap']}")
    print(f"shadow written: {result['shadow_written']}")
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
