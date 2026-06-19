"""
wiki-agent / tests / test_pipeline_mine.py

ingest 정규화/필터와 mine_gaps의 빈도+점수 조건을 검증. DB/모델 없음.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import ingest, mine


def test_ingest_retrieval_log_filters_short_queries_and_adds_norm_query():
    rows = [
        {"id": 1, "query": "  How do I Retry?  ", "retrieved": [], "ts": 1.0},
        {"id": 2, "query": "ok", "retrieved": [], "ts": 2.0},
    ]
    out = ingest.ingest_retrieval_log(rows)
    assert len(out) == 1
    assert out[0]["norm_query"] == "how do i retry?"


def test_ingest_feedback_aggregates_down_rate():
    rows = [{"thumb": "down"}, {"thumb": "up"}, {"thumb": "down"}, {"thumb": "up"}]
    agg = ingest.ingest_feedback(rows)
    assert agg == {"n": 4, "down": 2, "down_rate": 0.5}


def test_ingest_feedback_empty():
    assert ingest.ingest_feedback([]) == {"n": 0, "down": 0, "down_rate": 0.0}


def _row(query, top_score):
    return {"query": query, "norm_query": query.lower(),
            "retrieved": [{"entry_id": "x", "score": top_score}]}


def test_mine_gaps_requires_both_frequency_and_low_score():
    rows = (
        [_row("rare question", -1.0)] * 2          # 빈도 미달
        + [_row("frequent but answered well", 5.0)] * 5  # 점수 충분히 높음
        + [_row("frequent and unanswered", -2.0)] * 4    # 둘 다 만족 -> gap
    )
    gaps = mine.mine_gaps(rows, min_freq=3, score_threshold=0.0)
    assert len(gaps) == 1
    assert gaps[0]["norm_query"] == "frequent and unanswered"
    assert gaps[0]["freq"] == 4
    assert gaps[0]["avg_top_score"] == -2.0
    assert gaps[0]["query_examples"] == ["frequent and unanswered"]


def test_mine_gaps_handles_missing_retrieved():
    """검색 결과가 전혀 없었던 쿼리(retrieved=[])는 가장 강한 gap 신호로 취급되어야 한다."""
    rows = [{"query": "no hits", "norm_query": "no hits", "retrieved": []}] * 3
    gaps = mine.mine_gaps(rows, min_freq=3, score_threshold=0.0)
    assert len(gaps) == 1
    assert gaps[0]["avg_top_score"] == mine.NO_HIT_SCORE
