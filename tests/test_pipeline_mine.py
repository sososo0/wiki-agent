"""
wiki-agent / tests / test_pipeline_mine.py

ingest 정규화/필터와 mine_gaps의 빈도+점수 조건을 검증. DB 없음, 모델은
cluster_paraphrased_queries 테스트에서만 스텁 embed_fn으로 대체(실제 로딩 없음).

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

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


def _make_embed_fn(vector_by_query, default=(0.0, 1.0)):
    def _embed_fn(texts):
        return np.array([vector_by_query.get(t, list(default)) for t in texts])
    return _embed_fn


def test_cluster_paraphrased_queries_merges_similar_norm_queries():
    vectors = {
        "how do i reset my password": [1.0, 0.0],
        "how can i reset password": [0.9, 0.0],       # 위와 유사(dot=0.9 >= threshold)
        "how do i delete my account": [0.0, 1.0],      # 무관한 질문
    }
    rows = [
        {"norm_query": "how do i reset my password"},
        {"norm_query": "how can i reset password"},
        {"norm_query": "how do i delete my account"},
    ]

    result = ingest.cluster_paraphrased_queries(
        rows, embed_fn=_make_embed_fn(vectors), similarity_threshold=0.85)

    assert result[0]["norm_query"] == result[1]["norm_query"]
    assert result[0]["norm_query"] == "how can i reset password"  # 사전순 최솟값(결정적)
    assert result[2]["norm_query"] == "how do i delete my account"  # 무관한 건 그대로


def test_cluster_paraphrased_queries_respects_similarity_threshold():
    vectors = {
        "how do i reset my password": [1.0, 0.0],
        "how can i reset password": [0.9, 0.0],
    }
    rows = [
        {"norm_query": "how do i reset my password"},
        {"norm_query": "how can i reset password"},
    ]

    result = ingest.cluster_paraphrased_queries(
        rows, embed_fn=_make_embed_fn(vectors), similarity_threshold=0.95)  # 0.9 < 0.95

    assert result[0]["norm_query"] != result[1]["norm_query"]


def test_cluster_paraphrased_queries_empty_rows_returns_empty():
    assert ingest.cluster_paraphrased_queries([], embed_fn=_make_embed_fn({})) == []


def test_cluster_paraphrased_queries_single_unique_query_skips_embed_fn():
    calls = []
    rows = [{"norm_query": "only one question"}, {"norm_query": "only one question"}]

    result = ingest.cluster_paraphrased_queries(
        rows, embed_fn=lambda texts: calls.append(texts) or np.zeros((len(texts), 2)))

    assert calls == []  # unique_queries가 1개뿐이면 임베딩 호출 자체를 건너뜀
    assert result == rows


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


def test_mine_gaps_reports_no_hit_score_unmixed_when_only_some_occurrences_miss():
    """그룹에 미스가 1건이라도 있으면 avg_top_score는 그대로 NO_HIT_SCORE여야 한다 —
    실제 점수(5.0, 5.0)와 NO_HIT_SCORE(-1e9)를 같은 평균에 섞으면 -3.3e8 같은
    의미 없는 값이 나오는데, 이 값이 curate.py의 LLM 프롬프트에 그대로 들어간다."""
    rows = [
        {"query": "mixed", "norm_query": "mixed", "retrieved": [{"entry_id": "x", "score": 5.0}]},
        {"query": "mixed", "norm_query": "mixed", "retrieved": [{"entry_id": "x", "score": 5.0}]},
        {"query": "mixed", "norm_query": "mixed", "retrieved": []},
    ]
    gaps = mine.mine_gaps(rows, min_freq=3, score_threshold=0.0)
    assert len(gaps) == 1
    assert gaps[0]["avg_top_score"] == mine.NO_HIT_SCORE


def test_mine_gaps_averages_only_real_scores_when_no_misses():
    """미스가 전혀 없으면 평균은 그대로 실제 점수들의 산술 평균이어야 한다(기존 동작 보존)."""
    rows = [
        {"query": "all hit", "norm_query": "all hit", "retrieved": [{"entry_id": "x", "score": -1.0}]},
        {"query": "all hit", "norm_query": "all hit", "retrieved": [{"entry_id": "x", "score": -3.0}]},
        {"query": "all hit", "norm_query": "all hit", "retrieved": [{"entry_id": "x", "score": -2.0}]},
    ]
    gaps = mine.mine_gaps(rows, min_freq=3, score_threshold=0.0)
    assert len(gaps) == 1
    assert gaps[0]["avg_top_score"] == -2.0
