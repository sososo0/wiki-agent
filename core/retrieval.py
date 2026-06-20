"""
wiki-agent / core / retrieval.py

FTS5 BM25 키워드 검색에 dense 임베딩 검색 + RRF 융합 + cross-encoder rerank를 더한다.
core.wiki_store.search_wiki()가 이 모듈을 호출해 최종 랭킹을 만든다 — 랭킹 로직만
여기 분리하고, DB 접근/로깅은 wiki_store.py가 그대로 담당한다(역할 분리, 순수 함수라
pytest로 단독 검증 가능).

[의존성 추가 이유]
- sentence-transformers: dense 임베딩 + cross-encoder rerank를 로컬에서 계산.
  외부 임베딩 API 키/네트워크 비용 없이 검색 경로가 동작해야 MCP 서빙·평가가
  안정적으로 재현된다. (LLM-as-judge는 eval/run_eval.py 평가에만 쓰이고
  검색 경로와는 무관)
- numpy: 코사인 유사도 / RRF 계산.

[임베딩 캐시 없음 — 의도적]
코퍼스가 작아 매 쿼리 재인코딩 비용이 무시할 수준이고, 무거운 부분(모델 로딩)은
이미 모듈 싱글톤이라 프로세스당 1회만 발생한다. 코퍼스가 커지면 향후
pipeline/reindex.py(Step 7)에서 영속 임베딩 저장을 추가한다.
"""

import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

EMBED_MODEL = os.environ.get(
    "WIKI_AGENT_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
)
RERANK_MODEL = os.environ.get(
    "WIKI_AGENT_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
)
RRF_K = 60  # RRF 스무딩 상수(업계 표준 기본값)

_st_model = None
_ce_model = None


def _entry_text(entry: Dict) -> str:
    return f"{entry['topic']}. {entry['canonical']} {entry.get('body_md') or ''}".strip()


def _load_embed_model():
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer(EMBED_MODEL)
    return _st_model


def _load_rerank_model():
    global _ce_model
    if _ce_model is None:
        from sentence_transformers import CrossEncoder
        _ce_model = CrossEncoder(RERANK_MODEL)
    return _ce_model


def default_embed_fn(texts: Sequence[str]) -> np.ndarray:
    model = _load_embed_model()
    return np.asarray(model.encode(list(texts), normalize_embeddings=True))


def default_rerank_fn(query: str, texts: Sequence[str]) -> List[float]:
    model = _load_rerank_model()
    scores = model.predict([(query, t) for t in texts])
    return [float(s) for s in scores]


def _entry_vectors(
    entries: List[Dict], embed_fn: Callable, cache: Dict[str, Tuple[Optional[int], np.ndarray]]
) -> np.ndarray:
    """entry_id+version으로 캐시 적중하는 엔트리는 재인코딩을 건너뛴다(core/graph.py
    _get_node_vectors와 동일 패턴). 코퍼스가 커질수록(1000+) 매 쿼리마다 entries
    전체를 재인코딩하는 비용이 체감 지연이 되므로 도입 — cache를 안 주면(기본 None
    -> 호출부가 매번 새 dict) 항상 전체 재인코딩해 기존 동작/테스트와 동일하다."""
    vecs: List[Optional[np.ndarray]] = [None] * len(entries)
    miss_idx: List[int] = []
    miss_texts: List[str] = []

    for i, e in enumerate(entries):
        hit = cache.get(e["entry_id"])
        if hit is not None and hit[0] == e.get("version"):
            vecs[i] = hit[1]
        else:
            miss_idx.append(i)
            miss_texts.append(_entry_text(e))

    if miss_texts:
        new_vecs = np.asarray(embed_fn(miss_texts))
        for k, i in enumerate(miss_idx):
            vecs[i] = new_vecs[k]
            cache[entries[i]["entry_id"]] = (entries[i].get("version"), new_vecs[k])

    return np.asarray(vecs)


def _dense_rank(
    query: str,
    entries: List[Dict],
    embed_fn: Callable,
    cache: Optional[Dict[str, Tuple[Optional[int], np.ndarray]]] = None,
) -> List[str]:
    """entries 전체에 대해 쿼리와의 코사인 유사도 내림차순으로 entry_id를 반환."""
    if not entries:
        return []
    vecs = _entry_vectors(entries, embed_fn, cache if cache is not None else {})
    q_vec = np.asarray(embed_fn([query])[0])
    sims = vecs @ q_vec  # normalize_embeddings=True 이므로 내적 = 코사인 유사도
    order = np.argsort(-sims)
    return [entries[i]["entry_id"] for i in order]


def reciprocal_rank_fusion(
    rankings: List[List[str]], rrf_k: int = RRF_K
) -> List[Tuple[str, float]]:
    """여러 순위 리스트를 RRF로 융합: score(d) = sum(1 / (rrf_k + rank))."""
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for rank, entry_id in enumerate(ranking, start=1):
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def hybrid_search(
    query: str,
    entries: List[Dict],
    bm25_ranked_ids: List[str],
    k: int = 5,
    fetch_k: int = 20,
    embed_fn: Optional[Callable] = None,
    rerank_fn: Optional[Callable] = None,
    cache: Optional[Dict[str, Tuple[Optional[int], np.ndarray]]] = None,
) -> List[Dict]:
    """BM25 + dense 랭킹을 RRF로 합치고 cross-encoder로 재정렬해 상위 k개를 반환.

    반환 shape은 search_wiki()와 동일: entry_id/topic/canonical/score/confidence.
    embed_fn/rerank_fn을 주입하면(테스트용) 실제 모델을 로딩하지 않고 검증 가능.
    cache를 주면(entry_id+version 키) 호출부가 들고 있는 동안 콘텐츠가 안 바뀐
    엔트리는 재인코딩을 건너뛴다 — 안 주면(기본값) 매 호출 전체 재인코딩이라
    기존 동작과 동일(테스트 격리에 영향 없음, core/graph.py와 동일 패턴).
    """
    embed_fn = embed_fn or default_embed_fn
    rerank_fn = rerank_fn or default_rerank_fn

    by_id = {e["entry_id"]: e for e in entries}
    dense_ids = _dense_rank(query, entries, embed_fn, cache)
    fused = reciprocal_rank_fusion([bm25_ranked_ids[:fetch_k], dense_ids[:fetch_k]])
    candidate_ids = [eid for eid, _ in fused[:fetch_k] if eid in by_id]
    if not candidate_ids:
        return []

    texts = [_entry_text(by_id[eid]) for eid in candidate_ids]
    rerank_scores = rerank_fn(query, texts)
    order = sorted(range(len(candidate_ids)), key=lambda i: -rerank_scores[i])[:k]

    return [
        {
            "entry_id": candidate_ids[i],
            "topic": by_id[candidate_ids[i]]["topic"],
            "canonical": by_id[candidate_ids[i]]["canonical"],
            "score": round(float(rerank_scores[i]), 4),
            "confidence": by_id[candidate_ids[i]]["confidence"],
        }
        for i in order
    ]
