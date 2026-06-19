"""
wiki-agent / tests / test_retrieval.py

core/retrieval.py 검증. embed_fn/rerank_fn을 스텁으로 주입해 모델 다운로드 없이
RRF 융합 로직만 검증한다. 실제 모델을 쓰는 통합 테스트 1개만 RUN_SLOW_TESTS로 가드.

실행: pytest
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import retrieval


def test_rrf_math():
    rankings = [["a", "b", "c"], ["b", "a", "c"]]
    scores = dict(retrieval.reciprocal_rank_fusion(rankings, rrf_k=60))
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 63)
    # a, b는 동률이고 c가 가장 낮음
    assert scores["c"] < scores["a"]
    assert scores["a"] == pytest.approx(scores["b"])


ENTRIES = [
    {"entry_id": "e1", "topic": "Apples", "canonical": "apple content",
     "body_md": "", "confidence": 1.0},
    {"entry_id": "e2", "topic": "Bananas", "canonical": "banana content",
     "body_md": "", "confidence": 1.0},
    {"entry_id": "e3", "topic": "Cherries", "canonical": "cherry content",
     "body_md": "", "confidence": 1.0},
]


def _embed_fn(texts):
    """텍스트에 포함된 과일 키워드로 좌표를 정하는 스텁(banana 쿼리가 e2와 가장 가까움)."""
    vecs = []
    for t in texts:
        low = t.lower()
        if "banana" in low:
            vecs.append([1.0, 0.0])
        elif "apple" in low:
            vecs.append([0.0, 1.0])
        else:
            vecs.append([0.6, 0.6])
    return np.array(vecs)


def _identity_rerank_fn(query, texts):
    """입력 순서를 그대로 보존하도록 내림차순 점수를 부여(순수 융합 결과만 검증하기 위함)."""
    return list(range(len(texts), 0, -1))


def test_hybrid_search_fuses_bm25_and_dense():
    # BM25만으로는 e2(banana)가 최하위, 하지만 쿼리가 의미적으로 banana에 가깝다.
    bm25_ranked_ids = ["e1", "e3", "e2"]
    results = retrieval.hybrid_search(
        "banana query", ENTRIES, bm25_ranked_ids, k=3, fetch_k=10,
        embed_fn=_embed_fn, rerank_fn=_identity_rerank_fn,
    )
    ids = [r["entry_id"] for r in results]
    assert set(ids) == {"e1", "e2", "e3"}
    # dense 신호 덕분에 BM25 단독 순위(e2가 마지막)보다 e2가 앞으로 와야 한다.
    assert ids.index("e2") < bm25_ranked_ids.index("e2")


def test_hybrid_search_bm25_empty_falls_back_to_dense():
    """키워드 매치가 전혀 없어도(BM25 빈 리스트) dense 랭킹만으로 결과를 낸다."""
    results = retrieval.hybrid_search(
        "banana query", ENTRIES, [], k=3, fetch_k=10,
        embed_fn=_embed_fn, rerank_fn=_identity_rerank_fn,
    )
    ids = [r["entry_id"] for r in results]
    assert ids[0] == "e2"  # dense 유사도가 가장 높은 entry


def test_hybrid_search_shape_matches_search_wiki_contract():
    results = retrieval.hybrid_search(
        "banana query", ENTRIES, ["e1", "e2", "e3"], k=2, fetch_k=10,
        embed_fn=_embed_fn, rerank_fn=_identity_rerank_fn,
    )
    assert len(results) == 2
    for r in results:
        assert set(r) == {"entry_id", "topic", "canonical", "score", "confidence"}


@pytest.mark.skipif(
    not os.environ.get("RUN_SLOW_TESTS"),
    reason="실제 sentence-transformers 모델을 다운로드/로딩한다 (RUN_SLOW_TESTS=1로 실행)")
def test_hybrid_search_with_real_models():
    results = retrieval.hybrid_search(
        "how do I retry failed requests safely",
        ENTRIES + [{"entry_id": "e4", "topic": "Retry backoff",
                    "canonical": "Use exponential backoff with jitter for retries.",
                    "body_md": "", "confidence": 1.0}],
        bm25_ranked_ids=["e4"], k=3, fetch_k=10,
    )
    assert results[0]["entry_id"] == "e4"
