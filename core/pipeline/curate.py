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
from typing import Any, Callable, Dict, Optional

CURATE_MODEL = os.environ.get("WIKI_AGENT_CURATE_MODEL", "claude-haiku-4-5")

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def default_llm_fn(query_examples, model=CURATE_MODEL) -> Dict[str, str]:
    """실제 Anthropic 호출로 {"topic", "canonical", "body_md"} JSON을 생성."""
    prompt = (
        "Users repeatedly asked questions that our knowledge base could not "
        "answer well. Based ONLY on the question text below (no other "
        "knowledge), draft a short wiki entry that would help answer them. "
        "Keep body_md under 80 words, plain prose, no markdown code fences "
        "inside it. Reply with JSON only, no other text: "
        '{"topic": "...", "canonical": "one sentence summary", "body_md": "..."}\n\n'
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

    slug = _slugify(gap["norm_query"])
    digest = hashlib.sha1(gap["norm_query"].encode("utf-8")).hexdigest()[:8]
    entry_id = f"wiki_gap_{slug}_{digest}"

    return {
        "op": "create",
        "entry_id": entry_id,
        "topic": drafted["topic"],
        "canonical": drafted["canonical"],
        "body_md": drafted.get("body_md", ""),
        "provenance": "curated_from_logs",
        "confidence": 0.5,
        "sources": [
            {"type": "retrieval_log_query", "query": q, "verified": False}
            for q in gap.get("query_occurrences", gap["query_examples"])
        ],
        "reason": (
            f"freq={gap['freq']} avg_top_score={gap['avg_top_score']:.3f} "
            f"< threshold"
        ),
    }
