"""
wiki-agent / core / pipeline / dedupe.py

문서 ingestion의 멱등성 분기. 새 테이블 없이 entry_id 결정성(curate.
doc_chunk_entry_id) + 기존 엔트리의 sources[].chunk_hash 비교로 "콘텐츠가
안 바뀌었으면 LLM 호출조차 하지 않는다"를 구현한다. DB 접근 없는 순수 함수
— existing_entries_by_id는 호출부(scripts/ingest_doc.py)가 wiki_store에서
미리 읽어 dict로 넘긴다.

분기 3가지:
- skip:   entry_id가 이미 있고 chunk_hash가 같음 -> 콘텐츠 불변, curate 생략
- create: entry_id가 없음 -> 신규
- update: entry_id가 있고 chunk_hash가 다름. 기존이 아직 shadow(또는
          deprecated)면 같은 entry_id를 그대로 덮어써도 안전(아직 게이트를
          통과해 active가 된 적이 없으므로). 기존이 이미 active면 같은
          entry_id를 직접 덮어쓰는 것은 게이트를 거치지 않은 active 갱신이라
          HARD CONSTRAINT 위반 -> 새 entry_id({base}_v{n})로 만들고
          supersedes=base_entry_id를 채워 promote.py의 기존 supersedes->
          deprecated 강등 경로를 그대로 태운다.
"""

from typing import Any, Dict, Optional

from core.pipeline.curate import doc_chunk_entry_id


def _existing_chunk_hash(existing: Dict[str, Any]) -> Optional[str]:
    for src in existing.get("sources") or []:
        if "chunk_hash" in src:
            return src["chunk_hash"]
    return None


def resolve_doc_chunk_op(
    candidate: Dict[str, Any],
    existing_entries_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """candidate(chunk.to_doc_candidates 출력 1건) -> {"op", "entry_id", "supersedes"}.

    existing_entries_by_id: {entry_id: {"status": "active"|"shadow"|"deprecated",
    "version": int, "sources": [...], ...}} — active/shadow 엔트리를 합쳐서
    호출부가 미리 구성."""
    base_entry_id = doc_chunk_entry_id(candidate)
    existing = existing_entries_by_id.get(base_entry_id)

    if existing is None:
        return {"op": "create", "entry_id": base_entry_id, "supersedes": None}

    if _existing_chunk_hash(existing) == candidate["chunk_hash"]:
        return {"op": "skip", "entry_id": base_entry_id, "supersedes": None}

    if existing.get("status") == "active":
        new_version = existing.get("version", 1) + 1
        new_entry_id = f"{base_entry_id}_v{new_version}"
        return {"op": "update", "entry_id": new_entry_id, "supersedes": base_entry_id}

    return {"op": "update", "entry_id": base_entry_id, "supersedes": None}
