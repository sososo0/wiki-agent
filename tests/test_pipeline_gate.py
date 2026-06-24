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


def _stub_judge_high(patch, existing_entries):
    return 1.0, "ok"


def _stub_judge_low(patch, existing_entries):
    return 0.1, "hallucination risk: fabricated benchmark numbers"


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


def test_curated_from_web_without_verified_source_is_blocked():
    """★ HARD CONSTRAINT 확장: curated_from_web도 agent_generated와 동일한
    위험군(LLM이 스스로 근거를 댐)이라 미검증 source면 차단되어야 한다."""
    patch = {**GOOD_PATCH, "provenance": "curated_from_web",
             "sources": [{"type": "web", "url": "https://example.com", "verified": False}]}
    ok, reason = gate.passes_gate(
        patch, today_writes=0, existing_entries=EXISTING_ENTRIES,
        judge_fn=_stub_judge_high,
    )
    assert ok is False
    assert reason == "curated_from_web requires a verified source"


def test_curated_from_web_with_verified_source_is_allowed_past_step_1():
    patch = {**GOOD_PATCH, "provenance": "curated_from_web",
             "sources": [{"type": "web", "url": "https://example.com/a", "verified": True},
                         {"type": "web", "url": "https://example.com/b", "verified": True}]}
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
    assert ok is False
    assert reason.startswith("failed grounding/contradiction check")
    assert "hallucination risk" in reason


def test_default_judge_fn_flags_contradiction_with_existing_entry(monkeypatch):
    """default_judge_fn의 JSON 파싱/스코어링 로직을 가짜 응답으로 검증(API 호출 없음)."""

    class _FakeTextBlock:
        type = "text"
        text = (
            '{"self_contradictory": false, "contradicts_existing": true, '
            '"contradicting_entry_id": "wiki_0001", "hallucination_risk": false, '
            '"explanation": "recommends fixed delay, existing entry requires backoff with jitter"}'
        )

    class _FakeResponse:
        content = [_FakeTextBlock()]

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(gate, "_anthropic_client", lambda: _FakeClient())

    score, reason = gate.default_judge_fn(GOOD_PATCH, EXISTING_ENTRIES)
    assert score == 0.0
    assert "wiki_0001" in reason


def test_default_judge_fn_passes_when_no_issues_flagged(monkeypatch):
    class _FakeTextBlock:
        type = "text"
        text = (
            '{"self_contradictory": false, "contradicts_existing": false, '
            '"contradicting_entry_id": null, "hallucination_risk": false, '
            '"explanation": "consistent and unremarkable"}'
        )

    class _FakeResponse:
        content = [_FakeTextBlock()]

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(gate, "_anthropic_client", lambda: _FakeClient())

    score, reason = gate.default_judge_fn(GOOD_PATCH, EXISTING_ENTRIES)
    assert score == 1.0
