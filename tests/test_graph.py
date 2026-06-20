"""
wiki-agent / tests / test_graph.py

core/graph.py의 build_graph()를 검증한다: 상태별 노드 태깅, supersedes 컬럼을
source 노드의 현재 status로 재해석하는 구조적 엣지(pending_update/superseded_by),
임베딩 기반 similar 엣지의 threshold/top-k/무방향 중복 제거, include_deprecated/
include_rejected 토글, 빈 DB·단일 노드 경계. tmp DB + embed_fn 스텁 주입으로
오프라인 실행(test_pipeline_promote.py와 동일 패턴).

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pytest

from core import graph, wiki_store


def _setup_db(tmp_path, monkeypatch, seed=True):
    db_path = str(tmp_path / "test_graph_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=seed)
    return db_path


def _const_embed_fn(texts):
    """노드 간 관계(엣지 타입)만 보는 테스트용 — 유사도 값 자체는 신경 안 씀."""
    return np.ones((len(texts), 1))


def _group_embed_fn(texts):
    """텍스트에 GROUPA/GROUPB 마커가 있으면 직교 one-hot 벡터로 보내는 스텁
    (test_pipeline_promote.py의 _embed_fn과 동일 패턴, 그룹 2개로 확장)."""
    vecs = []
    for t in texts:
        low = t.lower()
        if "groupa" in low:
            vecs.append([1.0, 0.0])
        elif "groupb" in low:
            vecs.append([0.0, 1.0])
        else:
            vecs.append([0.0, 0.0])
    return np.array(vecs)


def test_build_graph_tags_nodes_by_status(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.add_entry("wiki_shadow_1", "Shadow topic", "c", "b", status="shadow")
    wiki_store.add_entry("wiki_dep_1", "Dep topic", "c", "b", status="deprecated")
    wiki_store.add_entry("wiki_rej_1", "Rej topic", "c", "b", status="rejected")

    result = graph.build_graph(embed_fn=_const_embed_fn)

    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    assert len(nodes_by_id) == 8  # 5 seed active + shadow + deprecated + rejected
    assert nodes_by_id["wiki_shadow_1"]["status"] == "shadow"
    assert nodes_by_id["wiki_dep_1"]["status"] == "deprecated"
    assert nodes_by_id["wiki_rej_1"]["status"] == "rejected"
    assert nodes_by_id["wiki_0001"]["status"] == "active"


def test_build_graph_pending_update_edge_from_shadow_supersedes(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.add_entry(
        "wiki_shadow_1", "Shadow topic", "c", "b",
        status="shadow", supersedes="wiki_0001",
    )

    result = graph.build_graph(embed_fn=_const_embed_fn)

    matching = [e for e in result["edges"] if e["source"] == "wiki_shadow_1"]
    assert {"source": "wiki_shadow_1", "target": "wiki_0001",
            "type": "pending_update", "weight": 1.0} in matching


def test_build_graph_superseded_by_edge_from_deprecated(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.add_entry(
        "wiki_dep_1", "Dep topic", "c", "b",
        status="deprecated", supersedes="wiki_0001",
    )

    result = graph.build_graph(embed_fn=_const_embed_fn)

    matching = [e for e in result["edges"] if e["source"] == "wiki_dep_1"]
    assert {"source": "wiki_dep_1", "target": "wiki_0001",
            "type": "superseded_by", "weight": 1.0} in matching


def test_build_graph_dangling_supersedes_is_dropped(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.add_entry(
        "wiki_shadow_1", "Shadow topic", "c", "b",
        status="shadow", supersedes="does_not_exist",
    )

    result = graph.build_graph(embed_fn=_const_embed_fn)

    assert all(e["target"] != "does_not_exist" for e in result["edges"])
    assert not any(
        e["source"] == "wiki_shadow_1" and e["type"] == "pending_update"
        for e in result["edges"]
    )


def test_build_graph_similarity_edges_respect_threshold_and_topk(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=False)
    wiki_store.add_entry("a1", "GROUPA item 1", "c", "b", status="active")
    wiki_store.add_entry("a2", "GROUPA item 2", "c", "b", status="active")
    wiki_store.add_entry("b1", "GROUPB item 1", "c", "b", status="active")
    wiki_store.add_entry("b2", "GROUPB item 2", "c", "b", status="active")

    result = graph.build_graph(
        embed_fn=_group_embed_fn, similarity_threshold=0.5, top_k_similar=1,
    )

    similar_pairs = {
        tuple(sorted((e["source"], e["target"])))
        for e in result["edges"] if e["type"] == "similar"
    }
    assert similar_pairs == {("a1", "a2"), ("b1", "b2")}


def test_build_graph_no_self_loops_and_no_duplicate_undirected_similar_edges(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=False)
    ids = ["a1", "a2", "a3", "a4"]
    for eid in ids:
        wiki_store.add_entry(eid, f"GROUPA {eid}", "c", "b", status="active")

    result = graph.build_graph(
        embed_fn=_group_embed_fn, similarity_threshold=0.5, top_k_similar=len(ids),
    )

    similar_edges = [e for e in result["edges"] if e["type"] == "similar"]
    assert all(e["source"] != e["target"] for e in similar_edges)
    assert len(similar_edges) == len(ids) * (len(ids) - 1) // 2


def test_build_graph_include_deprecated_rejected_toggles(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.add_entry(
        "wiki_dep_1", "Dep topic", "c", "b",
        status="deprecated", supersedes="wiki_0001",
    )
    wiki_store.add_entry("wiki_rej_1", "Rej topic", "c", "b", status="rejected")

    result = graph.build_graph(
        embed_fn=_const_embed_fn, include_deprecated=False, include_rejected=False,
    )

    ids = {n["id"] for n in result["nodes"]}
    assert "wiki_dep_1" not in ids
    assert "wiki_rej_1" not in ids
    assert all(
        e["source"] not in {"wiki_dep_1", "wiki_rej_1"}
        and e["target"] not in {"wiki_dep_1", "wiki_rej_1"}
        for e in result["edges"]
    )


def test_build_graph_empty_db_returns_empty_graph(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=False)

    def _raising_embed_fn(texts):
        raise AssertionError("embed_fn should not be called for an empty graph")

    result = graph.build_graph(embed_fn=_raising_embed_fn)

    assert result == {"nodes": [], "edges": []}


def test_build_graph_single_node_no_similarity_edges(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=False)
    wiki_store.add_entry("solo", "Solo topic", "c", "b", status="active")

    result = graph.build_graph(embed_fn=_const_embed_fn)

    assert len(result["nodes"]) == 1
    assert result["edges"] == []


def test_build_graph_rejected_node_includes_rejected_reason(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=False)
    wiki_store.add_entry(
        "wiki_rej_1", "Rej topic", "c", "b", status="rejected",
        sources=[{"type": "document", "rejected_reason": "fabricated fact"}],
    )

    result = graph.build_graph(embed_fn=_const_embed_fn)

    node = next(n for n in result["nodes"] if n["id"] == "wiki_rej_1")
    assert node["rejected_reason"] == "fabricated fact"
