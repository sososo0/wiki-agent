"""
wiki-agent / tests / test_pipeline_promote.py

simulate_candidate_retriever의 supersedes 병합과 promote_if_better의
"회귀 없을 때만 커밋" 로직을 검증한다. embed_fn/rerank_fn/evaluate_fn을
스텁 주입해 실제 ML 모델/LLM 호출 없이(tmp DB, 오프라인) 검증한다.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from core import wiki_store
from core.pipeline import promote


def _embed_fn(texts):
    """banana 키워드가 포함된 텍스트를 [1,0] 쪽으로 보내는 스텁(test_retrieval.py와 동일 패턴)."""
    vecs = []
    for t in texts:
        vecs.append([1.0, 0.0] if "banana" in t.lower() else [0.0, 1.0])
    return np.array(vecs)


def _identity_rerank_fn(query, texts):
    return list(range(len(texts), 0, -1))


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_promote_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def test_simulate_candidate_retriever_merges_supersedes_and_new_entries(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    # wiki_0001을 banana 내용으로 교체하는 update-type shadow candidate
    wiki_store.add_entry(
        "wiki_0001__candidate", "Banana retries",
        "Use banana-flavored exponential backoff.", "banana banana banana",
        status="shadow", provenance="curated_from_logs", supersedes="wiki_0001",
    )
    # 완전히 새로운 shadow entry (supersedes 없음)
    wiki_store.add_entry(
        "wiki_gap_banana_1", "Banana topic",
        "All about bananas.", "banana content",
        status="shadow", provenance="curated_from_logs",
    )

    retriever = promote.simulate_candidate_retriever(embed_fn=_embed_fn, rerank_fn=_identity_rerank_fn)
    results = retriever("banana query", k=5)
    ids = [r["entry_id"] for r in results]

    assert "wiki_0001" in ids  # supersedes 병합으로 target_id 그대로 유지
    assert "wiki_gap_banana_1" in ids
    assert "wiki_0001__candidate" not in ids  # candidate 행 자체는 노출되지 않음

    merged = next(r for r in results if r["entry_id"] == "wiki_0001")
    assert merged["canonical"] == "Use banana-flavored exponential backoff."  # shadow 내용으로 교체됨


def _stub_evaluate_factory(base_scores, candidate_scores):
    def _evaluate_fn(retriever, gold, k=5):
        return base_scores if retriever is wiki_store.search_wiki else candidate_scores
    return _evaluate_fn


def test_promote_if_better_does_not_commit_on_regression(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.add_entry(
        "wiki_gap_test_1", "New topic", "New canonical.", "body",
        status="shadow", provenance="curated_from_logs",
    )
    evaluate_fn = _stub_evaluate_factory(
        base_scores={"recall@k": 0.9, "mrr": 0.8, "correctness": 0.7},
        candidate_scores={"recall@k": 0.5, "mrr": 0.4, "correctness": 0.3},  # 회귀
    )

    result = promote.promote_if_better([{"q": "x"}], k=5, evaluate_fn=evaluate_fn)

    assert result["promoted"] is False
    assert result["activated_entry_ids"] == []
    shadow_ids = {e["entry_id"] for e in wiki_store.list_shadow_entries()}
    assert "wiki_gap_test_1" in shadow_ids  # 그대로 shadow 유지


def test_promote_if_better_commits_when_no_regression(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.add_entry(
        "wiki_0001__candidate", "Better retries",
        "Updated canonical content.", "body",
        status="shadow", provenance="curated_from_logs", supersedes="wiki_0001",
    )
    wiki_store.add_entry(
        "wiki_gap_test_2", "New topic", "New canonical.", "body",
        status="shadow", provenance="curated_from_logs",
    )
    evaluate_fn = _stub_evaluate_factory(
        base_scores={"recall@k": 0.7, "mrr": 0.6, "correctness": 0.5},
        candidate_scores={"recall@k": 0.8, "mrr": 0.7, "correctness": 0.6},  # 회귀 없음
    )

    result = promote.promote_if_better([{"q": "x"}], k=5, evaluate_fn=evaluate_fn)

    assert result["promoted"] is True
    assert set(result["activated_entry_ids"]) == {"wiki_0001", "wiki_gap_test_2"}

    active_ids = {e["entry_id"] for e in wiki_store.list_active_entries()}
    assert "wiki_0001" in active_ids
    assert "wiki_gap_test_2" in active_ids

    updated = next(e for e in wiki_store.list_active_entries() if e["entry_id"] == "wiki_0001")
    assert updated["canonical"] == "Updated canonical content."

    remaining_shadow_ids = {e["entry_id"] for e in wiki_store.list_shadow_entries()}
    assert remaining_shadow_ids == set()  # candidate/new 모두 active 또는 deprecated로 빠짐
