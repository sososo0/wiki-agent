"""
wiki-agent / tests / test_pipeline_dedupe.py

resolve_doc_chunk_op의 skip_rejected/skip/create/update(shadow)/
update(active+supersedes) 분기를 검증. DB 없음(딕셔너리만 사용).

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import dedupe
from core.pipeline.curate import doc_chunk_entry_id

CANDIDATE = {
    "type": "doc_chunk",
    "doc_path": "docs/retry.md",
    "doc_hash": "deadbeef",
    "chunk_index": 0,
    "heading_path": ["Retries"],
    "text": "some text",
    "chunk_hash": "hash_v1",
}

BASE_ENTRY_ID = doc_chunk_entry_id(CANDIDATE)


def test_create_when_no_existing_entry():
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, {})
    assert result == {"op": "create", "entry_id": BASE_ENTRY_ID, "supersedes": None}


def test_skip_when_chunk_hash_unchanged():
    existing = {
        BASE_ENTRY_ID: {
            "status": "active", "version": 1,
            "sources": [{"type": "document", "chunk_hash": "hash_v1"}],
        }
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result == {"op": "skip", "entry_id": BASE_ENTRY_ID, "supersedes": None}


def test_update_same_entry_id_when_existing_is_shadow():
    existing = {
        BASE_ENTRY_ID: {
            "status": "shadow", "version": 1,
            "sources": [{"type": "document", "chunk_hash": "hash_old"}],
        }
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result == {"op": "update", "entry_id": BASE_ENTRY_ID, "supersedes": None}


def test_update_new_entry_id_with_supersedes_when_existing_is_active():
    existing = {
        BASE_ENTRY_ID: {
            "status": "active", "version": 3,
            "sources": [{"type": "document", "chunk_hash": "hash_old"}],
        }
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result == {
        "op": "update", "entry_id": f"{BASE_ENTRY_ID}_v4", "supersedes": BASE_ENTRY_ID,
    }


def test_update_treats_deprecated_like_shadow():
    existing = {
        BASE_ENTRY_ID: {
            "status": "deprecated", "version": 2,
            "sources": [{"type": "document", "chunk_hash": "hash_old"}],
        }
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result == {"op": "update", "entry_id": BASE_ENTRY_ID, "supersedes": None}


def test_skip_check_ignores_entries_without_chunk_hash():
    """seed/로그 마이닝 엔트리는 chunk_hash가 없는 source라 None과 비교되어
    절대 우연히 skip되지 않아야 한다(다른 candidate의 chunk_hash와 None은 다름)."""
    existing = {
        BASE_ENTRY_ID: {
            "status": "active", "version": 1,
            "sources": [{"type": "retrieval_log_query", "query": "x"}],
        }
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result["op"] == "update"


def test_rejected_entry_id_is_content_addressed_and_distinct_from_base():
    other = {**CANDIDATE, "chunk_hash": "hash_v2"}
    assert dedupe.rejected_entry_id(CANDIDATE) != dedupe.rejected_entry_id(other)
    assert dedupe.rejected_entry_id(CANDIDATE) != BASE_ENTRY_ID


def test_skip_rejected_when_same_chunk_hash_was_previously_rejected():
    existing = {
        dedupe.rejected_entry_id(CANDIDATE): {
            "status": "rejected", "version": 1,
            "sources": [{"chunk_hash": "hash_v1"}],
        }
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result == {"op": "skip_rejected", "entry_id": BASE_ENTRY_ID, "supersedes": None}


def test_rejected_record_does_not_block_retry_after_content_changes():
    """같은 base_entry_id라도 chunk_hash가 다르면(문서가 수정됨) 거부 기록과
    매칭되지 않아 정상적으로 create/update로 처리되어야 한다."""
    existing = {
        dedupe.rejected_entry_id(CANDIDATE): {
            "status": "rejected", "version": 1,
            "sources": [{"chunk_hash": "hash_v1"}],
        }
    }
    changed = {**CANDIDATE, "chunk_hash": "hash_v2"}
    result = dedupe.resolve_doc_chunk_op(changed, existing)
    assert result["op"] == "create"


def test_rejected_check_takes_priority_over_active_supersedes_branch():
    """active 콘텐츠가 바뀌어 새 버전이 거부된 적이 있으면(주소는 chunk_hash
    기반이라 버전 번호와 무관하게 안정적), 같은 변경 내용을 다시 만나도
    update(supersedes)로 또 LLM을 호출하지 않고 skip_rejected로 막아야 한다."""
    existing = {
        BASE_ENTRY_ID: {
            "status": "active", "version": 3,
            "sources": [{"type": "document", "chunk_hash": "hash_old"}],
        },
        dedupe.rejected_entry_id(CANDIDATE): {
            "status": "rejected", "version": 1,
            "sources": [{"chunk_hash": "hash_v1"}],
        },
    }
    result = dedupe.resolve_doc_chunk_op(CANDIDATE, existing)
    assert result["op"] == "skip_rejected"
