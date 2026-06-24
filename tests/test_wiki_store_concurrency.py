"""
wiki-agent / tests / test_wiki_store_concurrency.py

core/wiki_store._conn()/init_db()가 WAL 모드 + busy_timeout을 켜는지 검증한다.
동시 쓰기가 늘어날 때 "database is locked"로 즉시 죽는 대신 WAL(읽기-쓰기 비차단)
+ busy_timeout(쓰기끼리 겹쳐도 재시도)으로 완화하기 위한 설정.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_concurrency_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def test_init_db_enables_wal_journal_mode(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    conn = wiki_store._conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()

    assert mode == "wal"


def test_new_connections_have_busy_timeout_set(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    conn = wiki_store._conn()
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()

    assert timeout_ms == 5000
