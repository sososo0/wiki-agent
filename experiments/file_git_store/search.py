"""
wiki-agent / experiments / file_git_store / search.py

FTS5(SQLite 가상 테이블)가 없으니 BM25 대체용으로 표준 라이브러리만 쓴 단순
토큰 겹침 랭커를 만든다(품질 재현이 목적이 아니라 "검색 단계가 SQLite 없이도
끼울 자리가 있는가"를 보이는 게 목적). dense+rerank는 core/retrieval.py의
hybrid_search를 그대로 재사용한다 — 그 함수는 entries(List[Dict])만 받는
순수 함수라 SQLite/FTS5와 전혀 무관하다(import만 해서 검증).
"""

import re
from typing import Any, Dict, List

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core import retrieval


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def naive_keyword_rank(query: str, entries: List[Dict[str, Any]]) -> List[str]:
    """FTS5 BM25 대체 — query 토큰과 겹치는 토큰 수로만 정렬(idf 가중치 없음,
    BM25 품질 재현은 이번 프로토타입의 목적이 아니다)."""
    q_tokens = set(_tokenize(query))
    scored = []
    for e in entries:
        text = retrieval._entry_text(e)
        overlap = len(q_tokens & set(_tokenize(text)))
        scored.append((overlap, e["entry_id"]))
    scored.sort(key=lambda x: -x[0])
    return [eid for _, eid in scored]


def search(query: str, entries: List[Dict[str, Any]], *, k: int = 5,
           embed_fn=None, rerank_fn=None) -> List[Dict[str, Any]]:
    """파일 기반 entries에 core.retrieval.hybrid_search를 그대로 적용 — DB 의존이
    전혀 없다는 걸 보여주는 핵심 포인트. embed_fn/rerank_fn을 스텁으로 주입하면
    모델 로딩 없이도 동작 확인 가능(demo.py가 이렇게 씀)."""
    bm25_ids = naive_keyword_rank(query, entries)
    return retrieval.hybrid_search(
        query, entries, bm25_ids, k=k, embed_fn=embed_fn, rerank_fn=rerank_fn,
    )
