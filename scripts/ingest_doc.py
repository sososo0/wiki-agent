"""
wiki-agent / scripts / ingest_doc.py

문서 ingestion 파이프라인 오케스트레이션:
parse_directory -> chunk_sections -> to_doc_candidates -> (각 candidate)
dedupe.resolve_doc_chunk_op -> [skip | curate.curate_doc_chunk] -> gate.passes_gate
-> shadow 반영 -> reindex(no-op) -> promote.promote_if_better.

run_update_cycle.py(로그 마이닝 트리거)와 트리거 모양이 다르므로(파일/디렉터리
경로 인자가 필요하고, 사람이 의도적으로 실행) 별도 스크립트로 분리한다. 다만
daily_cap/gate/promote는 그대로 재사용 — daily_cap은 wiki_store.count_entries
("shadow", ...)가 provenance를 구분하지 않고 전역으로 세므로 로그 기반 갱신과
자동으로 합산된다.

콘텐츠가 안 바뀐 청크는 dedupe가 "skip"으로 분류해 curate(LLM 호출)를 아예
건너뛴다 — 같은 문서를 재실행해도 비용이 들지 않는 멱등성의 핵심. 게이트가
거부한 청크도 동일하게 chunk_hash 기준으로 기억해(status="rejected" 마커,
dedupe.rejected_entry_id) "skip_rejected"로 분류한다 — 콘텐츠가 그대로인데
거부된 청크를 재실행마다 다시 큐레이션/judge에 돌려 비용을 반복 지불하지
않게 한다. 문서 내용이 바뀌면(chunk_hash가 달라지면) 자동으로 새 주소가 되어
다시 시도된다.

게이트의 grounding judge는 기본적으로 gate.default_judge_fn을 쓰는데, 이건
sources의 "query" 필드만 읽어서 문서 출처(query 없음)는 source dict 자체를
stringify해 judge에 넘긴다 — 실제 청크 본문을 한 번도 보지 못한 채 판단하는
셈이라 거부율이 비정상적으로 높아진다. gate.py는 무수정 대상이라, judge_fn
미주입 시 curate.make_doc_judge_fn으로 만든 문서 전용 judge(원본 chunk_text를
직접 프롬프트에 넣음)를 기본값으로 주입한다.

실행: python scripts/ingest_doc.py <path...> [--daily-cap N] [--min-sources 1]
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store
from core.pipeline import chunk, curate, dedupe, gate, parse, promote, reindex


def _existing_entries_by_id() -> Dict[str, Dict[str, Any]]:
    """active+shadow+rejected 엔트리를 entry_id로 합친 dict(+status 태깅).
    dedupe.py가 chunk_hash 비교로 콘텐츠 변경 여부 및 과거 게이트 거부 여부를
    판단하는 입력. 영속 임베딩(있으면)을 "_embedding"으로 같이 채운다 —
    dedupe.resolve_doc_chunk_op의 근접 중복 체크(embed_fn 주입 시에만 실제로
    쓰임)가 DB를 직접 안 보고도 비교할 수 있게 미리 읽어서 넘기는 것."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for e in wiki_store.list_active_entries():
        by_id[e["entry_id"]] = {**e, "status": "active"}
    for e in wiki_store.list_shadow_entries():
        by_id[e["entry_id"]] = {**e, "status": "shadow"}
    for e in wiki_store.list_rejected_entries():
        by_id[e["entry_id"]] = {**e, "status": "rejected"}
    for entry_id, entry in by_id.items():
        cached = wiki_store.get_embedding(entry_id)
        if cached is not None:
            entry["_embedding"] = cached[1]
    return by_id


def run_doc_ingest(
    paths: List[str],
    *,
    gold_path: Optional[str] = None,
    k: int = 5,
    max_chars: int = chunk.DEFAULT_MAX_CHARS,
    min_chars: int = chunk.DEFAULT_MIN_CHARS,
    daily_cap: int = 20,
    min_sources: int = 1,
    llm_fn: Optional[Callable] = None,
    judge_fn: Optional[Callable] = None,
    evaluate_fn: Optional[Callable] = None,
    dry_run: bool = False,
    near_duplicate_check: bool = False,
    dedup_embed_fn: Optional[Callable] = None,
    near_duplicate_threshold: float = 0.97,
) -> Dict[str, Any]:
    """dry_run=True면 parse/chunk/dedupe까지만 실행하고 각 candidate가 어떤
    op(create/update/skip/skip_rejected)로 분류되는지만 보고한다 — curate(LLM
    호출), gate(judge LLM 호출), wiki_store.add_entry(DB 쓰기), promote(eval LLM
    호출 + 가능하면 DB 쓰기) 전부 건너뛴다. 콘텐츠/비용을 미리 점검하고 싶을 때
    실제 LLM 호출·DB 변경 없이 실행할 수 있게 한다.

    near_duplicate_check=True면(기본 off — 임베딩 모델을 추가로 로딩/호출)
    chunk_hash가 달라도 기존 엔트리와 임베딩 코사인 유사도가
    near_duplicate_threshold 이상이면 재큐레이션을 생략한다(core/pipeline/
    dedupe.py 참고) — 오탈자/포맷팅 수준의 편집마다 LLM을 다시 부르는 비용을
    줄인다."""
    if not dry_run:
        from eval.run_eval import GOLD_PATH, evaluate, load_gold
        evaluate_fn = evaluate_fn or evaluate

    dedup_embed_fn_resolved = None
    if near_duplicate_check:
        from core import retrieval
        dedup_embed_fn_resolved = dedup_embed_fn or retrieval.default_embed_fn

    summary: Dict[str, Any] = {
        "parsed_files": [],
        "failed_files": [],
        "skipped_chunks": 0,
        "skipped_rejected_chunks": 0,
        "skipped_near_duplicate_chunks": 0,
        "would_curate": [],
        "shadow_written": [],
        "rejected": [],
        "llm_calls": 0,
        "promote": None,
    }

    candidates: List[Dict[str, Any]] = []
    for path in paths:
        result = parse.parse_directory(path)
        summary["failed_files"].extend(result["failed"])
        for doc in result["parsed"]:
            summary["parsed_files"].append(doc["path"])
            chunks = chunk.chunk_sections(doc["sections"], max_chars=max_chars, min_chars=min_chars)
            candidates.extend(chunk.to_doc_candidates(doc["path"], doc["doc_hash"], chunks))

    existing_by_id = _existing_entries_by_id()
    existing_active_entries = wiki_store.list_active_entries()
    since_ts = time.time() - 86400

    # judge_fn 미주입 시 문서 전용 grounding judge를 기본으로 쓴다 — gate.py의
    # default_judge_fn은 sources의 "query" 필드만 읽어서 문서 출처(query 없음)는
    # source dict를 그대로 stringify해 judge에 넘기게 되어 실제 청크 본문을 보지
    # 못한다(gate.py는 무수정 대상). chunk_text_by_entry_id는 후보를 처리하며 채움.
    chunk_text_by_entry_id: Dict[str, str] = {}
    doc_judge_fn = judge_fn or curate.make_doc_judge_fn(chunk_text_by_entry_id)

    for cand in candidates:
        op_info = dedupe.resolve_doc_chunk_op(
            cand, existing_by_id,
            embed_fn=dedup_embed_fn_resolved, near_duplicate_threshold=near_duplicate_threshold,
        )
        if op_info["op"] == "skip":
            summary["skipped_chunks"] += 1
            continue
        if op_info["op"] == "skip_rejected":
            summary["skipped_rejected_chunks"] += 1
            continue
        if op_info["op"] == "skip_near_duplicate":
            summary["skipped_near_duplicate_chunks"] += 1
            continue

        if dry_run:
            summary["would_curate"].append({
                "doc_path": cand["doc_path"], "chunk_index": cand["chunk_index"],
                "op": op_info["op"], "entry_id": op_info["entry_id"],
            })
            continue

        summary["llm_calls"] += 1
        try:
            patch = curate.curate_doc_chunk(cand, llm_fn=llm_fn)
        except Exception as e:
            summary["rejected"].append({
                "doc_path": cand["doc_path"], "chunk_index": cand["chunk_index"],
                "reason": f"curate failed: {e}",
            })
            continue

        patch["entry_id"] = op_info["entry_id"]
        if op_info["supersedes"]:
            patch["supersedes"] = op_info["supersedes"]
        chunk_text_by_entry_id[patch["entry_id"]] = cand["text"]

        # 새 버전이 대체하려는 자기 자신과는 "근접 중복"으로 막히면 안 되므로 게이트
        # 중복/모순 체크 대상에서 제외한다(의도된 갱신, 우연한 중복이 아님).
        gate_existing = existing_active_entries
        if op_info["supersedes"]:
            gate_existing = [
                e for e in existing_active_entries if e["entry_id"] != op_info["supersedes"]
            ]

        today_writes = wiki_store.count_entries("shadow", since_ts=since_ts)
        ok, reason = gate.passes_gate(
            patch, today_writes,
            existing_entries=gate_existing,
            daily_cap=daily_cap,
            min_sources=min_sources,
            judge_fn=doc_judge_fn,
        )
        if not ok:
            summary["rejected"].append({"entry_id": patch["entry_id"], "reason": reason})
            rej_id = dedupe.rejected_entry_id(cand)
            wiki_store.add_entry(
                rej_id, patch["topic"], patch["canonical"], patch["body_md"],
                status="rejected", provenance=patch["provenance"], confidence=0.0,
                sources=[{**patch["sources"][0], "verified": False, "rejected_reason": reason}],
                tier=patch.get("tier"),
            )
            existing_by_id[rej_id] = {"status": "rejected", "version": 1, "sources": [
                {"chunk_hash": cand["chunk_hash"]}
            ]}
            continue

        wiki_store.add_entry(
            patch["entry_id"], patch["topic"], patch["canonical"], patch["body_md"],
            status="shadow", provenance=patch["provenance"],
            confidence=patch["confidence"], sources=patch["sources"],
            supersedes=patch.get("supersedes"), tier=patch.get("tier"),
        )
        summary["shadow_written"].append(patch["entry_id"])
        existing_by_id[patch["entry_id"]] = {
            "status": "shadow", "version": 1, "sources": patch["sources"],
        }
        reindex.reindex_changed([patch["entry_id"]])

    if dry_run:
        return summary

    gold = load_gold(gold_path or GOLD_PATH)
    summary["promote"] = promote.promote_if_better(gold, k=k, evaluate_fn=evaluate_fn)
    return summary


def main():
    parser = argparse.ArgumentParser(description="문서 ingestion 파이프라인 실행")
    parser.add_argument("paths", nargs="+", help="마크다운 파일 또는 디렉터리 경로(들)")
    parser.add_argument("--gold", default=None)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--max-chars", type=int, default=chunk.DEFAULT_MAX_CHARS)
    parser.add_argument("--min-chars", type=int, default=chunk.DEFAULT_MIN_CHARS)
    parser.add_argument("--daily-cap", type=int, default=20)
    parser.add_argument("--min-sources", type=int, default=1)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="LLM을 호출하거나 DB에 쓰지 않고, 각 청크가 create/update/skip/"
             "skip_rejected 중 무엇으로 분류되는지만 미리 보여준다(비용 없음).")
    parser.add_argument(
        "--near-duplicate-check", action="store_true",
        help="chunk_hash가 달라도 기존 엔트리와 임베딩 유사도가 높으면(오탈자/포맷팅 "
             "수준 편집) 재큐레이션을 생략한다(기본 off — 임베딩 모델 추가 로딩/호출).")
    parser.add_argument(
        "--near-duplicate-threshold", type=float, default=0.97,
        help="근접 중복 판정 코사인 유사도 임계값(기본 0.97) — --near-duplicate-check일 때만 적용")
    args = parser.parse_args()

    wiki_store.init_db(seed=True)
    result = run_doc_ingest(
        args.paths, gold_path=args.gold, k=args.k,
        max_chars=args.max_chars, min_chars=args.min_chars,
        daily_cap=args.daily_cap, min_sources=args.min_sources,
        dry_run=args.dry_run,
        near_duplicate_check=args.near_duplicate_check,
        near_duplicate_threshold=args.near_duplicate_threshold,
    )

    print(f"parsed files: {len(result['parsed_files'])}")
    if result["failed_files"]:
        print(f"failed files: {result['failed_files']}")
    print(f"skipped chunks (unchanged): {result['skipped_chunks']}")
    print(f"skipped chunks (already rejected, unchanged): {result['skipped_rejected_chunks']}")
    print(f"skipped chunks (near-duplicate): {result['skipped_near_duplicate_chunks']}")

    if args.dry_run:
        print(f"would curate (LLM call + gate, not run): {len(result['would_curate'])}")
        for item in result["would_curate"]:
            print(f"  - [{item['op']}] {item['doc_path']}#{item['chunk_index']} -> {item['entry_id']}")
        return

    print(f"llm calls: {result['llm_calls']}")
    print(f"shadow written: {result['shadow_written']}")
    print(f"rejected: {result['rejected']}")
    promote_result = result["promote"]
    print(f"promote: promoted={promote_result['promoted']} "
          f"activated={promote_result['activated_entry_ids']}")
    print(f"  base:      {promote_result['base']}")
    print(f"  candidate: {promote_result['candidate']}")


if __name__ == "__main__":
    main()
