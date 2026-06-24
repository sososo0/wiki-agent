"""
wiki-agent / tests / test_pipeline_reindex.py

reindex_changed()가 changed_entry_ids를 wiki_store.get_entry로 조회해 임베딩을
계산하고 wiki_store.set_embedding으로 영속화하는지, 그새 사라진 entry_id는
건너뛰는지, embed_fn이 실패해도 예외를 전파하지 않는지(lazy 폴백이 안전망이므로
사이클을 막으면 안 됨) 검증한다. 실제 ML 모델 없이 스텁 embed_fn으로 검증.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from core import wiki_store
from core.pipeline import reindex


def _stub_embed_fn(texts):
    return np.array([[float(len(t)), 0.0] for t in texts])


def _failing_embed_fn(texts):
    raise RuntimeError("model unavailable")


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_reindex_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def test_reindex_changed_persists_embedding_for_given_entry(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.add_entry("wiki_9001", "New topic", "New canonical.", "New body",
                          status="shadow")

    reindex.reindex_changed(["wiki_9001"], embed_fn=_stub_embed_fn)

    result = wiki_store.get_embedding("wiki_9001")
    assert result is not None
    version, vector = result
    assert version == 1
    assert vector.shape == (2,)


def test_reindex_changed_skips_missing_entry_id_silently(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    reindex.reindex_changed(["does_not_exist"], embed_fn=_stub_embed_fn)

    assert wiki_store.get_embedding("does_not_exist") is None


def test_reindex_changed_with_empty_list_does_not_call_embed_fn(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    calls = []

    reindex.reindex_changed([], embed_fn=lambda texts: calls.append(texts) or [])

    assert calls == []


def test_reindex_changed_swallows_embed_fn_failure(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.add_entry("wiki_9001", "New topic", "New canonical.", "New body",
                          status="shadow")

    reindex.reindex_changed(["wiki_9001"], embed_fn=_failing_embed_fn)  # 예외 전파 없이 조용히 리턴

    assert wiki_store.get_embedding("wiki_9001") is None


def test_reindex_changed_uses_entry_current_version(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.add_entry("wiki_9001", "v1 topic", "v1 canonical.", "v1 body")
    wiki_store.add_entry("wiki_9001", "v2 topic", "v2 canonical.", "v2 body")  # version -> 2

    reindex.reindex_changed(["wiki_9001"], embed_fn=_stub_embed_fn)

    version, _ = wiki_store.get_embedding("wiki_9001")
    assert version == 2
