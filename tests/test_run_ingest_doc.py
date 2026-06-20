"""
wiki-agent / tests / test_run_ingest_doc.py

scripts/ingest_doc.py의 통합 테스트(tmp DB + tmp 마크다운 파일). 증명할 것:
1) 정상 문서 -> shadow 엔트리 생성, llm_fn/judge_fn/evaluate_fn 전부 stub.
2) 동일 입력 재실행 -> chunk_hash 불변이므로 llm 호출 0회, shadow_written == [].
3) 문서 일부 수정 후 재실행 -> 바뀐 섹션만 새 candidate가 생겨 처리됨.
4) 실패 파일(읽기 불가/너무 큰 파일)이 섞여도 나머지 파일은 정상 처리됨.
5) 게이트가 거부한 청크는 콘텐츠가 안 바뀌면 재실행 시 LLM을 다시 호출하지
   않음(skip_rejected) — 문서가 바뀌면 다시 시도됨.

test_run_update_cycle.py와 동일 패턴: tmp DB + 전부 stub 주입으로 오프라인 실행.

실행: pytest
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store
from core.pipeline import parse as parse_module
from scripts.ingest_doc import run_doc_ingest

DOC_TEXT = """# Retries

Always retry transient failures such as timeouts and 5xx responses with
exponential backoff and random jitter, capping the total number of attempts.

# Rate limiting

Use a token bucket that refills at a fixed rate to limit requests per client
key, returning HTTP 429 with a Retry-After header once the bucket is empty.
"""


def _stub_llm_fn(heading_path, text):
    heading = " ".join(heading_path) if heading_path else "doc"
    return {
        "topic": heading,
        "canonical": text.strip().splitlines()[0] if text.strip() else heading,
        "body_md": text.strip(),
    }


def _stub_judge_fn(patch, existing_entries):
    return 1.0, "ok"


def _stub_evaluate_fn(retriever, gold, k=5):
    """promote 단계가 항상 무회귀로 보고하게 해 shadow가 active로 커밋되게 한다."""
    return {"recall@k": 0.9, "mrr": 0.8, "correctness": 0.7}


def _init_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_ingest_doc_wiki.db")
    monkeypatch.setenv("WIKI_AGENT_DB", db_path)
    wiki_store.DB_PATH = db_path
    wiki_store.init_db(seed=True)


def test_ingest_creates_shadow_for_new_doc(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    result = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    assert result["parsed_files"] == [str(doc_path)]
    assert result["failed_files"] == []
    assert result["llm_calls"] == 2
    assert len(result["shadow_written"]) == 2
    assert result["rejected"] == []
    assert result["promote"]["promoted"] is True

    active_ids = {e["entry_id"] for e in wiki_store.list_active_entries()}
    for eid in result["shadow_written"]:
        assert eid in active_ids


def test_ingest_is_idempotent_on_unchanged_doc(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    result = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    assert result["llm_calls"] == 0
    assert result["shadow_written"] == []
    assert result["skipped_chunks"] == 2


def test_ingest_reprocesses_only_changed_section(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    first = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )
    first_ids = set(first["shadow_written"])

    changed_text = DOC_TEXT.replace(
        "key, returning HTTP 429",
        "key (per-IP fallback when no key is present), returning HTTP 429",
    )
    doc_path.write_text(changed_text, encoding="utf-8")

    result = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    assert result["llm_calls"] == 1
    assert result["skipped_chunks"] == 1
    assert len(result["shadow_written"]) == 1
    # 변경된 청크는 이미 active였던 엔트리를 superseded하므로 base entry_id에
    # "_v" 접미사가 붙은 새 entry_id로 처리된다.
    assert result["shadow_written"][0] not in first_ids


def test_ingest_isolates_failed_files(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    good_path = tmp_path / "good.md"
    good_path.write_text(DOC_TEXT, encoding="utf-8")
    bad_path = tmp_path / "bad.md"
    bad_path.write_text("# Heading\n\nSome content here that is long enough.\n", encoding="utf-8")

    original_getsize = parse_module.os.path.getsize

    def _fake_getsize(path):
        if str(path) == str(bad_path):
            return parse_module.MAX_FILE_BYTES + 1
        return original_getsize(path)

    monkeypatch.setattr(parse_module.os.path, "getsize", _fake_getsize)

    result = run_doc_ingest(
        [str(tmp_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    assert result["parsed_files"] == [str(good_path)]
    assert len(result["failed_files"]) == 1
    assert result["failed_files"][0]["path"] == str(bad_path)
    assert len(result["shadow_written"]) == 2


def _stub_judge_fn_rejects_rate_limiting(patch, existing_entries):
    if "rate limiting" in patch["topic"].lower():
        return 0.1, "rejected for test"
    return 1.0, "ok"


def test_ingest_does_not_recurate_unchanged_rejected_chunk(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    first = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn_rejects_rate_limiting,
        evaluate_fn=_stub_evaluate_fn,
    )
    assert len(first["rejected"]) == 1
    assert first["llm_calls"] == 2

    second = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn_rejects_rate_limiting,
        evaluate_fn=_stub_evaluate_fn,
    )

    # Retries 섹션은 이미 active(skip), Rate limiting 섹션은 이전에 거부된 동일
    # 콘텐츠라 skip_rejected -> 어느 쪽도 LLM을 다시 호출하지 않아야 한다.
    assert second["llm_calls"] == 0
    assert second["skipped_chunks"] == 1
    assert second["skipped_rejected_chunks"] == 1
    assert second["rejected"] == []
    assert second["shadow_written"] == []


def test_ingest_retries_rejected_chunk_after_content_changes(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn_rejects_rate_limiting,
        evaluate_fn=_stub_evaluate_fn,
    )

    changed_text = DOC_TEXT.replace(
        "key, returning HTTP 429",
        "key (per-IP fallback when no key is present), returning HTTP 429",
    )
    doc_path.write_text(changed_text, encoding="utf-8")

    result = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn_rejects_rate_limiting,
        evaluate_fn=_stub_evaluate_fn,
    )

    assert result["llm_calls"] == 1
    assert result["skipped_rejected_chunks"] == 0
    assert len(result["rejected"]) == 1
