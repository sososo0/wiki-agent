"""
wiki-agent / tests / test_agentic_eval.py

agentic_eval.py 검증. decide_fn/search_fn/force_answer_fn/judge_fn 전부 스텁으로
대체해 네트워크 비용 없이 ReAct 루프(검색 N회 -> answer)와 집계 지표
(task_success_rate/avg_tool_calls/multihop_recall)를 검증한다. agentic_gold_set.jsonl
스키마도 검증한다.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from eval.agentic_eval import (
    GOLD_PATH,
    load_agentic_gold,
    run_agent_task,
    run_agentic_eval,
)


def fake_search_fn(rows):
    """query -> entry_id 목록을 흉내내는 search_fn 팩토리 (topic은 entry_id 재사용)."""
    def _search(query, k=5):
        return [{"entry_id": eid, "topic": eid} for eid in rows.get(query, [])][:k]
    return _search


def test_run_agent_task_answers_after_single_search():
    decisions = iter([
        {"action": "search", "query": "q1"},
        {"action": "answer", "answer": "final answer"},
    ])
    result = run_agent_task(
        "task1",
        search_fn=fake_search_fn({"q1": ["wiki_0001"]}),
        decide_fn=lambda task, transcript: next(decisions),
        max_turns=4,
    )
    assert result["answer"] == "final answer"
    assert result["tool_calls"] == 1
    assert result["retrieved_ids"] == {"wiki_0001"}


def test_run_agent_task_supports_multihop_searches():
    decisions = iter([
        {"action": "search", "query": "q1"},
        {"action": "search", "query": "q2"},
        {"action": "answer", "answer": "combined answer"},
    ])
    result = run_agent_task(
        "task1",
        search_fn=fake_search_fn({"q1": ["wiki_0001"], "q2": ["wiki_0005"]}),
        decide_fn=lambda task, transcript: next(decisions),
        max_turns=4,
    )
    assert result["tool_calls"] == 2
    assert result["retrieved_ids"] == {"wiki_0001", "wiki_0005"}
    assert result["answer"] == "combined answer"


def test_run_agent_task_forces_final_answer_at_max_turns():
    # decide_fn은 매번 search를 요청한다 -> max_turns에서 강제 종료되어야 함.
    result = run_agent_task(
        "task1",
        search_fn=fake_search_fn({"q": ["wiki_0001"]}),
        decide_fn=lambda task, transcript: {"action": "search", "query": "q"},
        force_answer_fn=lambda task, transcript: "forced answer",
        max_turns=2,
    )
    assert result["tool_calls"] == 2
    assert result["answer"] == "forced answer"


def test_run_agentic_eval_aggregates_success_rate_tool_calls_and_multihop_recall():
    tasks = [
        {"task": "t1", "gold_entry_ids": ["wiki_0001", "wiki_0005"], "must_contain": []},
        {"task": "t2", "gold_entry_ids": ["wiki_0002"], "must_contain": []},
    ]

    def decide_fn(task, transcript):
        if not transcript:
            return {"action": "search", "query": task}
        return {"action": "answer", "answer": f"answer for {task}"}

    def search_fn(query, k=5):
        rows = {"t1": ["wiki_0001"], "t2": ["wiki_0002"]}  # t1은 1개만 찾음(부분 multihop)
        return [{"entry_id": eid, "topic": eid} for eid in rows.get(query, [])][:k]

    def judge_fn(answer, ex):
        return 1 if ex["task"] == "t2" else 0

    result = run_agentic_eval(
        tasks, search_fn=search_fn, decide_fn=decide_fn, judge_fn=judge_fn, max_turns=4,
    )

    assert result["task_success_rate"] == pytest.approx(0.5)  # t2만 성공
    assert result["avg_tool_calls"] == pytest.approx(1.0)  # 둘 다 검색 1회
    # gold 총 3개(wiki_0001, wiki_0005, wiki_0002) 중 실제로 찾은 건 wiki_0001, wiki_0002 = 2개
    assert result["multihop_recall"] == pytest.approx(2 / 3)
    assert len(result["per_task"]) == 2


def test_agentic_gold_set_schema():
    tasks = load_agentic_gold(GOLD_PATH)
    assert len(tasks) >= 6
    for ex in tasks:
        assert set(ex) >= {"task", "gold_entry_ids", "must_contain"}
        assert isinstance(ex["task"], str) and ex["task"]
        assert isinstance(ex["gold_entry_ids"], list)
        assert len(ex["gold_entry_ids"]) >= 2  # 멀티홉: 2개 이상 엔트리 결합 필요
        assert isinstance(ex["must_contain"], list) and ex["must_contain"]
