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
