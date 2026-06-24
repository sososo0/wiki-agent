"""
wiki-agent / core / pipeline / parse.py

문서 ingestion 파이프라인의 입력 파싱 단계. 마크다운 파일을 ATX 헤더
(#, ##, ...) 기준으로 섹션 리스트로 분리한다. DB/LLM 접근 없는 순수 함수.

예외를 던지지 않고 항상 dict를 반환한다 — 한 파일의 실패(인코딩 오류, 너무
큰 파일, OS 오류)가 디렉터리 전체 ingestion을 막지 않도록 "error" 필드로
호출부에 넘긴다.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List

MAX_FILE_BYTES = int(os.environ.get("WIKI_AGENT_MAX_DOC_BYTES", 2 * 1024 * 1024))

# UTF-8 디코딩 실패 시 errors="replace"로 best-effort 디코딩하고, "�" 비율이
# 이 임계값을 넘을 때만 진짜 인코딩 오류로 실패 처리한다(적은 수의 잘못된
# 바이트는 무시하고 계속 진행).
ENCODING_ERROR_THRESHOLD = float(os.environ.get("WIKI_AGENT_ENCODING_ERROR_THRESHOLD", "0.05"))

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_section_text(text: str) -> List[Dict[str, Any]]:
    """마크다운 텍스트 -> [{"heading_path", "level", "text", "order"}].

    헤더가 전혀 없으면 전체 텍스트를 order=0, heading_path=[] 단일 섹션으로
    취급한다(.txt 등 헤더 없는 입력의 fallback)."""
    lines = text.splitlines()
    sections: List[Dict[str, Any]] = []
    stack: List[str] = []
    cur_level = None
    cur_lines: List[str] = []
    order = 0

    def _flush():
        nonlocal order
        body = "\n".join(cur_lines).strip()
        sections.append({
            "heading_path": list(stack),
            "level": cur_level if cur_level is not None else 0,
            "text": body,
            "order": order,
        })
        order += 1

    seen_header = False
    for line in lines:
        m = _HEADER_RE.match(line)
        if m:
            if seen_header or cur_lines:
                _flush()
            seen_header = True
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: level - 1] + [title]
            cur_level = level
            cur_lines = []
        else:
            cur_lines.append(line)

    if not seen_header:
        body = text.strip()
        return [{"heading_path": [], "level": 0, "text": body, "order": 0}]

    _flush()
    return sections


def parse_markdown_file(path: str) -> Dict[str, Any]:
    """파일 1개 -> {"path", "doc_hash", "sections", "error"}.

    예외를 던지지 않고 항상 dict를 반환한다. error가 None이 아니면 호출부가
    이 문서를 건너뛰어야 한다."""
    result: Dict[str, Any] = {"path": path, "doc_hash": None, "sections": [], "error": None}

    try:
        size = os.path.getsize(path)
    except OSError as e:
        result["error"] = f"stat failed: {e}"
        return result

    if size > MAX_FILE_BYTES:
        result["error"] = f"file too large ({size}b > {MAX_FILE_BYTES}b)"
        return result

    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        result["error"] = f"read failed: {e}"
        return result

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        replaced = text.count("�")
        if len(text) > 0 and replaced / len(text) > ENCODING_ERROR_THRESHOLD:
            result["error"] = "encoding error (too many invalid bytes)"
            return result

    result["doc_hash"] = hashlib.sha256(raw).hexdigest()
    if text.strip() == "":
        return result  # sections=[] 그대로, error=None (빈 파일은 실패가 아님)

    result["sections"] = parse_section_text(text)
    return result


def parse_directory(dir_path: str, *, glob: str = "**/*.md") -> Dict[str, Any]:
    """디렉터리 재귀 탐색 -> {"parsed": [...], "failed": [{"path","error"}]}.

    각 파일을 격리 처리하므로 한 파일의 실패가 다른 파일 파싱을 막지 않는다."""
    parsed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    p = Path(dir_path)
    paths = [p] if p.is_file() else sorted(p.glob(glob))

    for file_path in paths:
        try:
            res = parse_markdown_file(str(file_path))
        except Exception as e:  # noqa: BLE001 - 파싱 단계는 절대 전체를 막으면 안 됨
            failed.append({"path": str(file_path), "error": f"unexpected error: {e}"})
            continue
        if res["error"]:
            failed.append({"path": res["path"], "error": res["error"]})
        else:
            parsed.append(res)

    return {"parsed": parsed, "failed": failed}
