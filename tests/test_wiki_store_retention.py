"""
wiki-agent / tests / test_wiki_store_retention.py

core/wiki_store.purge_old_logs()가 retention_days보다 오래된 retrieval_log/feedback
행만 지우고, 그보다 최근 행과 conversation_log는 그대로 두는지 검증한다.

실행: pytest
"""

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_retention_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def _insert_retrieval_log(db_path, ts):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO retrieval_log (query, retrieved, ts) VALUES (?,?,?)",
                 ("q", "[]", ts))
    conn.commit()
    conn.close()


def _insert_feedback(db_path, ts):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO feedback (conv_id, turn_id, thumb, reason, ts) VALUES (?,?,?,?,?)",
                 ("c1", 0, "down", None, ts))
    conn.commit()
    conn.close()


def _insert_conversation_log(db_path, ts):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO conversation_log (conv_id, turn_id, query, answer, retrieved, ts) "
        "VALUES (?,?,?,?,?,?)",
        ("c1", 0, "q", "a", "[]", ts))
    conn.commit()
    conn.close()


def test_purge_deletes_only_rows_older_than_retention_days(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)
    now = time.time()
    old_ts = now - 40 * 86400   # 40일 전 -> 삭제 대상
    recent_ts = now - 5 * 86400  # 5일 전 -> 보존 대상

    _insert_retrieval_log(db_path, old_ts)
    _insert_retrieval_log(db_path, recent_ts)
    _insert_feedback(db_path, old_ts)
    _insert_feedback(db_path, recent_ts)

    result = wiki_store.purge_old_logs(retention_days=30)

    assert result == {"retrieval_log_deleted": 1, "feedback_deleted": 1}
    remaining_retrieval = wiki_store.list_retrieval_log()
    remaining_feedback = wiki_store.list_feedback()
    assert len(remaining_retrieval) == 1
    assert remaining_retrieval[0]["ts"] == recent_ts
    assert len(remaining_feedback) == 1
    assert remaining_feedback[0]["ts"] == recent_ts


def test_purge_does_not_touch_conversation_log(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)
    old_ts = time.time() - 100 * 86400
    _insert_conversation_log(db_path, old_ts)

    wiki_store.purge_old_logs(retention_days=30)

    assert len(wiki_store.list_conversation("c1")) == 1


def test_purge_with_no_old_rows_deletes_nothing(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)
    _insert_retrieval_log(db_path, time.time())

    result = wiki_store.purge_old_logs(retention_days=30)

    assert result == {"retrieval_log_deleted": 0, "feedback_deleted": 0}
    assert len(wiki_store.list_retrieval_log()) == 1
