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
from scripts.run_update_cycle import run_cycle

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


def _stub_judge_fn(patch):
    """password 관련 patch만 그라운딩 통과시키고, 나머지는 낮은 점수로 차단."""
    return 1.0 if "password" in patch["topic"].lower() else 0.1


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
    assert "failed grounding/contradiction check" in rejected_reasons
    assert not any("account" in eid or "delet" in eid for eid in shadow_ids), \
        f"bad (account deletion) patch should never have been written as shadow: {shadow_ids}"

    # promote는 일부러 회귀로 평가되므로 커밋되지 않아야 shadow 상태가 보존된다.
    assert result["promote"]["promoted"] is False
