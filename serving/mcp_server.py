"""
wiki-agent / serving / mcp_server.py

RAG 검색을 MCP 도구로 노출하는 stdio 서버. Hermes(또는 임의의 MCP 클라이언트)가
이 서버를 호출한다. 핵심 로직은 core.wiki_store에 있고, 이 파일은 얇은 어댑터다.

노출 도구는 2개로 최소화한다(Hermes 철학: "필요한 최소 표면만"):
  - search_wiki      : 지식베이스 검색 (읽기)
  - submit_feedback  : 답변 피드백 기록 (쓰기지만 KB가 아닌 feedback 테이블)
KB 쓰기(add_entry)는 의도적으로 노출하지 않는다 — 에이전트가 위키에 직접 못 쓰게.

실행: python -m serving.mcp_server   (또는 python serving/mcp_server.py)
의존성: pip install "mcp"   (core.wiki_store 자체는 표준 라이브러리만 사용)
"""

import os
import sys

# 레포 루트를 import 경로에 추가 → `from core.wiki_store import ...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP            # pip install mcp
from core.wiki_store import (
    init_db, search_wiki as _search, submit_feedback as _feedback,
)

init_db(seed=True)                                 # 최초 1회 스키마+시드 보장

mcp = FastMCP("wiki-agent")

# entry_id+version 키 임베딩 캐시. 안 주면(demo/app.py도 동일 패턴) search_wiki가
# 매 호출마다 활성 엔트리 전체(현재 400여 개)를 재인코딩한다 — 이 프로세스는
# stdio로 오래 사는 서버이므로 모듈 전역에 들고 다니면서 콘텐츠가 안 바뀐
# 엔트리는 재인코딩을 건너뛴다(core/retrieval.py _entry_vectors 참고).
_search_embed_cache: dict = {}


@mcp.tool()
def search_wiki(query: str, k: int = 5) -> dict:
    """Search the wiki knowledge base for relevant entries.

    Always cite the returned entry_id values in your answer. If no result is
    relevant, do not fabricate — say you don't know.

    Args:
        query: natural-language question to search for.
        k: number of entries to return (default 5).
    """
    results = _search(query, k, cache=_search_embed_cache)
    return {"count": len(results), "results": results}


@mcp.tool()
def submit_feedback(conv_id: str, turn_id: int, thumb: str) -> dict:
    """Record user feedback on an answer.

    Args:
        conv_id: conversation id.
        turn_id: turn number within the conversation.
        thumb: "up" or "down".
    """
    _feedback(conv_id, turn_id, thumb)
    return {"ok": True}


if __name__ == "__main__":
    mcp.run()        # 기본 stdio 트랜스포트 (Hermes/Claude Code 등이 프로세스 관리)
