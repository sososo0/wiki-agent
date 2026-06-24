"""
wiki-agent / scripts / translate_wiki_labels.py

위키 엔트리(topic/canonical/body_md)의 그래프 화면(/static/graph.html) 표시용
한글 번역을 만들어 translation_cache 테이블에 저장한다. 원본 wiki_entry는
절대 건드리지 않는다 — 검색/평가가 그 영어 원문에 의존하므로, 이건 순수
표시용 지역화 캐시다(core/wiki_store.py의 translation_cache 테이블 주석 참고).

entry_id+version으로 캐시 적중하는 엔트리는 재번역하지 않는다(core/graph.py의
임베딩 캐시와 동일 철학) — 같은 콘텐츠를 재번역해 비용을 반복 지불하지 않음.
여러 엔트리를 batch_size개씩 묶어 LLM 호출 1회로 처리해 비용을 줄인다.

실행: WIKI_AGENT_DB=<절대경로> python scripts/translate_wiki_labels.py [--batch-size 15]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store

TRANSLATE_MODEL = os.environ.get("WIKI_AGENT_TRANSLATE_MODEL", "claude-haiku-4-5")

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _extract_json_array(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '['~마지막 ']' 사이만 추출
    (core/pipeline/curate.py의 _extract_json_object와 동일 스타일, 배열 버전)."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def _all_entries() -> List[Dict[str, Any]]:
    """active+shadow+deprecated+rejected 전체 — core/graph.py의 build_graph()가
    그래프에 보여주는 노드 전부를 번역 대상으로 한다(동일 rows_by_status 패턴)."""
    entries: List[Dict[str, Any]] = []
    entries.extend(wiki_store.list_active_entries())
    entries.extend(wiki_store.list_shadow_entries())
    entries.extend(wiki_store.list_deprecated_entries())
    entries.extend(wiki_store.list_rejected_entries())
    return entries


def _translate_batch(entries: List[Dict[str, Any]], *, model: str) -> Dict[str, Dict[str, str]]:
    """entries(최대 batch_size개) -> {entry_id: {topic, canonical, body_md}}를
    LLM 호출 1회로 처리. 파싱 실패 시 빈 dict를 반환해 호출부가 이 배치 전체를
    스킵하게 한다(거짓 번역을 만들지 않음 — 다음 실행에서 다시 시도됨)."""
    items = "\n\n".join(
        f"[{e['entry_id']}]\ntopic: {e['topic']}\ncanonical: {e['canonical']}\n"
        f"body_md: {e.get('body_md') or ''}"
        for e in entries
    )
    prompt = (
        "Translate the following wiki entries into natural Korean. Keep "
        "technical proper nouns, library/API names, code identifiers, and "
        "numbers in English as-is — avoid unnecessary translation of terms "
        "that are already standard in technical Korean writing. "
        "Reply with a JSON array only, no other text, exactly one object "
        "per entry in the same order:\n"
        '[{"entry_id": "...", "topic": "...", "canonical": "...", '
        '"body_md": "..."}, ...]\n\n' + items
    )
    resp = _anthropic_client().messages.create(
        model=model, max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    if resp.stop_reason == "max_tokens":
        # 응답이 중간에 끊겨 JSON이 깨진 상태 — 파싱 시도조차 의미 없음
        # (배치가 너무 큼). 거짓 번역을 만들지 않고 그대로 실패 처리.
        return {}
    try:
        parsed = json.loads(_extract_json_array(text))
    except (json.JSONDecodeError, ValueError):
        return {}
    return {item["entry_id"]: item for item in parsed if "entry_id" in item}


def translate_all(*, lang: str = "ko", batch_size: int = 15,
                   model: str = TRANSLATE_MODEL) -> Dict[str, Any]:
    entries = _all_entries()
    cached = wiki_store.get_translations([e["entry_id"] for e in entries], lang=lang)

    to_translate = [
        e for e in entries
        if cached.get(e["entry_id"], {}).get("version") != e.get("version")
    ]

    summary: Dict[str, Any] = {
        "total": len(entries),
        "skipped_cached": len(entries) - len(to_translate),
        "translated": 0,
        "llm_calls": 0,
        "failed_batches": 0,
    }

    for i in range(0, len(to_translate), batch_size):
        batch = to_translate[i:i + batch_size]
        summary["llm_calls"] += 1
        translated = _translate_batch(batch, model=model)
        if not translated:
            summary["failed_batches"] += 1
            continue
        for e in batch:
            item = translated.get(e["entry_id"])
            if not item:
                continue
            wiki_store.set_translation(
                e["entry_id"], e.get("version"),
                item.get("topic"), item.get("canonical"), item.get("body_md"),
                lang=lang,
            )
            summary["translated"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="위키 엔트리의 그래프 화면용 한글 번역 캐시 생성(원본 영어는 안 건드림)")
    parser.add_argument("--lang", default="ko")
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument("--model", default=TRANSLATE_MODEL)
    args = parser.parse_args()

    wiki_store.init_db(seed=True)
    result = translate_all(lang=args.lang, batch_size=args.batch_size, model=args.model)

    print(f"total entries: {result['total']}")
    print(f"skipped (already cached, unchanged): {result['skipped_cached']}")
    print(f"translated: {result['translated']}")
    print(f"llm calls: {result['llm_calls']}")
    if result["failed_batches"]:
        print(f"failed batches: {result['failed_batches']}")


if __name__ == "__main__":
    main()
