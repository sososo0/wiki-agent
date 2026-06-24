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


def test_dry_run_reports_create_ops_without_calling_llm_or_writing_db(tmp_path, monkeypatch):
    """dry_run=True면 어떤 청크가 create/update/skip 대상인지만 보고하고,
    curate(LLM)/gate(judge LLM)/wiki_store.add_entry/promote(eval LLM) 전부
    건너뛰어야 한다 — 비용 없이 미리 점검할 수 있어야 하는 게 핵심."""
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("dry_run인데 LLM 호출 경로가 실행됨")

    active_before = wiki_store.list_active_entries()  # init_db(seed=True)의 시드 5개

    result = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_should_not_be_called, judge_fn=_should_not_be_called,
        evaluate_fn=_should_not_be_called,
        dry_run=True,
    )

    assert result["llm_calls"] == 0
    assert result["shadow_written"] == []
    assert result["promote"] is None
    assert len(result["would_curate"]) == 2
    assert {c["op"] for c in result["would_curate"]} == {"create"}
    assert wiki_store.list_active_entries() == active_before  # 시드 외 새 엔트리 없음
    assert wiki_store.list_shadow_entries() == []


def test_dry_run_still_reports_skip_for_unchanged_chunks(tmp_path, monkeypatch):
    """이미 처리된(콘텐츠 불변) 청크는 dry_run에서도 skip으로 잡혀야 한다 —
    dedupe 분류 자체는 평소와 동일하게 동작해야 의미가 있다."""
    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )

    result = run_doc_ingest([str(doc_path)], dry_run=True)

    assert result["would_curate"] == []
    assert result["skipped_chunks"] == 2


def test_near_duplicate_check_skips_recuration_for_minor_edit(tmp_path, monkeypatch):
    """근접 중복 체크(옵트인)를 켜면, chunk_hash가 달라도 기존 엔트리와 임베딩이
    거의 같으면(오탈자/문구 미세 수정 흉내) LLM을 다시 호출하지 않고 그대로
    넘어가야 한다 — 기존 콘텐츠도 안 바뀌어야 한다."""
    import numpy as np

    _init_db(tmp_path, monkeypatch)
    doc_path = tmp_path / "doc.md"
    doc_path.write_text(DOC_TEXT, encoding="utf-8")

    run_doc_ingest(
        [str(doc_path)],
        llm_fn=_stub_llm_fn, judge_fn=_stub_judge_fn, evaluate_fn=_stub_evaluate_fn,
    )
    retries_entry = next(
        e for e in wiki_store.list_active_entries() if e["topic"] == "Retries")
    wiki_store.set_embedding(retries_entry["entry_id"], retries_entry["version"], np.array([1.0, 0.0]))
    original_canonical = retries_entry["canonical"]

    # "Retries" 섹션만 한 단어 추가해 chunk_hash를 바꾼다(근접 중복 시뮬레이션).
    edited_text = DOC_TEXT.replace(
        "exponential backoff and random jitter,",
        "exponential backoff and random jitter plus some extra wording,",
    )
    doc_path.write_text(edited_text, encoding="utf-8")

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("근접 중복인데 curate(LLM)가 호출됨")

    result = run_doc_ingest(
        [str(doc_path)],
        llm_fn=_should_not_be_called, judge_fn=_should_not_be_called, evaluate_fn=_stub_evaluate_fn,
        near_duplicate_check=True, dedup_embed_fn=lambda texts: [[0.99, 0.0] for _ in texts],
    )

    assert result["skipped_near_duplicate_chunks"] == 1
    assert result["llm_calls"] == 0
    unchanged_entry = next(
        e for e in wiki_store.list_active_entries() if e["entry_id"] == retries_entry["entry_id"])
    assert unchanged_entry["canonical"] == original_canonical


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
