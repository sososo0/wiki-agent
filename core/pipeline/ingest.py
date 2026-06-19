"""
wiki-agent / core / pipeline / ingest.py

피드백 파이프라인의 입력 정규화 단계. retrieval_log/feedback 원본 행을
mine.py가 바로 쓸 수 있는 형태로 정리한다. DB 접근 없음(순수 함수) —
wiki_store.list_retrieval_log()/list_feedback()의 출력을 받아서 처리.
"""

from typing import Any, Dict, List


def ingest_retrieval_log(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """빈 쿼리/3자 미만 제거하고 그룹핑 키 norm_query(소문자+공백정리)를 추가."""
    out = []
    for row in rows:
        q = (row.get("query") or "").strip()
        if len(q) < 3:
            continue
        out.append({**row, "norm_query": " ".join(q.lower().split())})
    return out


def ingest_feedback(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """entry/query에 귀속하지 않는 집계 신호만 산출 (귀속 키가 없으므로)."""
    n = len(rows)
    down = sum(1 for r in rows if r.get("thumb") == "down")
    return {"n": n, "down": down, "down_rate": (down / n) if n else 0.0}
