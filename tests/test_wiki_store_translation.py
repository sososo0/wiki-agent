"""
wiki-agent / tests / test_wiki_store_translation.py

그래프 화면 표시용 번역 캐시(translation_cache)의 get_translations/set_translation
라운드트립을 검증한다. KB(wiki_entry)와 무관한 파생 캐시라 별도 파일로 분리.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_translation_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)
    return db_path


def test_set_then_get_translation_roundtrip(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    wiki_store.set_translation("wiki_0001", 1, "재시도 백오프 전략", "요약.", "본문")

    result = wiki_store.get_translations(["wiki_0001"])
    assert result["wiki_0001"]["version"] == 1
    assert result["wiki_0001"]["topic"] == "재시도 백오프 전략"
    assert result["wiki_0001"]["canonical"] == "요약."
    assert result["wiki_0001"]["body_md"] == "본문"


def test_get_translations_missing_entry_id_is_absent(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    result = wiki_store.get_translations(["wiki_0001"])
    assert "wiki_0001" not in result


def test_get_translations_empty_list_returns_empty_dict(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    assert wiki_store.get_translations([]) == {}


def test_set_translation_upserts_on_same_entry_and_lang(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    wiki_store.set_translation("wiki_0001", 1, "초안", "초안 요약.", "초안 본문")
    wiki_store.set_translation("wiki_0001", 2, "수정본", "수정 요약.", "수정 본문")

    result = wiki_store.get_translations(["wiki_0001"])
    assert result["wiki_0001"]["version"] == 2
    assert result["wiki_0001"]["topic"] == "수정본"


def test_translations_are_scoped_by_lang(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    wiki_store.set_translation("wiki_0001", 1, "한국어", "한국어 요약.", "한국어 본문", lang="ko")
    wiki_store.set_translation("wiki_0001", 1, "Japanese", "Japanese summary.", "Japanese body", lang="ja")

    ko = wiki_store.get_translations(["wiki_0001"], lang="ko")
    ja = wiki_store.get_translations(["wiki_0001"], lang="ja")
    assert ko["wiki_0001"]["topic"] == "한국어"
    assert ja["wiki_0001"]["topic"] == "Japanese"
