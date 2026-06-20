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
- 코퍼스가 작을 때는 매 호출마다 전체 재인코딩해도 무시할 비용이었지만, 코퍼스가
  커지면(1000+ 노드) 호출당 인코딩이 체감 지연이 된다. retrieval.py/wiki_store.py에
  영속 임베딩 컬럼을 추가하는 건 더 큰 변경이라 여기서는 하지 않고, 호출부가
  원하면 entry_id+version 키의 dict를 `cache` 인자로 넘겨 프로세스 생애 동안
  재사용할 수 있게만 한다(인자를 안 주면 매 호출 새 dict라 기존 동작과 동일 —
  테스트 격리에 영향 없음).
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

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


def _get_node_vectors(
    nodes: List[Dict[str, Any]],
    embed_fn: Callable,
    cache: Dict[str, Tuple[Optional[int], np.ndarray]],
) -> np.ndarray:
    """entry_id+version으로 캐시 적중하는 노드는 재인코딩을 건너뛴다.

    version은 promote/set_entry_status가 status만 바꿀 때는 올리지 않으므로
    (wiki_store.set_entry_status), 같은 콘텐츠가 active<->shadow 등으로 상태만
    바뀌어도 캐시가 그대로 유효하다."""
    vecs: List[Optional[np.ndarray]] = [None] * len(nodes)
    miss_idx: List[int] = []
    miss_texts: List[str] = []

    for i, n in enumerate(nodes):
        hit = cache.get(n["id"])
        if hit is not None and hit[0] == n.get("version"):
            vecs[i] = hit[1]
        else:
            miss_idx.append(i)
            miss_texts.append(retrieval._entry_text(n))

    if miss_texts:
        new_vecs = np.asarray(embed_fn(miss_texts))
        for k, i in enumerate(miss_idx):
            vecs[i] = new_vecs[k]
            cache[nodes[i]["id"]] = (nodes[i].get("version"), new_vecs[k])

    return np.asarray(vecs)


def build_graph(
    *,
    embed_fn: Optional[Callable] = None,
    similarity_threshold: float = 0.3,
    top_k_similar: int = 2,
    include_deprecated: bool = True,
    include_rejected: bool = True,
    cache: Optional[Dict[str, Tuple[Optional[int], np.ndarray]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """active+shadow(+deprecated/rejected) 엔트리를 {"nodes": [...], "edges": [...]}로.

    엔트리가 없으면 embed_fn을 호출하지 않고 즉시 빈 그래프를 반환한다(임베딩
    모델이 로드되지 않은 상태에서도 호출 가능해야 함).

    cache를 안 주면 매 호출 새 dict를 써서 기존과 동일하게 항상 전체 재인코딩한다
    (테스트가 기대하는 동작). 호출부가 프로세스 생애 동안 들고 있는 dict를 넘기면
    entry_id+version이 안 바뀐 노드는 재인코딩을 건너뛴다."""
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
        vecs = _get_node_vectors(nodes, embed_fn, cache if cache is not None else {})
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
