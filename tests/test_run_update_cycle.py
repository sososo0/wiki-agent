"""
wiki-agent / tests / test_run_update_cycle.py

scripts/run_update_cycle.py의 1사이클 통합 테스트(tmp DB). 더미
retrieval_log/feedback을 직접 적재해 두 가지를 한 번에 증명한다:
1) 좋은 patch -> shadow 엔트리가 실제로 생성됨.
2) 나쁜 patch(그라운딩 실패) -> 게이트가 막아 shadow로 쓰이지 않음.

llm_fn/judge_fn/evaluate_fn을 전부 스텁 주입해 LLM/ML 모델 호출 없이
오프라인으로 빠르게 실행된다. evaluate_fn은 일부러 회귀를 보고하게 해
promote가 커밋하지 않도록 만들어, 생성된 shadow 엔트리가 그대로
status='shadow'로 남아 있는 상태를 검증할 수 있게 한다.

실행: pytest
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store
from scripts.run_update_cycle import run_cycle, summary_notifications

PASSWORD_QUERY = "how do I reset my password"
DELETE_QUERY = "how do I delete my account"


def _seed_retrieval_log(query, n, score):
    conn = wiki_store._conn()
    for _ in range(n):
        conn.execute(
            "INSERT INTO retrieval_log (query, retrieved, ts) VALUES (?,?,?)",
            (query, json.dumps([{"entry_id": "wiki_0001", "score": score}]), time.time()),
        )
    conn.commit()
    conn.close()


def _stub_llm_fn(query_examples):
    if any("password" in q.lower() for q in query_examples):
        return {"topic": "Password reset", "canonical": "Go to settings and reset your password.",
                "body_md": "Account > Security > Reset password."}
    return {"topic": "Account deletion", "canonical": "Contact support to delete your account.",
            "body_md": "Email support@example.com."}


def _stub_judge_fn(patch, existing_entries):
    """password 관련 patch만 그라운딩 통과시키고, 나머지는 낮은 점수로 차단."""
    if "password" in patch["topic"].lower():
        return 1.0, "ok"
    return 0.1, "hallucination risk: unverifiable claim"


def _stub_evaluate_fn(retriever, gold, k=5):
    """promote 단계가 항상 회귀로 보고하게 해 shadow 상태가 그대로 보존되게 한다."""
    if retriever is wiki_store.search_wiki:
        return {"recall@k": 0.9, "mrr": 0.8, "correctness": 0.7}
    return {"recall@k": 0.1, "mrr": 0.1, "correctness": 0.1}


def test_run_cycle_creates_shadow_for_good_patch_and_blocks_bad_patch(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_run_cycle_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)

    _seed_retrieval_log(PASSWORD_QUERY, n=4, score=-5.0)
    _seed_retrieval_log(DELETE_QUERY, n=4, score=-5.0)

    result = run_cycle(
        gold_path=None, k=5,
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    assert result["mined"] == 2

    shadow_ids = {e["entry_id"] for e in wiki_store.list_shadow_entries()}
    password_shadow_ids = {eid for eid in shadow_ids if "password" in eid or "reset" in eid}
    assert len(password_shadow_ids) == 1, f"expected one password-related shadow entry, got {shadow_ids}"

    rejected_reasons = {r.get("reason") for r in result["rejected"]}
    assert any(r.startswith("failed grounding/contradiction check") for r in rejected_reasons)
    assert not any("account" in eid or "delet" in eid for eid in shadow_ids), \
        f"bad (account deletion) patch should never have been written as shadow: {shadow_ids}"

    # promote는 일부러 회귀로 평가되므로 커밋되지 않아야 shadow 상태가 보존된다.
    assert result["promote"]["promoted"] is False

    # 이번 사이클에서 shadow가 생겼는데도 승격이 안 됐으니, 종모양 알림 UI가
    # 보여줄 "회귀로 승격 차단됨" 경고가 떠 있어야 한다(실제 DB에 쓰였는지까지).
    levels = {n["level"] for n in wiki_store.list_notifications()}
    assert "warning" in levels


def _base_summary(**overrides):
    summary = {
        "mined": 0,
        "feedback": {"n": 0, "down": 0, "down_rate": 0.0},
        "shadow_written": [],
        "promote": {"promoted": True, "activated_entry_ids": []},
    }
    summary.update(overrides)
    return summary


def test_summary_notifications_always_includes_one_info_summary():
    notes = summary_notifications(_base_summary())
    assert len(notes) == 1
    assert notes[0][0] == "info"


def test_summary_notifications_warns_on_high_down_rate():
    summary = _base_summary(feedback={"n": 10, "down": 8, "down_rate": 0.8})
    notes = summary_notifications(summary)
    levels = [n[0] for n in notes]
    assert "warning" in levels
    assert any("부정" in n[1] for n in notes if n[0] == "warning")


def test_summary_notifications_does_not_warn_on_high_down_rate_with_too_few_samples():
    """down_rate가 높아도 표본이 너무 적으면(n<5) 노이즈일 뿐이라 경고하지 않는다."""
    summary = _base_summary(feedback={"n": 2, "down": 2, "down_rate": 1.0})
    notes = summary_notifications(summary)
    assert [n[0] for n in notes] == ["info"]


def test_summary_notifications_warns_when_shadow_written_but_not_promoted():
    summary = _base_summary(
        shadow_written=["wiki_gap_x"],
        promote={"promoted": False, "activated_entry_ids": []},
    )
    notes = summary_notifications(summary)
    levels = [n[0] for n in notes]
    assert "warning" in levels
    assert any("회귀" in n[1] for n in notes if n[0] == "warning")


def test_summary_notifications_no_warning_when_nothing_mined_and_not_promoted():
    """mine된 게 없으면 promoted=False는 그냥 "할 일 없었음"이라 경고가 아니다."""
    summary = _base_summary(promote={"promoted": False, "activated_entry_ids": []})
    notes = summary_notifications(summary)
    assert [n[0] for n in notes] == ["info"]


def test_run_cycle_writes_error_notification_and_reraises_on_crash(tmp_path, monkeypatch):
    """사이클이 예외로 죽어도 종모양 알림에 남아야 한다 — 삼키지 않고
    그대로 재raise해 hermes cron 자체의 실패 상태도 정상적으로 남아야 함."""
    import pytest

    db_path = str(tmp_path / "test_run_cycle_crash_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)

    from scripts import run_update_cycle

    def _broken_run_cycle(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_update_cycle, "run_cycle", _broken_run_cycle)
    monkeypatch.setattr(sys, "argv", ["run_update_cycle.py"])

    with pytest.raises(RuntimeError, match="boom"):
        run_update_cycle.main()

    notes = wiki_store.list_notifications()
    assert len(notes) == 1
    assert notes[0]["level"] == "error"
    assert "boom" in notes[0]["message"]
