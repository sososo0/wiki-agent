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

[클러스터 설계]
- 프론트(graph.html)가 전체 노드를 한 화면에 흩뿌리는 대신 "대표(backbone) 노드만
  먼저 보이고 클릭하면 펼쳐지는" 계층형으로 보여줄 수 있게, 노드를 묶어
  cluster_id/is_backbone/cluster_size를 부여한다.
- similar 엣지의 connected component로 묶는 방식은 top-k 유사도의 전이성
  (A~B~C) 때문에 실데이터(462노드)에서 거의 전부가 거대 덩어리 하나로
  합쳐져(컴포넌트 크기 [453, 3, 3, 3]) 폐기 — 대신 임베딩 벡터 자체에 k-means를
  돌려 크기가 골고루 분산된 클러스터를 만든다(compute_clusters).
- 각 클러스터의 대표는 centroid와 코사인 유사도가 가장 높은 실제 멤버 노드(가짜
  합성 노드 아님) — 동률은 entry_id 사전순으로 결정적 선택. 노드 수가
  target_cluster_size 이하면 클러스터링이 무의미하므로 스킵하고 전부 backbone으로
  둔다(작은 테스트 DB는 기존 평면 동작 그대로).
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


def _attach_translations(nodes: List[Dict[str, Any]], lang: str = "ko") -> None:
    """그래프 화면 표시용 topic_ko/canonical_ko/body_md_ko를 nodes에 in-place로
    부여한다. 원본 topic/canonical/body_md는 검색/평가가 쓰는 그대로 두고
    건드리지 않는다 — 순수 DB 읽기(translation_cache)일 뿐 LLM 호출 없음, 번역
    생성은 scripts/translate_wiki_labels.py가 오프라인으로 미리 해둔다.
    캐시된 version이 노드의 현재 version과 다르면(콘텐츠가 그새 바뀜) 무시하고
    None으로 둬서 프론트가 영어로 자연 폴백한다."""
    cached = wiki_store.get_translations([n["id"] for n in nodes], lang=lang)
    for n in nodes:
        hit = cached.get(n["id"])
        if hit and hit["version"] == n.get("version"):
            n["topic_ko"] = hit["topic"]
            n["canonical_ko"] = hit["canonical"]
            n["body_md_ko"] = hit["body_md"]
        else:
            n["topic_ko"] = None
            n["canonical_ko"] = None
            n["body_md_ko"] = None


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


def _topk_similarity_edges(
    ids: List[str],
    vecs: np.ndarray,
    *,
    top_k: int,
    threshold: float,
    edge_type: str = "similar",
    seen: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """ids[i]<->vecs[i](정규화됨)인 벡터들 사이 top-k + threshold 무방향 유사도
    엣지를 만든다. 노드 레벨 similar 엣지와 클러스터 대표(backbone) 레벨 엣지가
    같은 로직을 쓰므로 공유한다. seen을 두 레벨 호출에 공유하면 같은 쌍이
    두 번(예: 두 대표가 서로 node-level top-k에도 들고 backbone 레벨에도 또
    걸리는 경우) 중복으로 안 들어간다."""
    seen = seen if seen is not None else set()
    sims = vecs @ vecs.T
    np.fill_diagonal(sims, -1.0)
    edges: List[Dict[str, Any]] = []
    for i in range(len(ids)):
        order = np.argsort(-sims[i])[:top_k]
        for j in order:
            if sims[i, j] < threshold:
                continue
            pair_key = tuple(sorted((ids[i], ids[j])))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            edges.append({
                "source": ids[i], "target": ids[j], "type": edge_type,
                "weight": round(float(sims[i, j]), 4),
            })
    return edges


def _default_cluster_fields(nodes: List[Dict[str, Any]]) -> None:
    """클러스터링을 건너뛸 때(노드가 너무 적음) 모든 노드를 자기 자신만의
    backbone으로 표시 — 지금까지의 평면 그래프 동작과 동일하게 만든다."""
    for n in nodes:
        n["cluster_id"] = n["id"]
        n["is_backbone"] = True
        n["cluster_size"] = 1


def compute_clusters(
    nodes: List[Dict[str, Any]],
    vecs: np.ndarray,
    *,
    target_cluster_size: int = 12,
    max_clusters: int = 500,
    seed: int = 0,
    iters: int = 25,
) -> List[Dict[str, Any]]:
    """nodes를 in-place로 cluster_id/is_backbone/cluster_size 필드로 보강하고,
    size>=2인 클러스터 목록을 반환한다.

    similar 엣지의 connected component로 묶는 방식은 실제 데이터(462개 노드)에서
    top-k 유사도의 전이성(A~B~C) 때문에 거의 전부가 거대 덩어리 하나로 합쳐지는
    문제가 있어(컴포넌트 크기 [453, 3, 3, 3]) 채택하지 않았다. 대신 임베딩
    벡터 자체에 k-means를 돌려(정규화된 벡터라 dot product = 코사인 유사도,
    retrieval.default_embed_fn과 동일 전제) 크기가 골고루 분산된 클러스터를
    만든다. 노드 수가 target_cluster_size 이하면 클러스터링이 의미 없으므로
    스킵하고 전부 backbone으로 둔다(작은 테스트 DB의 기존 평면 동작 보존).

    클러스터 개수 k는 기본 n/target_cluster_size인데, 이걸 그대로 두면 n이
    커질수록 k도 같이 커져서 centroid 할당 행렬곱(vecs @ centroids.T)이
    O(n·k) = O(n²/target_cluster_size)로 사실상 2차가 된다(실측: n=50000에서
    약 16초). max_clusters로 k에 상한을 둬 n이 아주 커져도 O(n·max_clusters)
    (n에 선형)로 묶는다 — 트레이드오프: 코퍼스가
    target_cluster_size*max_clusters를 넘으면 클러스터당 멤버 수가
    target_cluster_size보다 커진다(의도된 양보)."""
    n = len(nodes)
    if n <= target_cluster_size or n < 2:
        _default_cluster_fields(nodes)
        return []

    k = min(max(1, round(n / target_cluster_size)), max_clusters)
    rng = np.random.default_rng(seed)
    centroid_idx = rng.choice(n, size=k, replace=False)
    centroids = vecs[centroid_idx].copy()

    # 클러스터별 Python for 루프(O(n·k))로 멤버를 모아 평균 내던 방식은 n/k가
    # 커지면(코퍼스가 수만 개) 압도적으로 느려진다(실측: n=50000에서 약 37초).
    # np.add.at/bincount로 한 번에 클러스터별 합/개수를 구해 O(n)으로 줄인다 —
    # 결과는 수학적으로 동일(클러스터별 평균 후 정규화), 빈 클러스터는 그대로
    # 이전 centroid 유지(기존 동작과 동일).
    assign = np.zeros(n, dtype=int)
    dim = vecs.shape[1]
    for _ in range(iters):
        sims = vecs @ centroids.T
        assign = np.argmax(sims, axis=1)
        sums = np.zeros((k, dim))
        np.add.at(sums, assign, vecs)
        counts = np.bincount(assign, minlength=k)
        nonzero = counts > 0
        means = sums[nonzero] / counts[nonzero, None]
        norms = np.linalg.norm(means, axis=1, keepdims=True)
        safe_norms = np.where(norms > 0, norms, 1.0)
        centroids[nonzero] = means / safe_norms

    sims = vecs @ centroids.T

    # 클러스터별 멤버 인덱스도 마찬가지로 "클러스터마다 전체 n을 다시 스캔"
    # (O(n·k))하던 걸 정렬 1번(O(n log n))으로 묶는다.
    order = np.argsort(assign, kind="stable")
    sorted_assign = assign[order]
    boundaries = np.searchsorted(sorted_assign, np.arange(k + 1))

    clusters: List[Dict[str, Any]] = []
    for c in range(k):
        start, end = boundaries[c], boundaries[c + 1]
        if start == end:
            continue
        # centroid와 코사인 유사도가 가장 높은 멤버를 대표로 — 동률은 entry_id
        # 사전순(결정적 선택, k-means 자체도 seed 고정이라 호출 간 안정적).
        member_idx = sorted(order[start:end].tolist(), key=lambda i: nodes[i]["id"])
        rep_i = max(member_idx, key=lambda i: (round(float(sims[i, c]), 8), ))
        rep_id = nodes[rep_i]["id"]
        size = len(member_idx)

        for i in member_idx:
            nodes[i]["cluster_id"] = rep_id
            nodes[i]["is_backbone"] = (i == rep_i)
            nodes[i]["cluster_size"] = size

        if size >= 2:
            clusters.append({
                "cluster_id": rep_id,
                "label": nodes[rep_i].get("topic") or rep_id,
                "size": size,
                "member_ids": [nodes[i]["id"] for i in member_idx],
            })

    return clusters


def build_graph(
    *,
    embed_fn: Optional[Callable] = None,
    similarity_threshold: float = 0.3,
    top_k_similar: int = 2,
    target_cluster_size: int = 12,
    max_clusters: int = 500,
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
        return {"nodes": [], "edges": [], "clusters": []}

    _attach_translations(nodes)

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

    clusters: List[Dict[str, Any]] = []
    if len(nodes) >= 2:
        embed_fn = embed_fn or retrieval.default_embed_fn
        vecs = _get_node_vectors(nodes, embed_fn, cache if cache is not None else {})
        ids = [n["id"] for n in nodes]
        id_to_idx = {n["id"]: i for i, n in enumerate(nodes)}

        # 클러스터링을 similar 엣지 계산보다 먼저 한다 — 클러스터를 알아야 "노드별
        # similar 엣지를 전체 n×n 대신 자기 클러스터 안에서만 계산"할 수 있다(아래).
        clusters = compute_clusters(
            nodes, vecs, target_cluster_size=target_cluster_size, max_clusters=max_clusters,
        )

        seen_undirected: set = set()
        if clusters:
            # 코퍼스가 커서 클러스터링이 활성화된 경우: similar 엣지를 전체 n×n
            # (O(n²), 코퍼스가 수만 개면 유사도 행렬 자체가 메모리를 못 감당) 대신
            # 클러스터 내부(~target_cluster_size개)에서만 계산해 O(n)에 가깝게
            # 줄인다. k-means 자체가 임베딩 기반이라 한 노드의 진짜 최근접 이웃은
            # 이미 같은 클러스터에 있을 확률이 높아 결과 품질 손실은 미미함.
            # (트레이드오프: k-means가 단 1개만 배정한 클러스터는 비교 대상이
            # 없어 similar 엣지를 못 받음 — 드문 경우라 받아들임.)
            for c in clusters:
                member_idx = [id_to_idx[mid] for mid in c["member_ids"]]
                edges.extend(_topk_similarity_edges(
                    [ids[i] for i in member_idx], vecs[member_idx],
                    top_k=top_k_similar, threshold=similarity_threshold,
                    edge_type="similar", seen=seen_undirected,
                ))
        else:
            # 클러스터링이 스킵된 소규모 그래프(n <= target_cluster_size) — n 자체가
            # 작아 전체비교 비용이 문제되지 않으므로 기존처럼 한 번에 계산.
            edges.extend(_topk_similarity_edges(
                ids, vecs, top_k=top_k_similar, threshold=similarity_threshold,
                edge_type="similar", seen=seen_undirected,
            ))

        # "큰 주제 노드들이 서로 연결되어 있어야" 한다는 요구사항: 대표(backbone)
        # 끼리도 같은 top-k+threshold 유사도 기준으로 엣지를 만든다. 대표는
        # 보통 자기 클러스터 멤버를 최근접 이웃으로 갖기 쉬워(top_k_similar이
        # 작으면) node-level 엣지만으로는 backbone들이 서로 연결 안 되고 따로
        # 떠 있는 덩어리로 보일 수 있다 — 그래서 대표들만 따로 모아 같은 로직을
        # 한 번 더 돌린다(seen_undirected 공유로 중복 엣지 방지). 대표는 항상
        # 전체 노드 수보다 훨씬 적어(~n/target_cluster_size) 이 비교는 이미 작다.
        if clusters:
            rep_ids = [c["cluster_id"] for c in clusters]
            rep_idx = [id_to_idx[rid] for rid in rep_ids]
            edges.extend(_topk_similarity_edges(
                rep_ids, vecs[rep_idx], top_k=top_k_similar, threshold=similarity_threshold,
                edge_type="cluster_similar", seen=seen_undirected,
            ))
    else:
        _default_cluster_fields(nodes)

    return {"nodes": nodes, "edges": edges, "clusters": clusters}
