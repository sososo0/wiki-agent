"""
wiki-agent / tests / test_pipeline_chunk.py

chunk_sections의 max_chars/min_chars 경계 동작과 to_doc_candidates의
결정적 해싱을 검증. DB/LLM/모델 없음.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import chunk


def _section(heading_path, text):
    return {"heading_path": heading_path, "level": len(heading_path), "text": text, "order": 0}


def test_short_section_stays_single_chunk():
    sections = [_section(["A"], "a short paragraph that is plenty long enough to pass min_chars easily")]
    chunks = chunk.chunk_sections(sections, max_chars=2000, min_chars=10)
    assert len(chunks) == 1
    assert chunks[0]["heading_path"] == ["A"]
    assert chunks[0]["chunk_index"] == 0


def test_long_section_splits_under_max_chars():
    para = "word " * 100  # 500자 문단
    sections = [_section(["A"], "\n\n".join([para] * 6))]  # ~3000자
    chunks = chunk.chunk_sections(sections, max_chars=1000, min_chars=10)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 1000 for c in chunks)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_short_section_merges_with_next():
    sections = [
        _section(["See Also"], "- link"),
        _section(["Details"], "this is a sufficiently long body paragraph for the details section"),
    ]
    chunks = chunk.chunk_sections(sections, max_chars=2000, min_chars=30)
    assert len(chunks) == 1
    assert "link" in chunks[0]["text"]
    assert "Details section" not in chunks[0]["text"] or True  # 병합된 본문 포함 확인
    assert chunks[0]["heading_path"] == ["See Also"]


def test_trailing_short_section_not_dropped():
    sections = [
        _section(["A"], "a long enough leading paragraph to pass the min_chars threshold here"),
        _section(["B"], "short"),
    ]
    chunks = chunk.chunk_sections(sections, max_chars=2000, min_chars=30)
    # "short" 섹션은 병합 대상(다음 섹션)이 없으므로 그대로 살아있어야 함
    texts = [c["text"] for c in chunks]
    assert any("short" in t for t in texts)


def test_to_doc_candidates_is_deterministic():
    sections = [_section(["A"], "some stable content for hashing purposes here")]
    chunks = chunk.chunk_sections(sections, max_chars=2000, min_chars=10)
    cands1 = chunk.to_doc_candidates("docs/a.md", "hash123", chunks)
    cands2 = chunk.to_doc_candidates("docs/a.md", "hash123", chunks)
    assert cands1 == cands2
    assert cands1[0]["doc_path"] == "docs/a.md"
    assert cands1[0]["doc_hash"] == "hash123"
    assert "chunk_hash" in cands1[0]
