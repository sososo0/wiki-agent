"""
wiki-agent / tests / test_wiki_store_backup.py

core/wiki_store.backup_db()가 Online Backup API로 현재 DB 내용을 dest_path에
그대로 복제하는지 검증한다(seed 엔트리 + 그 이후 변경분 모두 반영).

실행: pytest
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_backup_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def test_backup_db_replicates_seed_entries(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    dest_path = str(tmp_path / "backup.db")

    wiki_store.backup_db(dest_path)

    dest_conn = sqlite3.connect(dest_path)
    count = dest_conn.execute("SELECT COUNT(*) FROM wiki_entry").fetchone()[0]
    dest_conn.close()
    assert count == len(wiki_store.list_active_entries())


def test_backup_db_reflects_changes_made_after_init(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    wiki_store.add_entry("wiki_9001", "New topic", "New canonical.", "New body",
                          status="active")
    dest_path = str(tmp_path / "backup.db")

    wiki_store.backup_db(dest_path)

    dest_conn = sqlite3.connect(dest_path)
    row = dest_conn.execute(
        "SELECT topic FROM wiki_entry WHERE entry_id = ?", ("wiki_9001",)).fetchone()
    dest_conn.close()
    assert row is not None
    assert row[0] == "New topic"


def test_backup_db_does_not_modify_source_db(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)
    dest_path = str(tmp_path / "backup.db")
    before = len(wiki_store.list_active_entries())

    wiki_store.backup_db(dest_path)

    assert len(wiki_store.list_active_entries()) == before
    assert Path(db_path).exists()
