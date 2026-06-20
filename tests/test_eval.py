"""
wiki-agent / tests / test_eval.py

평가 하니스 검증. generate/judge_answer/judge_quality(LLM 호출)는 스텁으로 대체해
네트워크 비용 없이 recall@k/mrr 계산 로직, 골드셋 스키마, qualitative=True 옵트인
확장(기존 키 불변 + groundedness/completeness/relevance 평균/리포트)을 검증한다.

실행: pytest
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from eval.run_eval import evaluate, load_gold, GOLD_PATH

# 골드셋이 참조할 수 있는 entry_id 네임스페이스: 시드(wiki_000N) +
# 파이프라인이 실제로 생성하는 두 접두사(core/pipeline/curate.py의
# wiki_gap_/wiki_doc_) — 오타·존재하지 않는 id 참조를 막기 위한 가드.
SEED_IDS = {f"wiki_000{i}" for i in range(1, 6)}
KNOWN_ID_PREFIXES = ("wiki_gap_", "wiki_doc_")


def _is_known_gold_id(entry_id: str) -> bool:
    return entry_id in SEED_IDS or entry_id.startswith(KNOWN_ID_PREFIXES)


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
    assert len(gold) == 42
    answerable = [ex for ex in gold if not ex.get("unanswerable")]
    unanswerable = [ex for ex in gold if ex.get("unanswerable")]
    assert len(answerable) == 37
    assert len(unanswerable) == 5

    for ex in answerable:
        assert set(ex) >= {"q", "gold_entry_ids", "must_contain", "gold_answer"}
        assert isinstance(ex["q"], str) and ex["q"]
        assert isinstance(ex["gold_entry_ids"], list) and ex["gold_entry_ids"]
        assert all(_is_known_gold_id(eid) for eid in ex["gold_entry_ids"])
        assert isinstance(ex["must_contain"], list) and ex["must_contain"]
        assert isinstance(ex["gold_answer"], str) and ex["gold_answer"]

    for ex in unanswerable:
        assert isinstance(ex["q"], str) and ex["q"]
        assert ex["gold_entry_ids"] == []  # KB가 답을 모르는 문항임을 직접 표시


def test_evaluate_computes_escalation_correctness_for_unanswerable_only():
    gold = [
        {"q": "q1", "gold_entry_ids": ["a"], "must_contain": [], "gold_answer": ""},
        {"q": "q2", "gold_entry_ids": [], "must_contain": [], "gold_answer": None, "unanswerable": True},
        {"q": "q3", "gold_entry_ids": [], "must_contain": [], "gold_answer": None, "unanswerable": True},
    ]
    retriever = fake_retriever({"q1": ["a"]})
    scores = evaluate(
        retriever, gold, k=5,
        gen_fn=lambda q, hits: "stub answer",
        judge_fn=lambda answer, ex: 1,
        escalation_judge_fn=lambda answer, ex: 1 if ex["q"] == "q2" else 0,
    )
    # answerable 1문항만으로 계산 -> unanswerable이 분모를 오염시키지 않음
    assert scores["recall@k"] == pytest.approx(1.0)
    assert scores["correctness"] == pytest.approx(1.0)
    assert scores["escalation_correctness"] == pytest.approx(0.5)  # q2만 맞음


def test_evaluate_omits_escalation_key_when_no_unanswerable_items():
    gold = [{"q": "q1", "gold_entry_ids": ["a"], "must_contain": [], "gold_answer": ""}]
    retriever = fake_retriever({"q1": ["a"]})
    scores = evaluate(
        retriever, gold, k=5,
        gen_fn=lambda q, hits: "stub",
        judge_fn=lambda answer, ex: 1,
    )
    assert "escalation_correctness" not in scores


def test_evaluate_qualitative_false_by_default_omits_new_keys():
    """qualitative 인자를 안 주면(기본 False) 반환 키가 기존과 100% 동일해야 한다 —
    promote.py의 "recall@k"/"correctness" 회귀 판정 경로가 이 계약에 의존한다."""
    gold = [{"q": "q1", "gold_entry_ids": ["a"], "must_contain": [], "gold_answer": ""}]
    retriever = fake_retriever({"q1": ["a"]})
    scores = evaluate(
        retriever, gold, k=5,
        gen_fn=lambda q, hits: "stub",
        judge_fn=lambda answer, ex: 1,
    )
    assert set(scores) == {"recall@k", "mrr", "correctness"}


def test_evaluate_qualitative_true_adds_rubric_averages_and_report():
    gold = [
        {"q": "q1", "gold_entry_ids": ["a"], "must_contain": [], "gold_answer": ""},
        {"q": "q2", "gold_entry_ids": ["b"], "must_contain": [], "gold_answer": ""},
    ]
    retriever = fake_retriever({"q1": ["a"], "q2": ["b"]})
    stub_quality = {
        "q1": {"groundedness": 4, "completeness": 5, "relevance": 3, "rationale": "r1"},
        "q2": {"groundedness": 2, "completeness": 3, "relevance": 5, "rationale": "r2"},
    }
    scores = evaluate(
        retriever, gold, k=5,
        gen_fn=lambda q, hits: f"answer for {q}",
        judge_fn=lambda answer, ex: 1,
        qualitative=True,
        quality_judge_fn=lambda answer, ex: stub_quality[ex["q"]],
    )
    # 기존 키는 그대로 보존
    assert scores["recall@k"] == pytest.approx(1.0)
    assert scores["correctness"] == pytest.approx(1.0)
    # 새 키는 옵트인으로만 추가
    assert scores["groundedness"] == pytest.approx((4 + 2) / 2)
    assert scores["completeness"] == pytest.approx((5 + 3) / 2)
    assert scores["relevance"] == pytest.approx((3 + 5) / 2)
    assert len(scores["qualitative_report"]) == 2
    assert scores["qualitative_report"][0]["q"] == "q1"
    assert scores["qualitative_report"][0]["rationale"] == "r1"


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
