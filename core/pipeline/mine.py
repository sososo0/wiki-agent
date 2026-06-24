"""
wiki-agent / core / pipeline / mine.py

retrieval_log만으로 견고하게 뽑을 수 있는 "gap" 신호만 마이닝한다. fact/correction은
답변 텍스트(conversation_log)가 있어야 근거가 생기는데 이 스코프(입력=retrieval_log·
feedback)에는 없으므로 구현하지 않는다 — 근거 없는 LLM 추정을 막기 위한 의도적 축소.
"""

from typing import Any, Dict, List

NO_HIT_SCORE = -1e9  # 검색 결과가 전혀 없었던 쿼리는 가장 강한 gap 신호로 취급


def mine_gaps(
    ingested_rows: List[Dict[str, Any]],
    min_freq: int = 3,
    score_threshold: float = 0.0,
) -> List[Dict[str, Any]]:
    """norm_query로 그룹핑 → 빈도 >= min_freq 이고 평균 top-1 score < score_threshold인
    질문 그룹을 "이 주제는 위키에 없거나 약하다"는 gap 후보로 반환."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in ingested_rows:
        groups.setdefault(row["norm_query"], []).append(row)

    gaps = []
    for norm_query, rows in groups.items():
        freq = len(rows)
        if freq < min_freq:
            continue
        # 미스(retrieved=[]) 1건이라도 있으면 NO_HIT_SCORE를 그대로 보고한다 — 실제
        # 점수와 -1e9를 같은 평균에 섞으면 항상 의미 없는 값(예: -3.3e8)이 되어
        # "다른 occurrence는 점수가 좋았다"는 정보가 사라진 채 curate.py의 LLM
        # 프롬프트에 박힌다. 미스가 없을 때만 실제 점수들의 평균을 낸다.
        hit_scores = [row["retrieved"][0]["score"] for row in rows if row.get("retrieved")]
        has_miss = len(hit_scores) < len(rows)
        avg_top_score = NO_HIT_SCORE if has_miss else sum(hit_scores) / len(hit_scores)
        if avg_top_score >= score_threshold:
            continue
        query_examples = sorted({row["query"] for row in rows})
        gaps.append({
            "type": "gap",
            "norm_query": norm_query,
            "query_examples": query_examples,           # 중복 제거(LLM 프롬프트용)
            "query_occurrences": [row["query"] for row in rows],  # 중복 포함(출처 다양성 근거용)
            "freq": freq,
            "avg_top_score": avg_top_score,
        })
    return gaps
