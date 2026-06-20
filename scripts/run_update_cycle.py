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
        )
        summary["shadow_written"].append(patch["entry_id"])
        reindex.reindex_changed([patch["entry_id"]])

    gold = load_gold(gold_path or GOLD_PATH)
    summary["promote"] = promote.promote_if_better(gold, k=k, evaluate_fn=evaluate_fn)
    return summary


def main():
    parser = argparse.ArgumentParser(description="피드백 파이프라인 1사이클 실행")
    parser.add_argument("--gold", default=None)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    wiki_store.init_db(seed=True)
    result = run_cycle(gold_path=args.gold, k=args.k)

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


if __name__ == "__main__":
    main()
