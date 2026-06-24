"""
wiki-agent / core / pipeline / gate.py

오염 게이트. patch가 shadow로라도 DB에 들어가기 전 마지막 검문소. CLAUDE.md HARD
CONSTRAINT를 코드로 강제한다: agent_generated 단독(미검증 source) 승격/반영 금지 —
curated_from_web도 LLM이 스스로 근거를 대는 같은 위험군이라 동일 적용. 비용이 드는
체크(LLM grounding)는 마지막에 돌려 싸고 결정적인 체크부터 빨리 떨어뜨린다.

grounding 체크(default_judge_fn)는 "source 질문과 그럴듯하게 들어맞는가" 같은
막연한 기준이 아니라 자기모순/기존 active 엔트리와의 직접 모순/환각 3가지 binary
신호를 판정한다 — source는 사용자 질문 원문일 뿐 사실을 담지 않으므로, 검증
가능한 신호로 좁힌 것.
"""

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

JUDGE_MODEL = os.environ.get("WIKI_AGENT_GATE_JUDGE_MODEL", "claude-haiku-4-5")

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _extract_json_object(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '{'~마지막 '}' 사이만 추출."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def default_judge_fn(
    patch: Dict[str, Any],
    existing_entries: List[Dict[str, Any]],
    model: str = JUDGE_MODEL,
) -> Tuple[float, str]:
    """LLM-as-judge: 자기모순/기존 검증된 active 엔트리와의 모순/환각 3가지를 직접
    binary로 판정한다. 하나라도 true면 score=0.0, 전부 false면 score=1.0.
    (score, 설명) 반환."""
    existing_text = "\n".join(
        f"- [{e.get('entry_id')}] {e.get('topic')}: {e.get('canonical')}"
        for e in existing_entries
    ) or "(none)"
    prompt = (
        "You are reviewing a candidate knowledge-base entry before it is "
        "merged. Check exactly three things and reply with JSON only, no "
        "other text:\n"
        '{"self_contradictory": bool, "contradicts_existing": bool, '
        '"contradicting_entry_id": string|null, "hallucination_risk": bool, '
        '"explanation": "..."}\n\n'
        "- self_contradictory: does the entry's body contradict its own "
        "canonical summary?\n"
        "- contradicts_existing: does the entry directly contradict any of "
        "the existing verified entries below? If true, set "
        "contradicting_entry_id to that entry's id.\n"
        "- hallucination_risk: does the entry state suspiciously specific "
        "but unverifiable facts (invented version numbers, library/API "
        "names, statistics) that are not general, well-established "
        "knowledge?\n\n"
        f"Candidate entry topic: {patch.get('topic')}\n"
        f"Candidate entry canonical: {patch.get('canonical')}\n"
        f"Candidate entry body: {patch.get('body_md')}\n\n"
        f"Existing verified entries:\n{existing_text}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        parsed = json.loads(_extract_json_object(text))
    except (json.JSONDecodeError, ValueError):
        return 0.0, "judge response was not valid JSON"

    flagged = (
        parsed.get("self_contradictory")
        or parsed.get("contradicts_existing")
        or parsed.get("hallucination_risk")
    )
    explanation = parsed.get("explanation", "")
    if parsed.get("contradicts_existing") and parsed.get("contradicting_entry_id"):
        explanation = f"contradicts {parsed['contradicting_entry_id']}: {explanation}"
    return (0.0 if flagged else 1.0), (explanation or "ok")


def _tokens(text: str) -> set:
    return set((text or "").lower().split())


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def passes_gate(
    patch: Dict[str, Any],
    today_writes: int,
    *,
    existing_entries: List[Dict[str, Any]],
    pending_shadow_entries: Optional[List[Dict[str, Any]]] = None,
    daily_cap: int = 20,
    min_sources: int = 2,
    dup_threshold: float = 0.6,
    grounding_threshold: float = 0.7,
    judge_fn: Optional[Callable] = None,
) -> Tuple[bool, str]:
    """patch가 shadow로 들어가도 되는지 5단계로 검사. (통과여부, 이유) 반환.

    pending_shadow_entries: 이번/이전 사이클에서 이미 shadow로 쓴 후보들. 자카드
    중복 체크(4단계)에서만 existing_entries와 합쳐서 본다 — judge_fn(5단계)에는
    절대 안 넘긴다(judge 프롬프트는 "검증된 active 엔트리와의 모순"만 보도록
    설계돼 있어 깨짐 방지). 안 주면(기본 None) active만 보고 중복 체크하므로,
    같은 gap이 사이클마다 비슷한 shadow를 계속 쌓을 수 있던 문제가 있었다."""
    # 1. provenance 규칙 (★ HARD CONSTRAINT: LLM이 스스로 근거를 대는 provenance
    # — agent_generated, curated_from_web — 단독(미검증 source) 승격 금지)
    if patch.get("provenance") in ("agent_generated", "curated_from_web"):
        if not any(s.get("verified") for s in patch.get("sources", [])):
            return False, f"{patch.get('provenance')} requires a verified source"

    # 2. 일일 신규 shadow 상한
    if today_writes >= daily_cap:
        return False, "daily_cap exceeded"

    # 3. 신규 엔트리 출처 다양성
    if patch.get("op") == "create" and len(patch.get("sources", [])) < min_sources:
        return False, "insufficient source diversity"

    # 4. 기존 active + 이미 쌓인 shadow 후보와 근접 중복(자카드, 결정론적)
    patch_text = f"{patch.get('topic', '')} {patch.get('canonical', '')}"
    for entry in existing_entries + (pending_shadow_entries or []):
        existing_text = f"{entry.get('topic', '')} {entry.get('canonical', '')}"
        if _jaccard(patch_text, existing_text) >= dup_threshold:
            return False, "near-duplicate of existing entry"

    # 5. 자기모순/기존 검증 엔트리와의 모순/환각 LLM-judge (가장 비용이 드는 체크, 마지막)
    judge_fn = judge_fn or default_judge_fn
    score, judge_reason = judge_fn(patch, existing_entries)
    if score < grounding_threshold:
        return False, f"failed grounding/contradiction check: {judge_reason}"

    return True, "ok"
