"""
wiki-agent / tests / test_pipeline_parse.py

parse_section_text/parse_markdown_file/parse_directory의 실패 격리를 검증.
DB/LLM/모델 없음.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import parse


def test_parse_section_text_multiple_header_levels():
    text = (
        "# Top\n"
        "intro text\n"
        "## Sub A\n"
        "body a\n"
        "### Sub A 1\n"
        "body a1\n"
        "## Sub B\n"
        "body b\n"
    )
    sections = parse.parse_section_text(text)
    assert [s["heading_path"] for s in sections] == [
        ["Top"], ["Top", "Sub A"], ["Top", "Sub A", "Sub A 1"], ["Top", "Sub B"],
    ]
    assert sections[0]["text"] == "intro text"
    assert sections[2]["text"] == "body a1"


def test_parse_section_text_no_header_is_single_section():
    text = "just plain text\nwith no headers at all"
    sections = parse.parse_section_text(text)
    assert len(sections) == 1
    assert sections[0]["heading_path"] == []
    assert sections[0]["text"] == text


def test_parse_section_text_consecutive_headers_no_body():
    text = "# A\n## B\nbody b\n"
    sections = parse.parse_section_text(text)
    assert sections[0]["heading_path"] == ["A"]
    assert sections[0]["text"] == ""
    assert sections[1]["heading_path"] == ["A", "B"]
    assert sections[1]["text"] == "body b"


def test_parse_markdown_file_normal(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("# Title\nsome body\n", encoding="utf-8")
    res = parse.parse_markdown_file(str(f))
    assert res["error"] is None
    assert res["doc_hash"] is not None
    assert len(res["sections"]) == 1


def test_parse_markdown_file_empty(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    res = parse.parse_markdown_file(str(f))
    assert res["error"] is None
    assert res["sections"] == []


def test_parse_markdown_file_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(parse, "MAX_FILE_BYTES", 10)
    f = tmp_path / "big.md"
    f.write_text("x" * 100, encoding="utf-8")
    res = parse.parse_markdown_file(str(f))
    assert res["error"] is not None
    assert "too large" in res["error"]
    assert res["sections"] == []


def test_parse_markdown_file_missing_path():
    res = parse.parse_markdown_file("/nonexistent/path/doc.md")
    assert res["error"] is not None
    assert res["sections"] == []


def test_parse_markdown_file_bad_encoding_best_effort(tmp_path):
    f = tmp_path / "bad.md"
    # 대부분 유효한 UTF-8 텍스트에 적은 양의 잘못된 바이트만 섞음 -> best-effort 통과
    f.write_bytes(b"# Title\nmostly valid text " + b"\xff" + b" more valid text\n")
    res = parse.parse_markdown_file(str(f))
    assert res["error"] is None
    assert len(res["sections"]) >= 1


def test_encoding_error_threshold_is_configurable(tmp_path, monkeypatch):
    """ENCODING_ERROR_THRESHOLD를 낮추면, 이전엔 best-effort로 통과했던 같은
    입력이 인코딩 오류로 실패해야 한다(MAX_FILE_BYTES와 같은 env-var화 패턴)."""
    f = tmp_path / "bad.md"
    f.write_bytes(b"# Title\nmostly valid text " + b"\xff" + b" more valid text\n")

    res_default = parse.parse_markdown_file(str(f))
    assert res_default["error"] is None  # 기본 임계값(0.05)에서는 통과

    monkeypatch.setattr(parse, "ENCODING_ERROR_THRESHOLD", 0.0)
    res_strict = parse.parse_markdown_file(str(f))
    assert res_strict["error"] == "encoding error (too many invalid bytes)"


def test_parse_directory_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(parse, "MAX_FILE_BYTES", 10)
    good1 = tmp_path / "good1.md"
    good1.write_text("# A\nbody\n", encoding="utf-8")
    good2 = tmp_path / "good2.md"
    good2.write_text("# B\nbody\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("x" * 100, encoding="utf-8")  # MAX_FILE_BYTES 초과로 실패

    result = parse.parse_directory(str(tmp_path))
    assert len(result["parsed"]) == 2
    assert len(result["failed"]) == 1
    assert result["failed"][0]["path"] == str(bad)


def test_parse_directory_single_file(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("# Title\nbody\n", encoding="utf-8")
    result = parse.parse_directory(str(f))
    assert len(result["parsed"]) == 1
    assert result["failed"] == []
