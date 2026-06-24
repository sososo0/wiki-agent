"""
wiki-agent / tests / test_lru_cache.py

core/lru_cache.py의 LRUCache가 core/graph.py·core/retrieval.py가 기대하는
.get()/__setitem__ 캐시 계약을 만족하면서, maxsize를 넘으면 가장 오래 안 쓰인
키부터 버리는지(LRU) 검증한다.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.lru_cache import LRUCache


def test_get_returns_default_for_missing_key():
    cache = LRUCache(maxsize=10)
    assert cache.get("missing") is None
    assert cache.get("missing", "fallback") == "fallback"


def test_set_then_get_roundtrip():
    cache = LRUCache(maxsize=10)
    cache["a"] = (1, "vec_a")
    assert cache.get("a") == (1, "vec_a")
    assert len(cache) == 1


def test_overwriting_existing_key_updates_value():
    cache = LRUCache(maxsize=10)
    cache["a"] = (1, "vec_a")
    cache["a"] = (2, "vec_a_v2")
    assert cache.get("a") == (2, "vec_a_v2")
    assert len(cache) == 1


def test_exceeding_maxsize_evicts_oldest_key():
    cache = LRUCache(maxsize=2)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3  # maxsize=2 초과 -> 가장 오래된 "a"가 버려져야 함

    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_get_refreshes_recency_so_it_survives_eviction():
    cache = LRUCache(maxsize=2)
    cache["a"] = 1
    cache["b"] = 2
    cache.get("a")     # "a"를 최근 사용으로 갱신 -> 이제 "b"가 가장 오래됨
    cache["c"] = 3      # maxsize 초과 -> "b"가 버려져야 함

    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_contains():
    cache = LRUCache(maxsize=10)
    cache["a"] = 1
    assert "a" in cache
    assert "missing" not in cache
