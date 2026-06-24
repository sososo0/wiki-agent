"""
wiki-agent / core / pipeline / dedupe.py

문서 ingestion의 멱등성 분기. 새 테이블 없이 entry_id 결정성(curate.
doc_chunk_entry_id) + 기존 엔트리의 sources[].chunk_hash 비교로 "콘텐츠가
안 바뀌었으면 LLM 호출조차 하지 않는다"를 구현한다. DB 접근 없는 순수 함수 —
existing_entries_by_id는 호출부(scripts/ingest_doc.py)가 미리 읽어 dict로 넘긴다.

분기 4가지:
- skip_rejected: candidate의 chunk_hash가 이전 게이트 거부 기록과 동일 ->
          curate/judge 생략. rejected_entry_id()는 base_entry_id가 아니라
          chunk_hash 기반 별도 네임스페이스를 쓴다 — base_entry_id를 쓰면 그
          자리가 나중에 진짜 active/shadow가 될 PRIMARY KEY라 거부 기록이
          실제 콘텐츠를 덮어쓸 위험이 있기 때문.
- skip:   entry_id가 이미 있고 chunk_hash가 같음 -> 콘텐츠 불변, curate 생략
- create: entry_id가 없음 -> 신규
- update: entry_id가 있고 chunk_hash가 다름. 기존이 아직 shadow/deprecated면
          같은 entry_id를 덮어써도 안전(아직 active가 된 적 없으므로). 기존이
          이미 active면 직접 덮어쓰는 건 게이트를 거치지 않은 active 갱신이라
          HARD CONSTRAINT 위반 -> 새 entry_id({base}_v{n})로 만들고
          supersedes=base_entry_id를 채워 promote.py의 강등 경로를 태운다.
"""

from typing import Any, Callable, Dict, Optional

import numpy as np

from core.pipeline.curate import doc_chunk_entry_id


def _existing_chunk_hash(existing: Dict[str, Any]) -> Optional[str]:
    for src in existing.get("sources") or []:
        if "chunk_hash" in src:
            return src["chunk_hash"]
    return None


def rejected_entry_id(candidate: Dict[str, Any]) -> str:
    """candidate -> 게이트 거부 기록 전용 entry_id. chunk_hash로 주소를 잡아서
    콘텐츠가 바뀌면 자동으로 새 주소가 되고(=재시도 허용), active/shadow
    entry_id 네임스페이스와는 절대 겹치지 않는다."""
    base = doc_chunk_entry_id(candidate)
    return f"{base}_rej_{candidate['chunk_hash'][:8]}"


def resolve_doc_chunk_op(
    candidate: Dict[str, Any],
    existing_entries_by_id: Dict[str, Dict[str, Any]],
    *,
    embed_fn: Optional[Callable] = None,
    near_duplicate_threshold: float = 0.97,
) -> Dict[str, Any]:
    """candidate(chunk.to_doc_candidates 출력 1건) -> {"op", "entry_id", "supersedes"}.

    existing_entries_by_id: {entry_id: {"status", "version", "sources",
    "_embedding": np.ndarray|None, ...}} — active/shadow/rejected 엔트리를 합쳐
    호출부가 미리 구성한다. DB 접근 없는 순수 함수 원칙을 지키려고 "_embedding"도
    호출부가 미리 읽어서 넘긴다(있으면 wiki_store.get_embedding 값).

    embed_fn을 주면(기본 None, 안 주면 하위 호환) chunk_hash가 달라 "update"로
    분류되려는 후보에 대해서만 새 청크 텍스트를 인코딩해 기존 "_embedding"과
    코사인 유사도를 비교한다. near_duplicate_threshold(기본 0.97) 이상이면
    재큐레이션 없이 "skip_near_duplicate"로 분류 — 오탈자/줄바꿈 수준 편집이
    매번 LLM 재큐레이션을 부르는 비용을 줄인다. "_embedding"이 없으면 안전하게
    update로 폴백한다."""
    base_entry_id = doc_chunk_entry_id(candidate)

    if rejected_entry_id(candidate) in existing_entries_by_id:
        return {"op": "skip_rejected", "entry_id": base_entry_id, "supersedes": None}

    existing = existing_entries_by_id.get(base_entry_id)

    if existing is None:
        return {"op": "create", "entry_id": base_entry_id, "supersedes": None}

    if _existing_chunk_hash(existing) == candidate["chunk_hash"]:
        return {"op": "skip", "entry_id": base_entry_id, "supersedes": None}

    if embed_fn is not None and existing.get("_embedding") is not None:
        candidate_vec = np.asarray(embed_fn([candidate["text"]])[0])
        existing_vec = np.asarray(existing["_embedding"])
        similarity = float(candidate_vec @ existing_vec)
        if similarity >= near_duplicate_threshold:
            return {"op": "skip_near_duplicate", "entry_id": base_entry_id, "supersedes": None}

    if existing.get("status") == "active":
        new_version = existing.get("version", 1) + 1
        new_entry_id = f"{base_entry_id}_v{new_version}"
        return {"op": "update", "entry_id": new_entry_id, "supersedes": base_entry_id}

    return {"op": "update", "entry_id": base_entry_id, "supersedes": None}
