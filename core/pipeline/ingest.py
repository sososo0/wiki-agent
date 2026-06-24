"""
wiki-agent / core / pipeline / ingest.py

피드백 파이프라인의 입력 정규화 단계. retrieval_log/feedback 원본 행을
mine.py가 바로 쓸 수 있는 형태로 정리한다. DB 접근 없음(순수 함수) —
wiki_store.list_retrieval_log()/list_feedback()의 출력을 받아서 처리.
"""

from typing import Any, Callable, Dict, List, Optional

import numpy as np


def ingest_retrieval_log(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """빈 쿼리/3자 미만 제거하고 그룹핑 키 norm_query(소문자+공백정리)를 추가."""
    out = []
    for row in rows:
        q = (row.get("query") or "").strip()
        if len(q) < 3:
            continue
        out.append({**row, "norm_query": " ".join(q.lower().split())})
    return out


def cluster_paraphrased_queries(
    rows: List[Dict[str, Any]], *,
    embed_fn: Optional[Callable] = None,
    similarity_threshold: float = 0.85,
) -> List[Dict[str, Any]]:
    """norm_query가 다른데 의미가 같은 질문(paraphrase)을 임베딩 유사도로 묶어,
    같은 클러스터의 norm_query를 대표 문자열로 통일한다. mine.py의 그룹핑은
    여전히 단순 exact-match라 mine.py 자체는 무수정 — 같은 의미를 다르게 표현한
    질문이 각각 min_freq 미달로 영원히 gap을 못 넘는 문제를 여기서 흡수한다.

    opt-in 전용(--cluster-paraphrases 플래그 기본 off)이라 호출 안 하면 모델이
    전혀 로딩되지 않는다."""
    if not rows:
        return rows

    unique_queries = sorted({r["norm_query"] for r in rows})
    if len(unique_queries) <= 1:
        return rows

    if embed_fn is None:
        from core import retrieval
        embed_fn = retrieval.default_embed_fn

    vecs = np.asarray(embed_fn(unique_queries))
    sims = vecs @ vecs.T  # normalize_embeddings=True 가정

    n = len(unique_queries)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if sims[i, j] >= similarity_threshold:
                union(i, j)

    # 클러스터별 대표 = 사전순 최솟값(결정적 선택)
    cluster_rep: Dict[int, str] = {}
    for i in range(n):
        root = find(i)
        if root not in cluster_rep or unique_queries[i] < cluster_rep[root]:
            cluster_rep[root] = unique_queries[i]

    query_to_rep = {unique_queries[i]: cluster_rep[find(i)] for i in range(n)}
    return [{**r, "norm_query": query_to_rep[r["norm_query"]]} for r in rows]


def ingest_feedback(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """entry/query에 귀속하지 않는 집계 신호만 산출 (귀속 키가 없으므로)."""
    n = len(rows)
    down = sum(1 for r in rows if r.get("thumb") == "down")
    return {"n": n, "down": down, "down_rate": (down / n) if n else 0.0}
