"""
wiki-agent / core / pipeline / reindex.py

갱신 사이클(scripts/run_update_cycle.py)이 막 쓴 entry_id의 임베딩을 미리 계산해
영속 임베딩 테이블(wiki_embedding)에 적재한다. 안 해도 정답은 맞다 —
PersistentEmbeddingCache의 lazy 폴백이 그 자리에서 인코딩해 채워주므로, 이 함수는
인코딩 비용을 사용자 요청 경로에서 오프라인 파이프라인 쪽으로 옮기는 최적화일 뿐
정확성에 필수는 아니다.

그래서 임베딩 계산이 실패해도 사이클 전체를 막지 않는다 — lazy 폴백이 안전망이기
때문.
"""

from typing import Callable, List, Optional

from core import retrieval, wiki_store


def reindex_changed(changed_entry_ids: List[str], *, embed_fn: Optional[Callable] = None) -> None:
    """changed_entry_ids 각각을 wiki_store.get_entry로 조회해(status 무관) 현재
    version/텍스트로 임베딩을 계산하고 wiki_store.set_embedding으로 영속화한다.
    그새 사라진 entry_id는 조용히 건너뛴다."""
    entries = [wiki_store.get_entry(eid) for eid in changed_entry_ids]
    entries = [e for e in entries if e is not None]
    if not entries:
        return None

    embed_fn = embed_fn or retrieval.default_embed_fn
    try:
        texts = [retrieval._entry_text(e) for e in entries]
        vectors = embed_fn(texts)
        for entry, vector in zip(entries, vectors):
            wiki_store.set_embedding(entry["entry_id"], entry["version"], vector)
    except Exception:  # noqa: BLE001 - 최적화일 뿐, lazy 폴백이 있어 사이클을 막지 않음
        return None
