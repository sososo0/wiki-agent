"""
wiki-agent / core / pipeline / promote.py

"롤백은 애초에 커밋하지 않음으로 구현한다." search_wiki()는 호출마다 새
sqlite 커넥션을 열어 공유 트랜잭션으로 "가상 승격 상태"를 보여줄 수 없으므로,
먼저 active+shadow를 메모리에서 합친 candidate 엔트리 리스트로
retrieval.hybrid_search를 직접 호출해 평가하고, 회귀가 없을 때만 실제
DB에 커밋한다(shadow -> active, supersedes 대상은 병합 후 candidate 행을
deprecated로 강등). 회귀가 있으면 아무 것도 쓰지 않는다.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import retrieval, wiki_store


def _approx_bm25_rank(query: str, entries: List[Dict[str, Any]], limit: int) -> List[str]:
    """영속 FTS 인덱스 없는 가상 엔트리용 키워드 카운트 BM25 근사."""
    q_tokens = set(re.findall(r"[0-9A-Za-z가-힣]+", query.lower()))
    scored = []
    for e in entries:
        text = f"{e.get('topic', '')} {e.get('canonical', '')} {e.get('body_md') or ''}".lower()
        e_tokens = set(re.findall(r"[0-9A-Za-z가-힣]+", text))
        scored.append((len(q_tokens & e_tokens), e["entry_id"]))
    scored.sort(key=lambda x: -x[0])
    return [eid for _, eid in scored[:limit]]


def _merge_active_and_shadow() -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """active 엔트리 + (supersedes 병합/신규) shadow를 합친 가상 엔트리 리스트를 만든다.

    반환: (merged_entries, activated_entry_ids, shadow_rows)
    """
    active = wiki_store.list_active_entries()
    shadow = wiki_store.list_shadow_entries()

    merged: Dict[str, Dict[str, Any]] = {e["entry_id"]: e for e in active}
    activated_ids = []
    for s in shadow:
        target_id = s.get("supersedes") or s["entry_id"]
        merged[target_id] = {
            "entry_id": target_id,
            "topic": s["topic"],
            "canonical": s["canonical"],
            "body_md": s.get("body_md"),
            "confidence": s.get("confidence", 1.0),
            "provenance": s.get("provenance"),
            "sources": s.get("sources"),
        }
        activated_ids.append(target_id)
    return list(merged.values()), activated_ids, shadow


def simulate_candidate_retriever(
    *, embed_fn: Optional[Callable] = None, rerank_fn: Optional[Callable] = None
) -> Callable:
    """active+shadow를 합친 가상 상태로 검색하는 retriever 클로저(DB 쓰기 없음)."""
    entries, _, _ = _merge_active_and_shadow()

    def _retrieve(query: str, k: int = 5) -> List[Dict[str, Any]]:
        fetch_k = max(k * 4, 20)
        bm25_ids = _approx_bm25_rank(query, entries, fetch_k)
        return retrieval.hybrid_search(
            query, entries, bm25_ids, k=k, fetch_k=fetch_k,
            embed_fn=embed_fn, rerank_fn=rerank_fn,
        )

    return _retrieve


def promote_if_better(gold: List[Dict[str, Any]], k: int = 5, *, evaluate_fn: Callable) -> Dict[str, Any]:
    """base(실제 active) vs candidate(active+shadow 시뮬레이션) 비교.

    회귀(recall@k 또는 correctness 하락) 없으면 실제 커밋, 있으면 아무 것도 쓰지 않는다.
    """
    base = evaluate_fn(wiki_store.search_wiki, gold, k=k)
    candidate_retriever = simulate_candidate_retriever()
    candidate = evaluate_fn(candidate_retriever, gold, k=k)

    regressed = (
        candidate["recall@k"] < base["recall@k"]
        or candidate["correctness"] < base["correctness"]
    )
    if regressed:
        return {"base": base, "candidate": candidate, "promoted": False, "activated_entry_ids": []}

    _, activated_ids, shadow = _merge_active_and_shadow()
    for s in shadow:
        target_id = s.get("supersedes") or s["entry_id"]
        wiki_store.add_entry(
            target_id, s["topic"], s["canonical"], s.get("body_md"),
            status="active", provenance=s.get("provenance"),
            confidence=s.get("confidence", 1.0), sources=s.get("sources"),
        )
        if s.get("supersedes"):
            wiki_store.set_entry_status(s["entry_id"], "deprecated")

    return {"base": base, "candidate": candidate, "promoted": True, "activated_entry_ids": activated_ids}
