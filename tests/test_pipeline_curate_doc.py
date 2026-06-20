"""
wiki-agent / tests / test_pipeline_curate_doc.py

curate_doc_chunk()의 patch 구조/필수 필드를 스텁 llm_fn으로 검증, make_doc_judge_fn이
entry_id로 원본 chunk_text를 찾아 default_doc_judge_fn에 넘기는지 검증. 네트워크 없음.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import curate

CANDIDATE = {
    "type": "doc_chunk",
    "doc_path": "docs/retry.md",
    "doc_hash": "deadbeef",
    "chunk_index": 0,
    "heading_path": ["Retries", "Backoff"],
    "text": "Use exponential backoff with jitter for transient failures.",
    "chunk_hash": "abc123",
}


def _stub_doc_llm_fn(heading_path, text):
    return {
        "topic": "Backoff strategy",
        "canonical": "Use exponential backoff with jitter.",
        "body_md": "Retry transient failures with exponentially increasing delay plus jitter.",
    }


def test_curate_doc_chunk_produces_well_formed_patch():
    patch = curate.curate_doc_chunk(CANDIDATE, llm_fn=_stub_doc_llm_fn)
    assert patch["op"] == "create"
    assert patch["entry_id"].startswith("wiki_doc_")
    assert patch["topic"] == "Backoff strategy"
    assert patch["provenance"] == "doc_verified"
    assert patch["confidence"] == 0.9
    assert len(patch["sources"]) == 1
    src = patch["sources"][0]
    assert src["type"] == "document"
    assert src["path"] == "docs/retry.md"
    assert src["heading_path"] == ["Retries", "Backoff"]
    assert src["chunk_hash"] == "abc123"
    assert src["verified"] is True


def test_curate_doc_chunk_entry_id_is_deterministic():
    p1 = curate.curate_doc_chunk(CANDIDATE, llm_fn=_stub_doc_llm_fn)
    p2 = curate.curate_doc_chunk(CANDIDATE, llm_fn=_stub_doc_llm_fn)
    assert p1["entry_id"] == p2["entry_id"]


def test_curate_doc_chunk_entry_id_differs_by_chunk_index():
    other = {**CANDIDATE, "chunk_index": 1}
    p1 = curate.curate_doc_chunk(CANDIDATE, llm_fn=_stub_doc_llm_fn)
    p2 = curate.curate_doc_chunk(other, llm_fn=_stub_doc_llm_fn)
    assert p1["entry_id"] != p2["entry_id"]


def test_curate_doc_chunk_propagates_llm_fn_errors():
    def _broken_llm_fn(hp, t):
        raise ValueError("bad json")

    import pytest
    with pytest.raises(ValueError):
        curate.curate_doc_chunk(CANDIDATE, llm_fn=_broken_llm_fn)


def test_make_doc_judge_fn_passes_real_chunk_text_not_source_dict(monkeypatch):
    """gate.default_judge_fn은 sources에 "query"가 없는 문서 출처를 dict 그대로
    stringify해 judge에 넘긴다(실제 본문을 못 봄) — make_doc_judge_fn은 entry_id로
    원본 chunk_text를 찾아 default_doc_judge_fn에 직접 넘겨야 한다."""
    captured = {}

    def _stub_default_doc_judge_fn(patch, chunk_text, model=curate.CURATE_MODEL):
        captured["patch"] = patch
        captured["chunk_text"] = chunk_text
        return 0.95

    monkeypatch.setattr(curate, "default_doc_judge_fn", _stub_default_doc_judge_fn)

    chunk_text_by_entry_id = {"wiki_doc_abc": "the real source text"}
    judge_fn = curate.make_doc_judge_fn(chunk_text_by_entry_id)
    score = judge_fn({"entry_id": "wiki_doc_abc", "topic": "t"})

    assert score == 0.95
    assert captured["chunk_text"] == "the real source text"


def test_make_doc_judge_fn_defaults_to_empty_text_for_unknown_entry_id(monkeypatch):
    captured = {}

    def _stub_default_doc_judge_fn(patch, chunk_text, model=curate.CURATE_MODEL):
        captured["chunk_text"] = chunk_text
        return 0.0

    monkeypatch.setattr(curate, "default_doc_judge_fn", _stub_default_doc_judge_fn)

    judge_fn = curate.make_doc_judge_fn({})
    judge_fn({"entry_id": "unknown", "topic": "t"})

    assert captured["chunk_text"] == ""
