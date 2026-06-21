"""
wiki-agent / core / pipeline / curate.py

mine.py가 찾은 gap 후보를 위키 엔트리 patch(JSON)로 만든다. LLM은 gap의
query_examples(실제 retrieval_log에 남은 질문 원문)만 보고 생성하므로
patch는 항상 provenance="curated_from_logs" + sources에 그 질문들을 그대로
첨부한다(거짓 출처를 만들지 않음). llm_fn을 주입하면 실제 모델 호출 없이
테스트 가능 — core/는 eval/을 import하지 않고 같은 호출 패턴만 재사용한다.
"""

import hashlib
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

CURATE_MODEL = os.environ.get("WIKI_AGENT_CURATE_MODEL", "claude-haiku-4-5")

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


VALID_TIERS = ("basics", "intermediate", "advanced")


def default_llm_fn(query_examples, model=CURATE_MODEL) -> Dict[str, str]:
    """실제 Anthropic 호출로 {"topic", "canonical", "body_md", "tier"} JSON을 생성.

    tier는 문서 ingestion처럼 파일명에서 추론할 신호가 없는 로그 마이닝 경로의
    유일한 분류 수단이라, LLM이 질문의 성격을 보고 직접 분류하게 한다(curate()가
    유효하지 않은 값은 advanced로 폴백)."""
    prompt = (
        "Users repeatedly asked questions that our knowledge base could not "
        "answer well. Based ONLY on the question text below (no other "
        "knowledge), draft a short wiki entry that would help answer them. "
        "Keep body_md under 80 words, plain prose, no markdown code fences "
        "inside it. Also classify the difficulty tier of the question: "
        "\"basics\" (asking for a basic definition), \"intermediate\" "
        "(asking how to apply/configure something in practice), or "
        "\"advanced\" (asking about deep internals or edge cases). "
        "Reply with JSON only, no other text: "
        '{"topic": "...", "canonical": "one sentence summary", "body_md": "...", '
        '"tier": "basics|intermediate|advanced"}\n\n'
        "Questions:\n" + "\n".join(f"- {q}" for q in query_examples)
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(_extract_json_object(text))


def _extract_json_object(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '{'~마지막 '}' 사이만 추출."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:40] or "gap"


def gap_entry_id(norm_query: str) -> str:
    """norm_query -> 결정적 entry_id. LLM 호출과 무관하게 계산 가능 — curate()가
    LLM을 부르기 전에 호출부(run_update_cycle.py)가 이미 게이트 거부된 적이
    있는 gap인지 먼저 확인할 수 있어야 하므로 별도 공개 함수로 분리한다
    (doc_chunk_entry_id와 동일한 목적)."""
    slug = _slugify(norm_query)
    digest = hashlib.sha1(norm_query.encode("utf-8")).hexdigest()[:8]
    return f"wiki_gap_{slug}_{digest}"


def rejected_gap_entry_id(norm_query: str) -> str:
    """게이트가 거부한 gap을 norm_query 단위로 기억하는 전용 entry_id
    (dedupe.rejected_entry_id와 동일한 목적 — 문서 ingestion은 chunk_hash로
    "콘텐츠 불변"을 판단하지만, gap은 콘텐츠가 질문 문구 자체이므로 norm_query가
    그 역할을 한다). base gap entry_id와 네임스페이스가 겹치지 않아 나중에 그
    질문이 진짜 active/shadow가 될 자리를 침범하지 않는다."""
    return f"{gap_entry_id(norm_query)}_rej"


def curate(
    gap: Dict[str, Any],
    *,
    llm_fn: Optional[Callable] = None,
    model: str = CURATE_MODEL,
) -> Dict[str, Any]:
    """gap -> patch dict. llm_fn(query_examples) -> {topic, canonical, body_md} 형태.

    JSON 파싱 실패 등은 그대로 예외를 던져 호출부(run_update_cycle)가 이 후보를
    skip하도록 한다(가짜 patch를 만들지 않음)."""
    llm_fn = llm_fn or (lambda qs: default_llm_fn(qs, model=model))
    drafted = llm_fn(gap["query_examples"])

    entry_id = gap_entry_id(gap["norm_query"])

    # LLM이 tier를 빼먹거나 오타를 내도(예: "Advanced") 파이프라인이 멈추지 않게
    # 유효한 3개 값 중 하나가 아니면 advanced로 폴백 — 실제 운영 중 반복된
    # 구체적 질문이라는 gap의 특성상 advanced가 가장 안전한 기본값.
    tier = drafted.get("tier")
    if tier not in VALID_TIERS:
        tier = "advanced"

    return {
        "op": "create",
        "entry_id": entry_id,
        "topic": drafted["topic"],
        "canonical": drafted["canonical"],
        "body_md": drafted.get("body_md", ""),
        "provenance": "curated_from_logs",
        "confidence": 0.5,
        "tier": tier,
        "sources": [
            {"type": "retrieval_log_query", "query": q, "verified": False}
            for q in gap.get("query_occurrences", gap["query_examples"])
        ],
        "reason": (
            f"freq={gap['freq']} avg_top_score={gap['avg_top_score']:.3f} "
            f"< threshold"
        ),
    }


def default_doc_llm_fn(heading_path, text, model=CURATE_MODEL) -> Dict[str, str]:
    """실제 Anthropic 호출로 문서 청크 -> {"topic", "canonical", "body_md"} JSON.

    질문에서 답을 추론하는 default_llm_fn과 달리, 이미 쓰여진 본문을 요약/정제만
    한다(출처가 사람이 작성한 문서 자체이므로 새 사실을 지어내지 않도록 지시)."""
    heading = " > ".join(heading_path) if heading_path else "(no heading)"
    prompt = (
        "You are curating a wiki entry from an existing verified document. "
        "Summarize and restructure the section below WITHOUT adding any fact "
        "not present in it. Keep body_md under 80 words, plain prose, no "
        "markdown code fences inside it. Reply with JSON only, no other text: "
        '{"topic": "...", "canonical": "one sentence summary", "body_md": "..."}\n\n'
        f"Section heading: {heading}\n\nSection text:\n{text}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text_out = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(_extract_json_object(text_out))


def infer_doc_tier(doc_path: str) -> str:
    """문서 ingestion 경로는 scripts/generate_corpus.py가 정한 파일명 접두사로
    난이도를 결정적으로 알 수 있다(basics_/intermediate_, 그 외는 advanced) —
    로그 마이닝 경로(curate())처럼 LLM 분류가 필요 없다."""
    fname = doc_path.rsplit("/", 1)[-1]
    if fname.startswith("basics_"):
        return "basics"
    if fname.startswith("intermediate_"):
        return "intermediate"
    return "advanced"


def doc_chunk_entry_id(candidate: Dict[str, Any]) -> str:
    """candidate(doc_path + chunk_index) -> 결정적 entry_id. LLM 호출과 무관하게
    계산 가능 — dedupe.py가 curate_doc_chunk()를 호출(=LLM 비용 발생)하기 전에
    이미 같은 entry_id가 존재하는지 먼저 확인할 수 있어야 하므로 별도 공개 함수로
    분리한다(같은 파일의 같은 섹션 위치는 항상 같은 entry_id)."""
    heading_slug = _slugify(" ".join(candidate["heading_path"]) or candidate["doc_path"])
    digest = hashlib.sha1(
        f"{candidate['doc_path']}::{candidate['chunk_index']}".encode("utf-8")
    ).hexdigest()[:8]
    return f"wiki_doc_{heading_slug}_{digest}"


def curate_doc_chunk(
    candidate: Dict[str, Any],
    *,
    llm_fn: Optional[Callable] = None,
    model: str = CURATE_MODEL,
) -> Dict[str, Any]:
    """문서 청크 candidate(chunk.to_doc_candidates 출력) -> patch dict.

    curate()와 출력 모양(op/entry_id/topic/canonical/body_md/provenance/
    confidence/sources/reason)은 동일하지만, provenance="doc_verified"(사람이
    작성한 실재 문서 본문이 출처이므로 curated_from_logs보다 신뢰도가 높음)이고
    sources는 단일 문서 출처 1건이다(질문 다건이 아니므로 query 필드가 없음).
    llm_fn(heading_path, text) -> {topic, canonical, body_md} 형태로 주입 가능."""
    llm_fn = llm_fn or (lambda hp, t: default_doc_llm_fn(hp, t, model=model))
    drafted = llm_fn(candidate["heading_path"], candidate["text"])

    entry_id = doc_chunk_entry_id(candidate)

    return {
        "op": "create",
        "entry_id": entry_id,
        "topic": drafted["topic"],
        "canonical": drafted["canonical"],
        "body_md": drafted.get("body_md", ""),
        "provenance": "doc_verified",
        "confidence": 0.9,
        "tier": infer_doc_tier(candidate["doc_path"]),
        "sources": [{
            "type": "document",
            "path": candidate["doc_path"],
            "heading_path": candidate["heading_path"],
            "chunk_hash": candidate["chunk_hash"],
            "verified": True,
        }],
        "reason": f"doc_path={candidate['doc_path']} chunk_index={candidate['chunk_index']}",
    }


def default_doc_judge_fn(
    patch: Dict[str, Any], chunk_text: str, model: str = CURATE_MODEL,
) -> Tuple[float, str]:
    """gate.default_judge_fn과 동일한 (score, reason) 0~1 grounding 계약을 따르지만,
    그쪽은 `s.get('query', s)`로 source를 텍스트화해서 문서 출처(query 필드 없음)에서는
    source dict를 그대로 stringify해 judge에 넘기는 문제가 있다(실제 청크 본문을
    한 번도 보지 못함) — gate.py는 무수정 대상이라, 여기서 원본 chunk_text를
    직접 프롬프트에 넣는 문서 전용 judge를 만들어 ingest_doc.py가 주입한다.
    gate.passes_gate가 `score, reason = judge_fn(patch, existing_entries)`로
    호출하므로 반환 타입을 그 계약과 동일하게 맞춘다."""
    prompt = (
        "You are reviewing a candidate knowledge-base entry before it is "
        "merged. Judge whether the entry content is grounded in the source "
        "document text below (no fabricated facts, no internal "
        "contradiction). Reply with a single number between 0 and 1 "
        "(1 = fully grounded, 0 = fabricated/contradictory), nothing else.\n\n"
        f"Entry topic: {patch.get('topic')}\n"
        f"Entry canonical: {patch.get('canonical')}\n"
        f"Entry body: {patch.get('body_md')}\n\n"
        f"Source document text:\n{chunk_text}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "0")
    match = re.search(r"[01](?:\.\d+)?", text)
    score = float(match.group()) if match else 0.0
    return score, f"doc grounding score={score}"


def make_doc_judge_fn(
    chunk_text_by_entry_id: Dict[str, str],
    *,
    model: str = CURATE_MODEL,
) -> Callable[[Dict[str, Any], List[Dict[str, Any]]], Tuple[float, str]]:
    """entry_id -> 원본 chunk 텍스트 매핑(클로저로 참조, ingest_doc.py가 candidate를
    돌면서 채움)을 들고 있는 judge_fn을 만들어 반환. gate.passes_gate(judge_fn=...)는
    `judge_fn(patch, existing_entries)`로 호출하고 `(score, reason)`을 기대하므로
    그 시그니처에 맞춘다 — existing_entries는 문서 grounding 판단에 쓰지 않지만
    (청크 본문 자체가 출처이므로) 호출 계약상 받아야 한다."""
    def _judge(patch: Dict[str, Any], existing_entries: List[Dict[str, Any]]) -> Tuple[float, str]:
        chunk_text = chunk_text_by_entry_id.get(patch.get("entry_id"), "")
        return default_doc_judge_fn(patch, chunk_text, model=model)
    return _judge
