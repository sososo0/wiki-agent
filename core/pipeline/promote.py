"""
wiki-agent / core / pipeline / promote.py

"롤백은 애초에 커밋하지 않음으로 구현한다." search_wiki()는 호출마다 새
sqlite 커넥션을 열어 공유 트랜잭션으로 "가상 승격 상태"를 보여줄 수 없으므로,
먼저 active+shadow를 메모리에서 합친 candidate 엔트리 리스트로
retrieval.hybrid_search를 직접 호출해 평가하고, 회귀가 없을 때만 실제
DB에 커밋한다(shadow -> active, supersedes 대상은 병합 후 candidate 행을
deprecated로 강등). 회귀가 있으면 아무 것도 쓰지 않는다.

골드셋 회귀 체크만으로는 새로 mine된 gap이 실제로 메워졌는지 알 수 없다(골드셋엔
새 gap 주제의 문항이 없으므로). evaluate_gap_recall은 가짜 gold_answer를 만들지
않고, 각 shadow 엔트리를 만든 계기였던 source 질문들을 그대로 다시 검색해 그
엔트리가 실제로 잡히는지만 본다 — promote_if_better가 이 결과도 회귀 조건에 포함.
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
            "tier": s.get("tier"),
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


def evaluate_gap_recall(
    shadow_rows: List[Dict[str, Any]], candidate_retriever: Callable, k: int = 5
) -> Dict[str, Any]:
    """각 shadow 엔트리가 자신을 만든 계기였던 source 질문들에 대해 실제로
    검색되는지 확인 — 고정 골드셋이 모르는 신규 gap 주제의 개선 여부를
    가짜 gold_answer 없이 검증하는 유일한 방법(순수 retrieval 신호)."""
    per_entry = []
    for s in shadow_rows:
        target_id = s.get("supersedes") or s["entry_id"]
        queries = [src["query"] for src in (s.get("sources") or []) if src.get("query")]
        if not queries:
            continue
        hits = sum(
            1 for q in queries
            if target_id in [h["entry_id"] for h in candidate_retriever(q, k)]
        )
        per_entry.append({
            "entry_id": target_id,
            "gap_recall": hits / len(queries),
            "n_queries": len(queries),
        })
    mean_gap_recall = (
        sum(e["gap_recall"] for e in per_entry) / len(per_entry) if per_entry else 1.0
    )
    return {"mean_gap_recall": mean_gap_recall, "per_entry": per_entry}


def promote_if_better(
    gold: List[Dict[str, Any]], k: int = 5, *,
    evaluate_fn: Callable, gap_recall_threshold: float = 0.6,
) -> Dict[str, Any]:
    """base(실제 active) vs candidate(active+shadow 시뮬레이션) 비교.

    recall@k/correctness가 골드셋 전체 기준으로 떨어지면(gold_set_regressed)
    그 회귀를 특정 entry 탓이라고 추가 비용(entry별 재평가) 없이 확신할 수
    없으므로 안전하게 전체를 막는다(기존 all-or-nothing 동작 그대로).

    골드셋 자체는 안 떨어졌는데 mean_gap_recall만 임계치 미달인 경우는 다르다 —
    evaluate_gap_recall이 이미 entry별 gap_recall을 공짜로 계산해주므로, 자기
    출처 질문도 못 잡는 entry만 걸러내고 나머지는 승격한다(부분 승격). 한
    사이클에 쌓인 shadow 중 단 하나가 나쁘다고 나머지 좋은 후보까지 영원히
    막히던 문제를 줄인다.
    """
    base = evaluate_fn(wiki_store.search_wiki, gold, k=k)
    candidate_retriever = simulate_candidate_retriever()
    candidate = evaluate_fn(candidate_retriever, gold, k=k)

    _, _, shadow = _merge_active_and_shadow()
    gap_recall = evaluate_gap_recall(shadow, candidate_retriever, k=k)

    gold_set_regressed = (
        candidate["recall@k"] < base["recall@k"]
        or candidate["correctness"] < base["correctness"]
    )
    if gold_set_regressed:
        return {
            "base": base, "candidate": candidate, "gap_recall": gap_recall,
            "promoted": False, "activated_entry_ids": [], "skipped_entry_ids": [],
        }

    bad_ids = {
        e["entry_id"] for e in gap_recall["per_entry"] if e["gap_recall"] < gap_recall_threshold
    }
    promotable = [s for s in shadow if (s.get("supersedes") or s["entry_id"]) not in bad_ids]
    skipped_ids = [
        s.get("supersedes") or s["entry_id"] for s in shadow
        if (s.get("supersedes") or s["entry_id"]) in bad_ids
    ]

    activated: List[str] = []
    for s in promotable:
        target_id = s.get("supersedes") or s["entry_id"]
        wiki_store.add_entry(
            target_id, s["topic"], s["canonical"], s.get("body_md"),
            status="active", provenance=s.get("provenance"),
            confidence=s.get("confidence", 1.0), sources=s.get("sources"),
            tier=s.get("tier"),
        )
        if s.get("supersedes"):
            wiki_store.set_entry_status(s["entry_id"], "deprecated")
        activated.append(target_id)

    return {
        "base": base, "candidate": candidate, "gap_recall": gap_recall,
        "promoted": bool(activated), "activated_entry_ids": activated,
        "skipped_entry_ids": skipped_ids,
    }
