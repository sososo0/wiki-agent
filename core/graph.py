"""
wiki-agent / core / graph.py

위키 엔트리들을 노드/엣지 그래프로 변환하는 읽기 전용 파생 뷰(derived view). DB에
아무것도 쓰지 않는다 — wiki_store가 제공하는 read primitives(list_active_entries 등)
를 조합만 한다(core/retrieval.py와 동일한 역할 분담: wiki_store=DB 접근,
graph=순수 변환 로직, embed_fn 주입 가능해 pytest로 단독 검증).

[엣지 설계]
- supersedes 컬럼은 promote.py가 승격 시 "이 shadow가 어떤 active를 대체하는지"로
  쓰고, 강등 후(deprecated)에도 지우지 않는다(wiki_store.set_entry_status는 status만
  바꿈) — 같은 컬럼을 source 노드의 현재 status로 구분해 재해석한다:
  shadow -> "pending_update"(승격 대기 중), deprecated -> "superseded_by"(과거 이력).
- similar 엣지는 default_embed_fn(정규화된 임베딩, dot product = cosine similarity,
  retrieval.py의 _dense_rank와 동일 전제)으로 모든 노드 텍스트를 인코딩해 계산한다.
  노드별 top-k 중 threshold 이상만 남기고, 무방향 페어 키로 중복을 제거한다(A의
  top-k에 B가, B의 top-k에 A가 동시에 들 수 있어 union 효과로 노드당 정확히
  top_k_similar개가 아니라 그 이상일 수 있음 — 의도된 동작).
- 코퍼스가 작아 임베딩 캐시는 두지 않는다(retrieval.py와 동일한 트레이드오프).
"""

from typing import Any, Callable, Dict, List, Optional

import numpy as np

try:
    from core import retrieval, wiki_store
except ImportError:        # python core/graph.py 로 직접 실행할 때
    import retrieval
    import wiki_store


def _to_node(entry: Dict[str, Any], status: str) -> Dict[str, Any]:
    rejected_reason = None
    if status == "rejected":
        sources = entry.get("sources") or []
        if sources:
            rejected_reason = sources[0].get("rejected_reason")
    return {
        "id": entry["entry_id"],
        "topic": entry.get("topic"),
        "canonical": entry.get("canonical"),
        "body_md": entry.get("body_md"),
        "status": status,
        "provenance": entry.get("provenance"),
        "confidence": entry.get("confidence"),
        "version": entry.get("version"),
        "supersedes": entry.get("supersedes"),
        "rejected_reason": rejected_reason,
    }


def build_graph(
    *,
    embed_fn: Optional[Callable] = None,
    similarity_threshold: float = 0.3,
    top_k_similar: int = 2,
    include_deprecated: bool = True,
    include_rejected: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """active+shadow(+deprecated/rejected) 엔트리를 {"nodes": [...], "edges": [...]}로.

    엔트리가 없으면 embed_fn을 호출하지 않고 즉시 빈 그래프를 반환한다(임베딩
    모델이 로드되지 않은 상태에서도 호출 가능해야 함)."""
    rows_by_status = [
        ("active", wiki_store.list_active_entries()),
        ("shadow", wiki_store.list_shadow_entries()),
    ]
    if include_deprecated:
        rows_by_status.append(("deprecated", wiki_store.list_deprecated_entries()))
    if include_rejected:
        rows_by_status.append(("rejected", wiki_store.list_rejected_entries()))

    nodes = [
        _to_node(row, status)
        for status, rows in rows_by_status
        for row in rows
    ]
    if not nodes:
        return {"nodes": [], "edges": []}

    node_ids = {n["id"] for n in nodes}
    edges: List[Dict[str, Any]] = []
    seen_structural = set()

    for n in nodes:
        supersedes = n.get("supersedes")
        if not supersedes or supersedes not in node_ids or supersedes == n["id"]:
            continue
        if n["status"] == "shadow":
            edge_type = "pending_update"
        elif n["status"] == "deprecated":
            edge_type = "superseded_by"
        else:
            continue
        key = (n["id"], supersedes, edge_type)
        if key in seen_structural:
            continue
        seen_structural.add(key)
        edges.append({
            "source": n["id"], "target": supersedes, "type": edge_type, "weight": 1.0,
        })

    if len(nodes) >= 2:
        embed_fn = embed_fn or retrieval.default_embed_fn
        texts = [retrieval._entry_text(n) for n in nodes]
        vecs = np.asarray(embed_fn(texts))
        sims = vecs @ vecs.T
        np.fill_diagonal(sims, -1.0)

        seen_undirected = set()
        for i, n in enumerate(nodes):
            order = np.argsort(-sims[i])[:top_k_similar]
            for j in order:
                if sims[i, j] < similarity_threshold:
                    continue
                a, b = n["id"], nodes[j]["id"]
                pair_key = tuple(sorted((a, b)))
                if pair_key in seen_undirected:
                    continue
                seen_undirected.add(pair_key)
                edges.append({
                    "source": a, "target": b, "type": "similar",
                    "weight": round(float(sims[i, j]), 4),
                })

    return {"nodes": nodes, "edges": edges}
