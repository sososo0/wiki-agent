"""
wiki-agent / core / lru_cache.py

core/graph.py(_get_node_vectors)와 core/retrieval.py(_entry_vectors)가 공유하는
임베딩 캐시 계약은 `cache.get(key) -> (version, vector)|None` + `cache[key] = (version,
vector)` 두 가지뿐이다(평범한 dict로 충분히 쓰여왔음). 문제는 데모/MCP 서버처럼 오래
사는 프로세스가 이 dict를 그대로 들고 있으면, 코퍼스가 커지거나 deprecated/rejected
엔트리가 쌓일수록(둘 다 DB에서 삭제되지 않음) 캐시도 한 번 본 entry_id를 영원히 들고
있어 메모리가 천천히 자란다(evict 없음).

LRUCache는 그 두 메서드만 구현한 최소 드롭인 대체물 — graph.py/retrieval.py는 무수정.
maxsize를 넘으면 가장 오래 안 쓰인(=가장 오래 전에 get/set된) 키부터 버린다. 표준
라이브러리(collections.OrderedDict)만 써서 새 의존성을 추가하지 않는다.
"""

import collections
from typing import Any, Optional


class LRUCache:
    def __init__(self, maxsize: int = 2000):
        self.maxsize = maxsize
        self._data: "collections.OrderedDict[str, Any]" = collections.OrderedDict()

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data
