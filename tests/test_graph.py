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

    assert result == {"nodes": [], "edges": [], "clusters": []}


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


def test_build_graph_attaches_korean_translation_when_version_matches(tmp_path, monkeypatch):
    """그래프 표시용 한글 번역 캐시(translation_cache)가 있고 version이 노드의
    현재 version과 일치하면 _ko 필드로 붙는다 — 원본 topic/canonical/body_md는
    그대로 영어로 남아야 한다(검색/평가가 그 필드를 그대로 쓰므로)."""
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.set_translation("wiki_0001", 1, "재시도 백오프 전략", "지수 백오프를 쓰세요.", "본문 번역")

    result = graph.build_graph(embed_fn=_const_embed_fn)

    node = next(n for n in result["nodes"] if n["id"] == "wiki_0001")
    assert node["topic"] == "Retry backoff strategy"
    assert node["topic_ko"] == "재시도 백오프 전략"
    assert node["canonical_ko"] == "지수 백오프를 쓰세요."
    assert node["body_md_ko"] == "본문 번역"


def test_build_graph_ignores_stale_translation_after_content_changes(tmp_path, monkeypatch):
    """캐시된 번역의 version이 노드의 현재 version과 다르면(콘텐츠가 그새
    바뀜) 무시하고 _ko 필드를 None으로 둬서 프론트가 영어로 폴백하게 한다."""
    _setup_db(tmp_path, monkeypatch, seed=True)
    wiki_store.set_translation("wiki_0001", 999, "오래된 번역", "오래된 요약.", "오래된 본문")

    result = graph.build_graph(embed_fn=_const_embed_fn)

    node = next(n for n in result["nodes"] if n["id"] == "wiki_0001")
    assert node["topic_ko"] is None
    assert node["canonical_ko"] is None
    assert node["body_md_ko"] is None


def test_build_graph_skips_clustering_for_small_graphs(tmp_path, monkeypatch):
    """노드 수가 target_cluster_size 이하면 클러스터링이 무의미하므로 스킵하고
    전부 backbone으로 둔다 — 작은 DB는 기존 평면 그래프 동작과 동일해야 함."""
    _setup_db(tmp_path, monkeypatch, seed=True)  # 5개 seed 엔트리

    result = graph.build_graph(embed_fn=_const_embed_fn, target_cluster_size=12)

    assert result["clusters"] == []
    assert all(n["is_backbone"] for n in result["nodes"])
    assert all(n["cluster_size"] == 1 for n in result["nodes"])
    assert all(n["cluster_id"] == n["id"] for n in result["nodes"])


def test_compute_clusters_groups_by_embedding_not_chained_similarity(tmp_path, monkeypatch):
    """connected-component 방식은 top-k 유사도의 전이성 때문에 무관한 그룹까지
    하나로 합쳐지는 문제가 있어 폐기했다(실데이터에서 462개 중 453개가 거대
    덩어리 하나로 뭉침) — k-means는 임베딩 벡터 자체로 묶으므로 이런 체이닝
    없이 GROUPA/GROUPB가 깨끗하게 분리되어야 한다."""
    _setup_db(tmp_path, monkeypatch, seed=False)
    for i in range(1, 5):
        wiki_store.add_entry(f"a{i}", f"GROUPA item {i}", "c", "b", status="active")
    for i in range(1, 5):
        wiki_store.add_entry(f"b{i}", f"GROUPB item {i}", "c", "b", status="active")

    result = graph.build_graph(embed_fn=_group_embed_fn, target_cluster_size=4)

    clusters = result["clusters"]
    assert len(clusters) == 2
    member_groups = [set(c["member_ids"]) for c in clusters]
    assert {"a1", "a2", "a3", "a4"} in member_groups
    assert {"b1", "b2", "b3", "b4"} in member_groups

    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    for c in clusters:
        backbones_in_cluster = [m for m in c["member_ids"] if nodes_by_id[m]["is_backbone"]]
        assert len(backbones_in_cluster) == 1
        assert backbones_in_cluster[0] == c["cluster_id"]
        assert all(nodes_by_id[m]["cluster_size"] == c["size"] for m in c["member_ids"])


def _three_group_angle_embed_fn(texts):
    """GROUPA/B/C를 2D 평면에서 0°/40°/90°에 배치 — A-B(cos 0.766), B-C(cos 0.643)는
    threshold(0.3) 이상이라 backbone 엣지가 생기지만 A-C(cos 0)는 안 생겨야 한다."""
    angles = {"groupa": 0, "groupb": 40, "groupc": 90}
    vecs = []
    for t in texts:
        low = t.lower()
        deg = next((d for marker, d in angles.items() if marker in low), None)
        if deg is None:
            vecs.append([0.0, 0.0])
        else:
            rad = np.deg2rad(deg)
            vecs.append([np.cos(rad), np.sin(rad)])
    return np.array(vecs)


def test_build_graph_connects_backbone_nodes_across_clusters(tmp_path, monkeypatch):
    """사용자 요구사항: '큰 주제 노드들이 서로 연결'되어야 한다 — 클러스터
    대표(backbone)끼리도 같은 top-k+threshold 기준으로 엣지가 생겨야 하고,
    threshold 미달인 쌍(A-C)은 안 생겨야 한다."""
    _setup_db(tmp_path, monkeypatch, seed=False)
    for group in ("a", "b", "c"):
        for i in range(1, 5):
            wiki_store.add_entry(f"{group}{i}", f"GROUP{group.upper()} item {i}", "c", "b", status="active")

    result = graph.build_graph(
        embed_fn=_three_group_angle_embed_fn, target_cluster_size=4,
        similarity_threshold=0.3, top_k_similar=2,
    )

    assert len(result["clusters"]) == 3
    backbone_pairs = {
        tuple(sorted((e["source"], e["target"])))
        for e in result["edges"] if e["type"] == "cluster_similar"
    }
    rep_by_group = {c["cluster_id"][0]: c["cluster_id"] for c in result["clusters"]}
    assert tuple(sorted((rep_by_group["a"], rep_by_group["b"]))) in backbone_pairs
    assert tuple(sorted((rep_by_group["b"], rep_by_group["c"]))) in backbone_pairs
    assert tuple(sorted((rep_by_group["a"], rep_by_group["c"]))) not in backbone_pairs


def test_build_graph_similar_edges_stay_within_cluster_when_clustering_active(tmp_path, monkeypatch):
    """스케일링 픽스: similar 엣지를 전체 n×n(O(n²)) 대신 클러스터 내부에서만
    계산하도록 바꿨다. 그룹당 멤버가 4개뿐인데 top_k_similar=5라 같은 그룹
    안에서는 4개밖에 못 채우고, GROUPA-GROUPB의 코사인 유사도(0.766)가
    threshold(0.3)를 넘어 예전(전체비교) 방식이면 5번째 후보로 크로스그룹
    멤버가 골라졌을 상황(실제로 옛 전체비교 로직으로 직접 확인: 14개 발생)인데도,
    클러스터링이 활성화되면(target_cluster_size=5) 같은 클러스터 멤버끼리만
    묶여야 한다 — cluster_similar(대표끼리)는 여전히 크로스클러스터로 존재할 수
    있으니 구분해서 확인."""
    _setup_db(tmp_path, monkeypatch, seed=False)
    for group in ("a", "b", "c"):
        for i in range(1, 6):
            wiki_store.add_entry(f"{group}{i}", f"GROUP{group.upper()} item {i}", "c", "b", status="active")

    result = graph.build_graph(
        embed_fn=_three_group_angle_embed_fn, target_cluster_size=5,
        similarity_threshold=0.3, top_k_similar=5,
    )

    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    similar_edges = [e for e in result["edges"] if e["type"] == "similar"]
    assert similar_edges, "테스트가 의미 있으려면 similar 엣지가 최소 1개는 있어야 함"
    for e in similar_edges:
        assert nodes_by_id[e["source"]]["cluster_id"] == nodes_by_id[e["target"]]["cluster_id"], (
            f"{e['source']}<->{e['target']} similar 엣지가 서로 다른 클러스터를 연결함"
        )


def test_compute_clusters_is_deterministic_across_calls(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, seed=False)
    for i in range(1, 5):
        wiki_store.add_entry(f"a{i}", f"GROUPA item {i}", "c", "b", status="active")
    for i in range(1, 5):
        wiki_store.add_entry(f"b{i}", f"GROUPB item {i}", "c", "b", status="active")

    r1 = graph.build_graph(embed_fn=_group_embed_fn, target_cluster_size=4)
    r2 = graph.build_graph(embed_fn=_group_embed_fn, target_cluster_size=4)

    assign1 = {n["id"]: n["cluster_id"] for n in r1["nodes"]}
    assign2 = {n["id"]: n["cluster_id"] for n in r2["nodes"]}
    assert assign1 == assign2
