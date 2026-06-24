"""
wiki-agent / tests / test_wiki_store_embedding.py

영속 임베딩 캐시(wiki_embedding)의 get_embedding/set_embedding 라운드트립과,
core/lru_cache.LRUCache와 동일 계약을 만족하는 PersistentEmbeddingCache가 메모리
미스 시 DB로 폴백하고, 새 프로세스(빈 메모리)에서도 영속된 값을 읽어오는지 검증한다.

실행: pytest
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_embedding_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def test_set_then_get_embedding_roundtrip(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    vector = np.array([0.1, 0.2, 0.3], dtype=np.float32)

    wiki_store.set_embedding("wiki_0001", 1, vector, model="test-model")

    version, restored = wiki_store.get_embedding("wiki_0001", model="test-model")
    assert version == 1
    assert np.allclose(restored, vector)


def test_get_embedding_missing_entry_id_is_none(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    assert wiki_store.get_embedding("wiki_0001", model="test-model") is None


def test_get_embedding_with_different_model_is_none(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.set_embedding("wiki_0001", 1, np.array([1.0, 0.0]), model="model-a")

    assert wiki_store.get_embedding("wiki_0001", model="model-b") is None


def test_set_embedding_upserts_on_same_entry_id(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.set_embedding("wiki_0001", 1, np.array([1.0, 0.0]), model="test-model")
    wiki_store.set_embedding("wiki_0001", 2, np.array([0.0, 1.0]), model="test-model")

    version, restored = wiki_store.get_embedding("wiki_0001", model="test-model")
    assert version == 2
    assert np.allclose(restored, [0.0, 1.0])


def test_persistent_cache_hits_memory_without_db_lookup(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    cache = wiki_store.PersistentEmbeddingCache(model="test-model")
    cache["wiki_0001"] = (1, np.array([1.0, 0.0]))

    version, vector = cache.get("wiki_0001")
    assert version == 1
    assert np.allclose(vector, [1.0, 0.0])


def test_persistent_cache_falls_back_to_db_on_memory_miss(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    writer = wiki_store.PersistentEmbeddingCache(model="test-model")
    writer["wiki_0001"] = (1, np.array([0.5, 0.5]))

    # 새 인스턴스 = 빈 메모리. DB에 영속된 값을 읽어와야 한다(재인코딩 없이).
    reader = wiki_store.PersistentEmbeddingCache(model="test-model")
    version, vector = reader.get("wiki_0001")
    assert version == 1
    assert np.allclose(vector, [0.5, 0.5])


def test_persistent_cache_returns_default_when_absent_everywhere(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    cache = wiki_store.PersistentEmbeddingCache(model="test-model")

    assert cache.get("wiki_9999") is None
    assert cache.get("wiki_9999", "fallback") == "fallback"
