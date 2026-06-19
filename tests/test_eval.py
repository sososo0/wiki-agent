"""
wiki-agent / tests / test_eval.py

평가 하니스 검증. generate/judge_answer(LLM 호출)은 스텁으로 대체해
네트워크 비용 없이 recall@k/mrr 계산 로직과 골드셋 스키마를 검증한다.

실행: pytest
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from eval.run_eval import evaluate, load_gold, GOLD_PATH

SEED_IDS = {f"wiki_000{i}" for i in range(1, 6)}


def fake_retriever(rows):
    """query -> 정렬된 entry_id 목록을 흉내내는 retriever 팩토리."""
    def _retrieve(query, k=5):
        return [{"entry_id": eid} for eid in rows.get(query, [])][:k]
    return _retrieve


def test_evaluate_recall_and_mrr_math():
    gold = [
        {"q": "q1", "gold_entry_ids": ["a"], "must_contain": [], "gold_answer": ""},
        {"q": "q2", "gold_entry_ids": ["b"], "must_contain": [], "gold_answer": ""},
        {"q": "q3", "gold_entry_ids": ["z"], "must_contain": [], "gold_answer": ""},
    ]
    retriever = fake_retriever({
        "q1": ["a", "x", "y"],  # rank 1 hit
        "q2": ["x", "b", "y"],  # rank 2 hit
        "q3": ["x", "y", "w"],  # miss
    })
    scores = evaluate(
        retriever, gold, k=5,
        gen_fn=lambda q, hits: "stub answer",
        judge_fn=lambda answer, ex: 1,
    )
    assert scores["recall@k"] == pytest.approx(2 / 3)
    assert scores["mrr"] == pytest.approx((1 / 1 + 1 / 2 + 0) / 3)
    assert scores["correctness"] == pytest.approx(1.0)


def test_evaluate_correctness_uses_judge_fn():
    gold = [{"q": "q1", "gold_entry_ids": ["a"], "must_contain": [], "gold_answer": ""}]
    retriever = fake_retriever({"q1": ["a"]})
    scores = evaluate(
        retriever, gold, k=5,
        gen_fn=lambda q, hits: "stub",
        judge_fn=lambda answer, ex: 0,
    )
    assert scores["correctness"] == 0.0


def test_gold_set_schema():
    gold = load_gold(GOLD_PATH)
    assert len(gold) == 20
    for ex in gold:
        assert set(ex) >= {"q", "gold_entry_ids", "must_contain", "gold_answer"}
        assert isinstance(ex["q"], str) and ex["q"]
        assert isinstance(ex["gold_entry_ids"], list) and ex["gold_entry_ids"]
        assert set(ex["gold_entry_ids"]) <= SEED_IDS
        assert isinstance(ex["must_contain"], list) and ex["must_contain"]
        assert isinstance(ex["gold_answer"], str) and ex["gold_answer"]


@pytest.mark.skipif(
    not os.environ.get("RUN_SLOW_TESTS"),
    reason="search_wiki가 이제 sentence-transformers 모델을 로드한다 (RUN_SLOW_TESTS=1로 실행)")
def test_gold_set_retrieval_sanity(tmp_path, monkeypatch):
    """실제 search_wiki(하이브리드)로 골드셋 recall@5가 합리적 수준인지 확인 (LLM 호출 없음)."""
    db_path = str(tmp_path / "test_eval_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)

    from core import wiki_store
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)

    gold = load_gold(GOLD_PATH)
    scores = evaluate(
        wiki_store.search_wiki, gold, k=5,
        gen_fn=lambda q, hits: "stub",
        judge_fn=lambda answer, ex: 1,
    )
    assert scores["recall@k"] >= 0.6
