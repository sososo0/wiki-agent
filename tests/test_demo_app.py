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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core import retrieval, wiki_store
from demo import app as demo_app


def _fake_anthropic_client(response_text: str):
    """generate()가 호출하는 _anthropic_client().messages.create(...)를 흉내내는
    가짜 클라이언트 — 모델이 실제로 반환한 raw text를 그대로 흘려보내 JSON
    파싱/폴백 로직을 LLM 호출 없이 검증할 수 있게 한다."""
    class _Block:
        type = "text"
        text = response_text

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            return _Resp()

    class _Client:
        messages = _Messages()

    return _Client()


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
    # LLM 호출 예산(daily/conv/ip_daily)은 이제 매 테스트가 새로 만드는 tmp
    # DB(llm_call_budget 테이블)에 있어서 자동으로 깨끗하게 시작한다 — 리셋 불필요.
    # 버스트 리미터/명확화 대기 상태는 여전히 프로세스 메모리 dict라 직접 리셋해야
    # 한다(TestClient가 매 요청을 같은 고정 IP로 보내므로 안 비우면 테스트끼리
    # 영향을 준다).
    demo_app._ip_burst_log.clear()
    demo_app._pending_clarifications.clear()

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
    """conv-a/conv-b가 같은 브라우저(같은 owner_token)가 만든 것처럼 동일 토큰을
    보내야, 그 토큰으로 /conversations를 조회했을 때 둘 다 보여야 한다."""
    client.post("/chat", json={
        "conv_id": "conv-a", "turn_id": 0, "message": "first question in conv-a",
        "owner_token": "owner-1",
    })
    client.post("/chat", json={
        "conv_id": "conv-b", "turn_id": 0, "message": "first question in conv-b",
        "owner_token": "owner-1",
    })
    client.post("/chat", json={
        "conv_id": "conv-a", "turn_id": 1, "message": "second question in conv-a",
        "owner_token": "owner-1",
    })

    resp = client.get("/conversations", params={"owner_token": "owner-1"})
    assert resp.status_code == 200
    convs = {c["conv_id"]: c for c in resp.json()["conversations"]}
    assert convs["conv-a"]["turn_count"] == 2
    assert convs["conv-a"]["first_query"] == "first question in conv-a"
    assert convs["conv-a"]["title"] == "stub title"
    assert convs["conv-b"]["turn_count"] == 1


def test_conversations_without_owner_token_returns_empty(client):
    """owner_token을 안 보내면(fail-closed) 아무 대화도 보이면 안 된다 — 누구
    것인지 모르는 대화를 아무에게나 보여주지 않기 위함."""
    client.post("/chat", json={
        "conv_id": "conv-c", "turn_id": 0, "message": "first question in conv-c",
        "owner_token": "owner-2",
    })

    resp = client.get("/conversations")
    assert resp.status_code == 200
    assert resp.json() == {"conversations": []}


def test_conversations_does_not_leak_other_owners_conversations(client):
    """owner_token이 다르면 다른 사람의 대화가 안 보여야 한다."""
    client.post("/chat", json={
        "conv_id": "conv-mine", "turn_id": 0, "message": "my question",
        "owner_token": "owner-mine",
    })
    client.post("/chat", json={
        "conv_id": "conv-theirs", "turn_id": 0, "message": "their question",
        "owner_token": "owner-theirs",
    })

    resp = client.get("/conversations", params={"owner_token": "owner-mine"})
    conv_ids = {c["conv_id"] for c in resp.json()["conversations"]}
    assert conv_ids == {"conv-mine"}


def test_history_with_wrong_owner_token_returns_404(client):
    client.post("/chat", json={
        "conv_id": "conv-protected", "turn_id": 0, "message": "secret question",
        "owner_token": "owner-real",
    })

    resp = client.get("/history/conv-protected", params={"owner_token": "owner-wrong"})
    assert resp.status_code == 404


def test_history_with_correct_owner_token_succeeds(client):
    client.post("/chat", json={
        "conv_id": "conv-protected-2", "turn_id": 0, "message": "secret question",
        "owner_token": "owner-real",
    })

    resp = client.get("/history/conv-protected-2", params={"owner_token": "owner-real"})
    assert resp.status_code == 200
    assert len(resp.json()["turns"]) == 1


def test_history_for_legacy_conversation_without_owner_token_still_works(client):
    """owner_token 없이(레거시) 만들어진 대화는 conv_id를 직접 아는 /history
    조회는 여전히 허용된다(기존 동작 보존) — owner_token 미스매치 체크는
    저장된 owner_token이 있을 때만 적용된다."""
    client.post("/chat", json={
        "conv_id": "conv-legacy", "turn_id": 0, "message": "legacy question",
    })

    resp = client.get("/history/conv-legacy", params={"owner_token": "anything-or-nothing"})
    assert resp.status_code == 200
    assert len(resp.json()["turns"]) == 1


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


def test_chat_force_answer_without_pending_state_falls_back_to_new_question(client, monkeypatch):
    """force_answer=True인데 서버에 그 conv_id의 pending 명확화 상태가 없으면
    (예: 만료됐거나 클라이언트가 잘못 보냄) req.message를 그대로 새 질문으로
    처리해야 한다 — force_answer=True를 무비판적으로 generate()에 넘기면 안 됨."""
    calls = []
    monkeypatch.setattr(
        demo_app, "generate",
        lambda query, hits, model=None, hint="general", force_answer=False: (
            calls.append((query, force_answer))
            or {"type": "answer", "answer": "ok", "entry_ids_used": []}
        ),
    )
    client.post("/chat", json={
        "conv_id": "conv-force", "turn_id": 0, "message": "엉뚱한 새 질문",
        "force_answer": True,
    })
    assert calls == [("엉뚱한 새 질문", False)]


def test_chat_real_harness_resumes_pending_clarification_server_side(client, monkeypatch):
    """"진짜 하네스" 핵심 동작: clarify가 나가면 서버가 원본 질문을 pending으로
    저장하고, 사용자가 선택 텍스트만(문자열 조합 없이) 보내면 서버가 원본과
    합쳐 generate()를 호출해야 한다."""
    calls = []

    def _stub_generate(query, hits, model=None, hint="general", force_answer=False):
        calls.append((query, force_answer))
        if not force_answer:
            return {
                "type": "clarify",
                "question": "어떤 타임아웃을 말씀하시는 건가요?",
                "options": ["connect timeout", "read timeout"],
            }
        return {"type": "answer", "answer": "read timeout 설정 방법은...", "entry_ids_used": []}

    monkeypatch.setattr(demo_app, "generate", _stub_generate)

    first = client.post("/chat", json={
        "conv_id": "conv-harness", "turn_id": 0, "message": "타임아웃을 어떻게 설정해?",
    })
    assert first.json()["type"] == "clarify"
    assert "conv-harness" in demo_app._pending_clarifications

    second = client.post("/chat", json={
        # 클라이언트는 선택한 옵션 텍스트만 보낸다 — "원래 질문 — 선택" 조합 없음.
        "conv_id": "conv-harness", "turn_id": 1, "message": "read timeout",
        "force_answer": True,
    })
    assert second.json()["type"] == "answer"
    assert calls[1] == ("타임아웃을 어떻게 설정해? — read timeout", True)
    # pending은 한 번 쓰면 사라져야 한다(같은 명확화에 두 번 답할 수 없음).
    assert "conv-harness" not in demo_app._pending_clarifications


def test_chat_expired_pending_clarification_is_treated_as_new_question(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        demo_app, "generate",
        lambda query, hits, model=None, hint="general", force_answer=False: (
            calls.append((query, force_answer))
            or {"type": "answer", "answer": "ok", "entry_ids_used": []}
        ),
    )
    demo_app._pending_clarifications["conv-expired"] = {
        "query": "원본 질문",
        "created_at": time.time() - demo_app.PENDING_CLARIFY_TTL_SECONDS - 1,
    }

    client.post("/chat", json={
        "conv_id": "conv-expired", "turn_id": 0, "message": "새 답변",
        "force_answer": True,
    })
    assert calls == [("새 답변", False)]


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


def test_generate_falls_back_to_answer_when_model_disobeys_force_answer(monkeypatch):
    """실제로 발견된 버그의 회귀 테스트: force_answer=True인데도 모델이 (haiku가
    "다시 묻지 마라" 지시를 완벽히 따르지 않아) clarify 모양 JSON을 반환하면,
    그 내용을 버리고 "답변이 끊겼습니다"로 대체하던 게 원래 버그였다 — 사용자에게는
    "답변이 끊기고 되묻기도 반영이 안 된다"는 증상 하나로 보였다. question/options를
    답변으로 재구성해 보여줘야 한다."""
    clarify_json = (
        '{"type": "clarify", "question": "어떤 타임아웃을 말씀하시는 건가요?", '
        '"options": ["connect timeout", "read timeout"]}'
    )
    monkeypatch.setattr(
        demo_app, "_anthropic_client", lambda: _fake_anthropic_client(clarify_json),
    )

    result = demo_app.generate(
        "타임아웃을 어떻게 설정해? — read timeout",
        [{"entry_id": "wiki_1", "topic": "t", "canonical": "c"}],
        force_answer=True,
    )

    assert result["type"] == "answer"
    assert "어떤 타임아웃을 말씀하시는 건가요?" in result["answer"]
    assert "connect timeout" in result["answer"]
    assert result["entry_ids_used"] == ["wiki_1"]


def test_generate_returns_clarify_when_model_asks_and_not_forced(monkeypatch):
    clarify_json = (
        '{"type": "clarify", "question": "어떤 타임아웃을 말씀하시는 건가요?", '
        '"options": ["connect timeout", "read timeout"]}'
    )
    monkeypatch.setattr(
        demo_app, "_anthropic_client", lambda: _fake_anthropic_client(clarify_json),
    )

    result = demo_app.generate(
        "타임아웃을 어떻게 설정해?",
        [{"entry_id": "wiki_1", "topic": "t", "canonical": "c"}],
        force_answer=False,
    )

    assert result["type"] == "clarify"
    assert result["question"] == "어떤 타임아웃을 말씀하시는 건가요?"
    assert result["options"] == ["connect timeout", "read timeout"]


def test_generate_keeps_clarify_type_when_model_gives_no_options(monkeypatch):
    """실제로 발견된 버그의 회귀 테스트: 모델이 옵션 없이(빈 배열) clarify를
    반환하면, 조용히 일반 answer로 바꿔치기해서 옵션 UI 전체가 사라지는 게
    원래 버그였다(사용자에게는 "옵션이 안 보임"으로 보임). 옵션이 0개여도
    type은 clarify를 유지해야 한다 — 프런트엔드 자유 입력창은 그래도 쓸 수 있다."""
    clarify_json = (
        '{"type": "clarify", "question": "어떤 타임아웃을 말씀하시는 건가요?", "options": []}'
    )
    monkeypatch.setattr(
        demo_app, "_anthropic_client", lambda: _fake_anthropic_client(clarify_json),
    )

    result = demo_app.generate(
        "타임아웃을 어떻게 설정해?",
        [{"entry_id": "wiki_1", "topic": "t", "canonical": "c"}],
        force_answer=False,
    )

    assert result["type"] == "clarify"
    assert result["question"] == "어떤 타임아웃을 말씀하시는 건가요?"
    assert result["options"] == []


def test_generate_falls_back_to_error_message_when_api_call_raises(monkeypatch):
    """일시적 네트워크/레이트리밋 오류로 Anthropic 호출 자체가 실패해도, 처리
    안 된 500이 사용자에게 노출되는 대신 안내 문구로 폴백해야 한다."""
    class _RaisingMessages:
        def create(self, **kwargs):
            raise RuntimeError("simulated API failure")

    class _RaisingClient:
        messages = _RaisingMessages()

    monkeypatch.setattr(demo_app, "_anthropic_client", lambda: _RaisingClient())

    result = demo_app.generate(
        "질문", [{"entry_id": "wiki_1", "topic": "t", "canonical": "c"}],
    )

    assert result["type"] == "answer"
    assert "오류" in result["answer"]
    assert result["entry_ids_used"] == ["wiki_1"]


def test_generate_title_falls_back_to_truncated_query_when_api_call_raises(monkeypatch):
    class _RaisingMessages:
        def create(self, **kwargs):
            raise RuntimeError("simulated API failure")

    class _RaisingClient:
        messages = _RaisingMessages()

    monkeypatch.setattr(demo_app, "_anthropic_client", lambda: _RaisingClient())

    title = demo_app.generate_title("아주 긴 질문이라고 가정해보자")

    assert title == "아주 긴 질문이라고 가정해보자"[:40]


def test_chat_blocks_llm_call_when_per_ip_daily_budget_exhausted(client, monkeypatch):
    """conv_id를 바꿔도 같은 IP면 IP 일일 한도에 걸려야 한다 — conv_id는
    클라이언트가 임의로 새로 만들 수 있는 값이라, 대화당 한도만으로는
    공격자가 매 요청마다 새 conv_id를 보내 한도를 우회할 수 있다."""
    monkeypatch.setattr(demo_app, "PER_IP_DAILY_CALL_LIMIT", 1)

    first = client.post("/chat", json={
        "conv_id": "conv-ip-1", "turn_id": 0, "message": "first question",
    })
    assert first.json()["answer"] == "stub answer"

    second = client.post("/chat", json={
        "conv_id": "conv-ip-2", "turn_id": 0, "message": "second question, new conv_id",
    })
    assert second.json()["answer"] == demo_app.BUDGET_EXCEEDED_MESSAGE


def test_burst_limit_blocks_rapid_requests_across_any_endpoint(client, monkeypatch):
    """버스트 한도는 /chat뿐 아니라 모든 엔드포인트에 미들웨어로 걸려야 한다 —
    검색/그래프 연산도 서버 CPU 비용이 있어서 LLM 호출 여부와 무관하게 막아야
    하기 때문."""
    monkeypatch.setattr(demo_app, "BURST_LIMIT", 2)

    ok1 = client.get("/history/some-conv")
    ok2 = client.get("/history/some-conv")
    blocked = client.get("/history/some-conv")

    assert ok1.status_code == 200
    assert ok2.status_code == 200
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_chat_rejects_oversized_message(client):
    resp = client.post("/chat", json={
        "conv_id": "conv-toolong", "turn_id": 0, "message": "x" * 2001,
    })
    assert resp.status_code == 422


def test_chat_rejects_malformed_conv_id(client):
    resp = client.post("/chat", json={
        "conv_id": "<script>alert(1)</script>", "turn_id": 0, "message": "hello",
    })
    assert resp.status_code == 422


def test_feedback_rejects_invalid_thumb_value(client):
    resp = client.post("/feedback", json={
        "conv_id": "conv-1", "turn_id": 0, "thumb": "sideways",
    })
    assert resp.status_code == 422


def test_notifications_endpoint_lists_unread_count(client):
    """notifications 테이블에는 데모(/chat 등)가 아니라 갱신 사이클(신뢰된
    오프라인 스크립트)만 쓴다 — 여기서는 wiki_store.add_notification을 직접
    호출해 그 결과를 GET /notifications가 읽기만 하는 경로를 검증한다."""
    wiki_store.add_notification("info", "사이클 완료", "gap 0개 발견")
    wiki_store.add_notification("warning", "회귀로 승격 차단됨", "shadow 1개가 active로 못 감")

    resp = client.get("/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unread_count"] == 2
    assert len(body["notifications"]) == 2
    assert body["notifications"][0]["title"] == "회귀로 승격 차단됨"  # 최신순


def test_cycle_history_endpoint_lists_rows_in_chronological_order(client):
    """cycle_history도 notifications와 같은 이유로 신뢰된 오프라인 스크립트만
    쓰고 GET /cycle-history는 읽기만 한다 — 단, 추이 차트용이라 최신순이 아니라
    시간순(ts ASC)으로 나와야 한다."""
    wiki_store.add_cycle_history(
        mined=1, shadow_count=1, promoted=False, activated_count=0,
        recall_at_k=0.5, mrr=0.4, correctness=0.3,
    )
    wiki_store.add_cycle_history(
        mined=2, shadow_count=1, promoted=True, activated_count=1,
        recall_at_k=0.9, mrr=0.8, correctness=0.7, escalation_correctness=1.0,
    )

    resp = client.get("/cycle-history")
    assert resp.status_code == 200
    cycles = resp.json()["cycles"]
    assert len(cycles) == 2
    assert cycles[0]["recall_at_k"] == 0.5  # 시간순 첫 번째
    assert cycles[1]["promoted"] == 1
    assert cycles[1]["escalation_correctness"] == 1.0


def test_notifications_mark_read_decrements_unread_count(client):
    wiki_store.add_notification("info", "사이클 완료", "gap 0개 발견")
    notif_id = wiki_store.list_notifications()[0]["id"]

    resp = client.post(f"/notifications/{notif_id}/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    body = client.get("/notifications").json()
    assert body["unread_count"] == 0
    assert body["notifications"][0]["read"] == 1


def test_notifications_read_blocked_without_admin_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(demo_app, "ADMIN_TOKEN", "secret")
    wiki_store.add_notification("info", "사이클 완료", "gap 0개 발견")
    notif_id = wiki_store.list_notifications()[0]["id"]

    resp = client.post(f"/notifications/{notif_id}/read")
    assert resp.status_code == 403
    assert wiki_store.list_notifications()[0]["read"] == 0  # 실제로 안 바뀌어야 함


def test_notifications_read_allowed_with_correct_admin_token(client, monkeypatch):
    monkeypatch.setattr(demo_app, "ADMIN_TOKEN", "secret")
    wiki_store.add_notification("info", "사이클 완료", "gap 0개 발견")
    notif_id = wiki_store.list_notifications()[0]["id"]

    resp = client.post(
        f"/notifications/{notif_id}/read", headers={"X-Admin-Token": "secret"})
    assert resp.status_code == 200
    assert wiki_store.list_notifications()[0]["read"] == 1


def test_consume_call_budget_counters_are_shared_via_db_not_process_memory(client):
    """daily/conv/ip_daily 카운터가 demo.app 모듈의 in-memory dict가 아니라
    wiki_store(DB)에 있어야 한다 — 여러 워커 프로세스가 떠도 한도가 공유되는
    근거. 직접 wiki_store 함수로 조회해 /chat 호출 결과와 일치하는지 확인."""
    client.post("/chat", json={"conv_id": "conv-budget", "turn_id": 0, "message": "first question"})
    client.post("/chat", json={"conv_id": "conv-budget", "turn_id": 1, "message": "second question"})

    # turn_id=0은 generate() 1회 + 제목 생성 백그라운드 태스크 1회 = 2건,
    # turn_id=1은 generate() 1회 -> 합계 3건.
    assert wiki_store.get_call_budget_counter("conv", "conv-budget") == 3
    today = time.strftime("%Y-%m-%d")
    assert wiki_store.get_call_budget_counter("daily", today) >= 3
