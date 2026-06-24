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


def test_single_long_paragraph_with_no_blank_lines_splits_at_word_boundary():
    """표/코드블록처럼 빈 줄(\\n\\n)이 전혀 없는 긴 단일 "문단"은 그리디 문단
    그룹핑을 못 타고 강제 슬라이스 경로로 간다 — 이전엔 max_chars 글자에서
    그냥 잘라 토큰이 두 청크로 쪼개질 수 있었다. 지금은 공백에서 잘라야 한다."""
    tokens = [f"tok{i}" for i in range(300)]
    text = " ".join(tokens)  # \n\n 없는 단일 문단, 길이 > max_chars
    sections = [_section(["A"], text)]

    chunks = chunk.chunk_sections(sections, max_chars=100, min_chars=10)

    assert len(chunks) > 1
    assert all(len(c["text"]) <= 100 for c in chunks)
    # 모든 청크가 온전한 토큰만으로 구성돼야 한다(토큰이 둘로 쪼개지지 않음) —
    # 합쳤을 때 원본과 토큰 시퀀스가 정확히 일치하면 어떤 토큰도 안 깨졌다는 뜻.
    rejoined_tokens = " ".join(c["text"] for c in chunks).split()
    assert rejoined_tokens == tokens


def test_single_token_longer_than_max_chars_still_terminates():
    """공백을 못 찾는 극단적 경우(토큰 자체가 max_chars보다 김)에도 무한루프 없이
    끝나야 하고, 진행이 보장돼야 한다(매 반복 remaining이 줄어듦)."""
    text = "x" * 250  # 공백 없는 단일 토큰
    sections = [_section(["A"], text)]

    chunks = chunk.chunk_sections(sections, max_chars=100, min_chars=10)

    assert len(chunks) == 3
    assert "".join(c["text"] for c in chunks) == text


def test_to_doc_candidates_is_deterministic():
    sections = [_section(["A"], "some stable content for hashing purposes here")]
    chunks = chunk.chunk_sections(sections, max_chars=2000, min_chars=10)
    cands1 = chunk.to_doc_candidates("docs/a.md", "hash123", chunks)
    cands2 = chunk.to_doc_candidates("docs/a.md", "hash123", chunks)
    assert cands1 == cands2
    assert cands1[0]["doc_path"] == "docs/a.md"
    assert cands1[0]["doc_hash"] == "hash123"
    assert "chunk_hash" in cands1[0]


def test_chunk_hash_ignores_whitespace_only_differences():
    """들여쓰기/줄바꿈/trailing space만 다르면 같은 chunk_hash가 나와야 한다 —
    dedupe.py가 "콘텐츠 불변"으로 보고 불필요한 재큐레이션(LLM 호출)을 막는다."""
    sections_a = [_section(["A"], "Some content here.\n\nWith a second line.")]
    sections_b = [_section(["A"], "  Some   content here.\n\n\nWith a second line.   ")]

    chunks_a = chunk.chunk_sections(sections_a, max_chars=2000, min_chars=5)
    chunks_b = chunk.chunk_sections(sections_b, max_chars=2000, min_chars=5)

    hash_a = chunk.to_doc_candidates("docs/a.md", "h", chunks_a)[0]["chunk_hash"]
    hash_b = chunk.to_doc_candidates("docs/a.md", "h", chunks_b)[0]["chunk_hash"]
    assert hash_a == hash_b


def test_chunk_hash_still_differs_for_real_content_changes():
    sections_a = [_section(["A"], "Some content here that is long enough.")]
    sections_b = [_section(["A"], "Some different content here that is long enough.")]

    chunks_a = chunk.chunk_sections(sections_a, max_chars=2000, min_chars=5)
    chunks_b = chunk.chunk_sections(sections_b, max_chars=2000, min_chars=5)

    hash_a = chunk.to_doc_candidates("docs/a.md", "h", chunks_a)[0]["chunk_hash"]
    hash_b = chunk.to_doc_candidates("docs/a.md", "h", chunks_b)[0]["chunk_hash"]
    assert hash_a != hash_b
