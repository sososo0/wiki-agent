"""
wiki-agent / tests / test_mcp_server.py

serving/mcp_server.py의 search_wiki 도구가 module-level _search_embed_cache를
실제로 재사용하는지 검증한다(demo/app.py의 _search_embed_cache와 동일 패턴 —
이게 없으면 에이전트가 검색할 때마다 활성 엔트리 전체를 재인코딩한다). 실제
임베딩/rerank 모델은 로딩하지 않고 core/retrieval.py의 default_embed_fn/
default_rerank_fn을 스텁 주입해 오프라인으로 빠르게 실행된다(tests/test_demo_app.py와
동일 패턴).

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pytest

from core import retrieval, wiki_store


def _counting_embed_fn(call_sizes):
    def _fn(texts):
        call_sizes.append(len(texts))
        return np.ones((len(texts), 4), dtype=float)
    return _fn


def _stub_rerank_fn(query, texts):
    return [1.0] * len(texts)


@pytest.fixture
def mcp_server_module(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_mcp_server_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)

    import importlib
    from serving import mcp_server
    importlib.reload(mcp_server)  # _search_embed_cache를 매 테스트 깨끗하게
    return mcp_server


def test_search_wiki_returns_results(mcp_server_module, monkeypatch):
    monkeypatch.setattr(retrieval, "default_embed_fn", lambda texts: np.ones((len(texts), 4)))
    monkeypatch.setattr(retrieval, "default_rerank_fn", _stub_rerank_fn)

    result = mcp_server_module.search_wiki("how do I retry failed requests", k=3)

    assert result["count"] == 3
    assert all("entry_id" in r for r in result["results"])


def test_search_wiki_reuses_cache_across_calls(mcp_server_module, monkeypatch):
    """캐시가 없으면 두 번째 호출도 첫 번째와 동일하게 활성 엔트리 전체(seed 5개)를
    재인코딩한다. 캐시가 제대로 연결됐다면 두 번째 호출은 엔트리가 안 바뀌었으니
    쿼리 1건만 새로 인코딩해야 한다(entry 쪽 0건 재인코딩)."""
    call_sizes = []
    monkeypatch.setattr(retrieval, "default_embed_fn", _counting_embed_fn(call_sizes))
    monkeypatch.setattr(retrieval, "default_rerank_fn", _stub_rerank_fn)

    mcp_server_module.search_wiki("how do I retry failed requests", k=3)
    assert len(mcp_server_module._search_embed_cache) == 5  # seed 엔트리 5개 전부 캐시됨

    first_call_total = sum(call_sizes)
    call_sizes.clear()

    mcp_server_module.search_wiki("how do I retry failed requests", k=3)
    second_call_total = sum(call_sizes)

    # 두 번째 호출은 쿼리 1건만 재인코딩(entry_vectors는 캐시 hit으로 0건) —
    # 캐시가 없었다면 첫 호출과 동일하게 엔트리 5개 + 쿼리 1건이 다시 인코딩됐을 것.
    assert second_call_total == 1
    assert second_call_total < first_call_total
