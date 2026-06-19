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

EMBED_MODEL = os.environ.get("WIKI_AGENT_EMBED_MODEL", "all-MiniLM-L6-v2")
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


def _dense_rank(query: str, entries: List[Dict], embed_fn: Callable) -> List[str]:
    """entries 전체에 대해 쿼리와의 코사인 유사도 내림차순으로 entry_id를 반환."""
    if not entries:
        return []
    vecs = np.asarray(embed_fn([_entry_text(e) for e in entries]))
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
) -> List[Dict]:
    """BM25 + dense 랭킹을 RRF로 합치고 cross-encoder로 재정렬해 상위 k개를 반환.

    반환 shape은 search_wiki()와 동일: entry_id/topic/canonical/score/confidence.
    embed_fn/rerank_fn을 주입하면(테스트용) 실제 모델을 로딩하지 않고 검증 가능.
    """
    embed_fn = embed_fn or default_embed_fn
    rerank_fn = rerank_fn or default_rerank_fn

    by_id = {e["entry_id"]: e for e in entries}
    dense_ids = _dense_rank(query, entries, embed_fn)
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
