"""
wiki-agent / core / pipeline / chunk.py

parse.py가 만든 섹션 리스트를 LLM 큐레이션 입력 단위(청크)로 변환한다.
순수 함수, core/eval 어느 쪽도 import하지 않음(core/pipeline/* 컨벤션).

[청킹 전략 — 비용/레이턴시/검색품질 트레이드오프]
- 헤더(섹션) 1개를 1차 단위로 삼는다: 마크다운 헤더는 저자가 의도한 주제 경계라서
  임의 길이 슬라이딩 윈도우보다 청크당 주제 응집도가 높아 큐레이션 품질이 좋다.
- max_chars=2000(약 400~500 토큰): curate.py의 default_llm_fn 출력 스케일
  (max_tokens=1024)과 맞춤. 너무 길면 LLM이 핵심을 못 뽑고, 너무 짧으면 청크 수
  (=curate LLM 호출 수, 비용의 주된 드라이버)가 늘어난다.
- 오버랩 없음: LLM이 청크를 읽고 자기완결적인 canonical/body_md를 다시 쓰므로
  경계 손실은 큐레이션에서 흡수된다. 오버랩을 쓰면 인접 청크에 정보가 중복
  생성돼 gate의 자카드 중복 체크에 걸리거나 LLM 호출이 낭비될 위험이 크다.
- min_chars=80 미달 섹션은 다음 섹션과 병합: 한두 문장뿐인 섹션마다 LLM 호출을
  만드는 건 비용 대비 가치가 없음.
"""

import hashlib
from typing import Any, Dict, List

DEFAULT_MAX_CHARS = 2000
DEFAULT_MIN_CHARS = 80


def _last_whitespace_before(text: str, limit: int) -> int:
    """text[:limit] 안에서 가장 뒤쪽 공백 문자의 인덱스, 없으면 -1."""
    for i in range(min(limit, len(text)) - 1, -1, -1):
        if text[i].isspace():
            return i
    return -1


def _split_long(heading_path: List[str], text: str, max_chars: int) -> List[Dict[str, Any]]:
    if len(text) <= max_chars:
        return [{"heading_path": heading_path, "text": text}]

    paragraphs = text.split("\n\n")
    grouped: List[str] = []
    cur = ""
    for para in paragraphs:
        candidate = f"{cur}\n\n{para}".strip() if cur else para
        if len(candidate) > max_chars and cur:
            grouped.append(cur)
            cur = para
        else:
            cur = candidate
    if cur:
        grouped.append(cur)

    out: List[Dict[str, Any]] = []
    for g in grouped:
        if len(g) <= max_chars:
            out.append({"heading_path": heading_path, "text": g})
        else:
            # 단일 문단이 max_chars보다 긴 극단적 경우(표/코드블록 등): 단어
            # 중간이 아니라 가능하면 공백에서 잘라 큐레이션 품질 저하를 줄인다.
            # 공백을 못 찾으면 그 지점에서 강제로 자른다.
            remaining = g
            while remaining:
                if len(remaining) <= max_chars:
                    out.append({"heading_path": heading_path, "text": remaining})
                    break
                cut = _last_whitespace_before(remaining, max_chars)
                if cut <= 0:
                    cut = max_chars
                out.append({"heading_path": heading_path, "text": remaining[:cut].rstrip()})
                remaining = remaining[cut:].lstrip()
    return out


def chunk_sections(
    sections: List[Dict[str, Any]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> List[Dict[str, Any]]:
    """섹션 리스트 -> [{"heading_path", "text", "chunk_index"}].

    min_chars 미달 섹션은 다음 섹션과 병합(병합 대상이 없으면, 즉 파일의
    마지막 섹션이면 그대로 둠 — 버리지 않음). max_chars 초과 섹션은 문단
    경계에서 그리디 분할."""
    emitted: List[Dict[str, Any]] = []
    pending: Dict[str, Any] = None

    for sec in sections:
        heading_path, text = sec["heading_path"], sec["text"]
        if pending is not None:
            merged_text = f"{pending['text']}\n\n{text}".strip() if pending["text"] else text
            heading_path = pending["heading_path"] or heading_path
            text = merged_text
            pending = None

        if len(text) < min_chars:
            pending = {"heading_path": heading_path, "text": text}
            continue

        emitted.extend(_split_long(heading_path, text, max_chars))

    if pending is not None:
        emitted.extend(_split_long(pending["heading_path"], pending["text"], max_chars))

    for i, c in enumerate(emitted):
        c["chunk_index"] = i
    return emitted


def to_doc_candidates(
    doc_path: str, doc_hash: str, chunks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """chunk -> mine.mine_gaps() 출력과 같은 역할(후속 curate 단계 입력)을 하는
    candidate 리스트. 콘텐츠 기반 chunk_hash로 결정적.

    chunk_hash는 공백을 정규화한 텍스트로 계산한다(LLM에 넘기는 실제 콘텐츠는
    그대로 둠) — 들여쓰기/trailing space만 바뀐 재실행은 dedupe.py가 "콘텐츠
    불변"으로 보고 skip해, 의미 없는 포맷팅 변경마다 전체 재큐레이션이 일어나는
    걸 막는다."""
    candidates = []
    for c in chunks:
        normalized = " ".join(c["text"].split())
        chunk_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
        candidates.append({
            "type": "doc_chunk",
            "doc_path": doc_path,
            "doc_hash": doc_hash,
            "chunk_index": c["chunk_index"],
            "heading_path": c["heading_path"],
            "text": c["text"],
            "chunk_hash": chunk_hash,
        })
    return candidates
