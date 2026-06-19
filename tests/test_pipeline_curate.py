"""
wiki-agent / tests / test_pipeline_curate.py

curate()의 patch 구조/필수 필드를 스텁 llm_fn으로 검증. 네트워크 없음.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import curate

GAP = {
    "type": "gap",
    "norm_query": "how do i cancel a subscription",
    "query_examples": ["how do I cancel a subscription", "cancel subscription steps"],
    "freq": 4,
    "avg_top_score": -1.5,
}


def _stub_llm_fn(query_examples):
    return {
        "topic": "Cancelling a subscription",
        "canonical": "Go to account settings and click cancel.",
        "body_md": "Navigate to Account > Subscription > Cancel.",
    }


def test_curate_produces_well_formed_patch():
    patch = curate.curate(GAP, llm_fn=_stub_llm_fn)
    assert patch["op"] == "create"
    assert patch["entry_id"].startswith("wiki_gap_")
    assert patch["topic"] == "Cancelling a subscription"
    assert patch["provenance"] == "curated_from_logs"
    assert patch["confidence"] == 0.5
    assert len(patch["sources"]) == 2
    for src, q in zip(patch["sources"], GAP["query_examples"]):
        assert src == {"type": "retrieval_log_query", "query": q, "verified": False}


def test_curate_entry_id_is_deterministic_for_same_gap():
    p1 = curate.curate(GAP, llm_fn=_stub_llm_fn)
    p2 = curate.curate(GAP, llm_fn=_stub_llm_fn)
    assert p1["entry_id"] == p2["entry_id"]


def test_curate_propagates_llm_fn_errors():
    def _broken_llm_fn(qs):
        raise ValueError("bad json")

    import pytest
    with pytest.raises(ValueError):
        curate.curate(GAP, llm_fn=_broken_llm_fn)
