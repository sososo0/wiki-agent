"""
wiki-agent / tests / test_demo_app.py

demo/app.py (FastAPI 채팅 백엔드)의 /chat, /feedback 엔드포인트를 tmp DB +
스텁 generate()로 검증한다. 실제 Anthropic 호출/임베딩 모델 로딩 없이
오프라인으로 빠르게 실행된다(search_wiki 자체는 core/retrieval.py의
embed_fn/rerank_fn을 스텁 주입할 수 없으므로 여기서는 demo.app.generate만
스텁한다 — search_wiki 동작은 기존 retrieval 테스트에서 이미 검증됨).

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core import retrieval, wiki_store
from demo import app as demo_app


def _stub_embed_fn(texts):
    return np.ones((len(texts), 4), dtype=float)


def _stub_rerank_fn(query, texts):
    return [1.0] * len(texts)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_demo_app_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)

    monkeypatch.setattr(demo_app, "generate", lambda query, hits, model=None: "stub answer")
    monkeypatch.setattr(demo_app, "generate_title", lambda query, model=None: "stub title")
    monkeypatch.setattr(retrieval, "default_embed_fn", _stub_embed_fn)
    monkeypatch.setattr(retrieval, "default_rerank_fn", _stub_rerank_fn)

    with TestClient(demo_app.app) as c:
        yield c


def test_chat_returns_answer_and_logs_turn(client):
    resp = client.post("/chat", json={
        "conv_id": "conv-1", "turn_id": 0, "message": "how do I retry failed requests",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub answer"
    assert isinstance(body["retrieved"], list) and len(body["retrieved"]) > 0

    conn = wiki_store._conn()
    row = conn.execute(
        "SELECT query, answer FROM conversation_log WHERE conv_id=? AND turn_id=?",
        ("conv-1", 0),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["answer"] == "stub answer"


def test_history_returns_logged_turns_in_order(client):
    client.post("/chat", json={
        "conv_id": "conv-hist", "turn_id": 0, "message": "how do I retry failed requests",
    })
    client.post("/chat", json={
        "conv_id": "conv-hist", "turn_id": 1, "message": "what about rate limiting",
    })

    resp = client.get("/history/conv-hist")
    assert resp.status_code == 200
    turns = resp.json()["turns"]
    assert [t["turn_id"] for t in turns] == [0, 1]
    assert turns[0]["query"] == "how do I retry failed requests"
    assert turns[0]["answer"] == "stub answer"
    assert isinstance(turns[0]["retrieved"], list) and len(turns[0]["retrieved"]) > 0
    assert isinstance(turns[0]["retrieved"][0], str)


def test_history_unknown_conv_id_returns_empty(client):
    resp = client.get("/history/does-not-exist")
    assert resp.status_code == 200
    assert resp.json() == {"turns": []}


def test_conversations_lists_past_conversations_with_preview(client):
    client.post("/chat", json={
        "conv_id": "conv-a", "turn_id": 0, "message": "first question in conv-a",
    })
    client.post("/chat", json={
        "conv_id": "conv-b", "turn_id": 0, "message": "first question in conv-b",
    })
    client.post("/chat", json={
        "conv_id": "conv-a", "turn_id": 1, "message": "second question in conv-a",
    })

    resp = client.get("/conversations")
    assert resp.status_code == 200
    convs = {c["conv_id"]: c for c in resp.json()["conversations"]}
    assert convs["conv-a"]["turn_count"] == 2
    assert convs["conv-a"]["first_query"] == "first question in conv-a"
    assert convs["conv-a"]["title"] == "stub title"
    assert convs["conv-b"]["turn_count"] == 1


def test_chat_first_turn_sets_conversation_title(client):
    client.post("/chat", json={
        "conv_id": "conv-title", "turn_id": 0, "message": "how do I retry failed requests",
    })

    conn = wiki_store._conn()
    row = conn.execute(
        "SELECT title FROM conversation_meta WHERE conv_id=?", ("conv-title",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["title"] == "stub title"


def test_feedback_writes_feedback_row(client):
    resp = client.post("/feedback", json={"conv_id": "conv-1", "turn_id": 0, "thumb": "up"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    conn = wiki_store._conn()
    row = conn.execute(
        "SELECT thumb FROM feedback WHERE conv_id=? AND turn_id=?", ("conv-1", 0),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["thumb"] == "up"
