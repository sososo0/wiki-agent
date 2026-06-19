"""
wiki-agent / core / pipeline / gate.py

오염 게이트. patch가 shadow로라도 DB에 들어가기 전 마지막 검문소.
CLAUDE.md HARD CONSTRAINT를 코드로 강제한다: agent_generated 단독(미검증
source) 승격/반영 금지. 비용이 드는 체크(LLM grounding)는 가장 마지막에
돌려 싸고 결정적인 체크부터 빨리 떨어뜨린다(daily_cap, source 다양성,
자카드 중복은 전부 네트워크 없이 결정론적).
"""

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


def default_judge_fn(patch: Dict[str, Any], model: str = JUDGE_MODEL) -> float:
    """LLM-as-judge: patch 내용이 sources의 질문/근거와 모순 없이 근거되어 있으면
    1.0에 가깝게, 지어낸 내용이면 0에 가깝게 0~1 점수를 반환."""
    sources_text = "\n".join(
        f"- {s.get('query', s)}" for s in patch.get("sources", [])
    )
    prompt = (
        "You are reviewing a candidate knowledge-base entry before it is "
        "merged. Judge whether the entry content is plausibly grounded in "
        "the source questions (no fabricated facts, no internal "
        "contradiction). Reply with a single number between 0 and 1 "
        "(1 = fully grounded, 0 = fabricated/contradictory), nothing else.\n\n"
        f"Entry topic: {patch.get('topic')}\n"
        f"Entry canonical: {patch.get('canonical')}\n"
        f"Entry body: {patch.get('body_md')}\n\n"
        f"Source questions:\n{sources_text}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "0")
    match = re.search(r"[01](?:\.\d+)?", text)
    return float(match.group()) if match else 0.0


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
    daily_cap: int = 20,
    min_sources: int = 2,
    dup_threshold: float = 0.6,
    grounding_threshold: float = 0.7,
    judge_fn: Optional[Callable] = None,
) -> Tuple[bool, str]:
    """patch가 shadow로 들어가도 되는지 5단계로 검사. (통과여부, 이유) 반환."""
    # 1. provenance 규칙 (★ HARD CONSTRAINT: agent_generated 단독 승격 금지)
    if patch.get("provenance") == "agent_generated":
        if not any(s.get("verified") for s in patch.get("sources", [])):
            return False, "agent_generated requires a verified source"

    # 2. 일일 신규 shadow 상한
    if today_writes >= daily_cap:
        return False, "daily_cap exceeded"

    # 3. 신규 엔트리 출처 다양성
    if patch.get("op") == "create" and len(patch.get("sources", [])) < min_sources:
        return False, "insufficient source diversity"

    # 4. 기존 active 엔트리와 근접 중복(자카드, 결정론적)
    patch_text = f"{patch.get('topic', '')} {patch.get('canonical', '')}"
    for entry in existing_entries:
        existing_text = f"{entry.get('topic', '')} {entry.get('canonical', '')}"
        if _jaccard(patch_text, existing_text) >= dup_threshold:
            return False, "near-duplicate of existing entry"

    # 5. grounding/모순 LLM-judge (가장 비용이 드는 체크, 마지막)
    judge_fn = judge_fn or default_judge_fn
    if judge_fn(patch) < grounding_threshold:
        return False, "failed grounding/contradiction check"

    return True, "ok"
