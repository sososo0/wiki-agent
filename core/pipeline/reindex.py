"""
wiki-agent / core / pipeline / reindex.py

현재 아키텍처(core/retrieval.py, Step 3)는 영속 임베딩 캐시가 없다 —
search_wiki()가 매 쿼리마다 list_active_entries()를 직접 재임베딩하므로
"색인을 갱신"할 대상 자체가 없다. 그래서 이 함수는 거의 no-op이다.
오케스트레이션 흐름의 자리는 유지해서(가이드 구조 보존) 코퍼스가 커져
영속 임베딩 저장이 필요해지는 시점에 이 지점을 채우면 된다.
"""

from typing import List


def reindex_changed(changed_entry_ids: List[str]) -> None:
    """no-op: 임베딩 캐시가 없으므로 재색인할 것이 없다."""
    return None
