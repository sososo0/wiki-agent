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

    monkeypatch.setattr(
        demo_app, "generate",
        lambda query, hits, model=None, hint="general", force_answer=False: {
            "type": "answer", "answer": "stub answer", "entry_ids_used": [],
        },
    )
    monkeypatch.setattr(demo_app, "generate_title", lambda query, model=None: "stub title")
    monkeypatch.setattr(retrieval, "default_embed_fn", _stub_embed_fn)
    monkeypatch.setattr(retrieval, "default_rerank_fn", _stub_rerank_fn)
    # 호출 예산은 모듈 전역(dict)이라 테스트 간에 그대로 남으면 순서/실행 횟수에
    # 따라 한도 초과가 다르게 발생할 수 있다 — 매 테스트 시작 전 깨끗한 상태로
    # 리셋해 격리한다.
    demo_app._daily_budget.update({"date": None, "count": 0})
    demo_app._conv_call_counts.clear()

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


def test_feedback_stores_optional_reason(client):
    client.post("/feedback", json={
        "conv_id": "conv-reason", "turn_id": 0, "thumb": "down", "reason": "근거 부족",
    })

    conn = wiki_store._conn()
    row = conn.execute(
        "SELECT thumb, reason FROM feedback WHERE conv_id=? AND turn_id=?",
        ("conv-reason", 0),
    ).fetchone()
    conn.close()
    assert row["thumb"] == "down"
    assert row["reason"] == "근거 부족"


def test_chat_passes_through_clarify_response_without_extra_call(client, monkeypatch):
    """generate()가 clarify를 반환하면 /chat이 그 모양 그대로 전달해야 한다 —
    추가 LLM 호출 없이 같은 1회 호출 응답을 그대로 쓰는 게 핵심 설계."""
    calls = []

    def _stub_generate(query, hits, model=None, hint="general", force_answer=False):
        calls.append(force_answer)
        return {
            "type": "clarify",
            "question": "어떤 타임아웃을 말씀하시는 건가요?",
            "options": ["connect timeout", "read timeout", "total timeout"],
        }

    monkeypatch.setattr(demo_app, "generate", _stub_generate)

    resp = client.post("/chat", json={
        "conv_id": "conv-clarify", "turn_id": 0, "message": "타임아웃을 어떻게 설정해?",
    })
    body = resp.json()
    assert body["type"] == "clarify"
    assert body["question"] == "어떤 타임아웃을 말씀하시는 건가요?"
    assert body["options"] == ["connect timeout", "read timeout", "total timeout"]
    assert body["cited_entry_ids"] == []
    assert calls == [False]  # force_answer는 클라이언트가 보낸 값 그대로 전달됨

    conn = wiki_store._conn()
    row = conn.execute(
        "SELECT answer FROM conversation_log WHERE conv_id=? AND turn_id=?",
        ("conv-clarify", 0),
    ).fetchone()
    conn.close()
    assert row["answer"] == "어떤 타임아웃을 말씀하시는 건가요?"


def test_chat_forwards_force_answer_flag_to_generate(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        demo_app, "generate",
        lambda query, hits, model=None, hint="general", force_answer=False: (
            calls.append(force_answer) or {"type": "answer", "answer": "ok", "entry_ids_used": []}
        ),
    )
    client.post("/chat", json={
        "conv_id": "conv-force", "turn_id": 0, "message": "x — connect timeout",
        "force_answer": True,
    })
    assert calls == [True]


def test_chat_blocks_llm_call_when_daily_budget_exhausted(client, monkeypatch):
    """일일 한도를 다 쓰면 generate()를 호출하지 않고 바로 안내 메시지를 반환해야
    한다 — 호출이 일어났다면 스텁이 예외를 던져 테스트가 실패한다."""
    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("generate() should not be called when budget is exhausted")

    monkeypatch.setattr(demo_app, "generate", _should_not_be_called)
    monkeypatch.setattr(demo_app, "DAILY_CALL_LIMIT", 0)

    resp = client.post("/chat", json={
        "conv_id": "conv-budget", "turn_id": 0, "message": "any question",
    })
    body = resp.json()
    assert body["answer"] == demo_app.BUDGET_EXCEEDED_MESSAGE
    assert body["type"] == "answer"


def test_chat_blocks_llm_call_when_per_conversation_budget_exhausted(client, monkeypatch):
    monkeypatch.setattr(demo_app, "PER_CONV_CALL_LIMIT", 1)

    first = client.post("/chat", json={
        "conv_id": "conv-percap", "turn_id": 0, "message": "first question",
    })
    assert first.json()["answer"] == "stub answer"

    second = client.post("/chat", json={
        "conv_id": "conv-percap", "turn_id": 1, "message": "second question",
    })
    assert second.json()["answer"] == demo_app.BUDGET_EXCEEDED_MESSAGE

    # 다른 대화는 이 conv_id의 한도와 무관하게 정상 동작해야 한다.
    other = client.post("/chat", json={
        "conv_id": "conv-other", "turn_id": 0, "message": "third question",
    })
    assert other.json()["answer"] == "stub answer"
