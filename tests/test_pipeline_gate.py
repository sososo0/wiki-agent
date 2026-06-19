"""
wiki-agent / tests / test_pipeline_gate.py

passes_gate의 5단계를 각각 단독으로 깨는 patch로 검증한다. LLM 호출은
judge_fn 스텁으로 대체 — 네트워크/비용 없음. 특히 agent_generated +
미검증 source 차단은 CLAUDE.md HARD CONSTRAINT를 직접 증명하는 테스트다.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import gate

GOOD_PATCH = {
    "op": "create",
    "entry_id": "wiki_gap_foo_123",
    "topic": "Foo bar",
    "canonical": "Foo bar baz qux.",
    "body_md": "Foo bar baz qux details.",
    "provenance": "curated_from_logs",
    "sources": [
        {"type": "retrieval_log_query", "query": "what is foo bar", "verified": False},
        {"type": "retrieval_log_query", "query": "explain foo bar baz", "verified": False},
    ],
}

EXISTING_ENTRIES = [
    {"entry_id": "wiki_0001", "topic": "Retry backoff strategy",
     "canonical": "Use exponential backoff with jitter for transient failures."},
]


def _stub_judge_high(patch):
    return 1.0


def _stub_judge_low(patch):
    return 0.1


def test_good_patch_passes():
    ok, reason = gate.passes_gate(
        GOOD_PATCH, today_writes=0, existing_entries=EXISTING_ENTRIES,
        judge_fn=_stub_judge_high,
    )
    assert (ok, reason) == (True, "ok")


def test_agent_generated_without_verified_source_is_blocked():
    """★ HARD CONSTRAINT: agent_generated 단독(미검증 source)은 무조건 차단."""
    patch = {**GOOD_PATCH, "provenance": "agent_generated",
             "sources": [{"type": "guess", "verified": False}]}
    ok, reason = gate.passes_gate(
        patch, today_writes=0, existing_entries=EXISTING_ENTRIES,
        judge_fn=_stub_judge_high,
    )
    assert ok is False
    assert reason == "agent_generated requires a verified source"


def test_agent_generated_with_verified_source_is_allowed_past_step_1():
    patch = {**GOOD_PATCH, "provenance": "agent_generated",
             "sources": [{"type": "doc", "verified": True},
                         {"type": "doc", "verified": True}]}
    ok, reason = gate.passes_gate(
        patch, today_writes=0, existing_entries=EXISTING_ENTRIES,
        judge_fn=_stub_judge_high,
    )
    assert (ok, reason) == (True, "ok")


def test_daily_cap_exceeded_is_blocked():
    ok, reason = gate.passes_gate(
        GOOD_PATCH, today_writes=20, existing_entries=EXISTING_ENTRIES,
        daily_cap=20, judge_fn=_stub_judge_high,
    )
    assert (ok, reason) == (False, "daily_cap exceeded")


def test_insufficient_source_diversity_is_blocked():
    patch = {**GOOD_PATCH, "sources": [GOOD_PATCH["sources"][0]]}
    ok, reason = gate.passes_gate(
        patch, today_writes=0, existing_entries=EXISTING_ENTRIES,
        min_sources=2, judge_fn=_stub_judge_high,
    )
    assert (ok, reason) == (False, "insufficient source diversity")


def test_near_duplicate_of_existing_entry_is_blocked():
    patch = {**GOOD_PATCH, "topic": "Retry backoff strategy",
             "canonical": "Use exponential backoff with jitter for transient failures."}
    ok, reason = gate.passes_gate(
        patch, today_writes=0, existing_entries=EXISTING_ENTRIES,
        judge_fn=_stub_judge_high,
    )
    assert (ok, reason) == (False, "near-duplicate of existing entry")


def test_failed_grounding_check_is_blocked():
    ok, reason = gate.passes_gate(
        GOOD_PATCH, today_writes=0, existing_entries=EXISTING_ENTRIES,
        judge_fn=_stub_judge_low,
    )
    assert (ok, reason) == (False, "failed grounding/contradiction check")
