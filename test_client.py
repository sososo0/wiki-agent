"""
wiki-agent / test_client.py

mcp 패키지 없이도 핵심 로직(검색 + 로깅 + 피드백)을 검증한다.
Hermes를 붙이기 전에 이걸 먼저 통과시켜라 → 이후 문제 원인이 깔끔히 분리된다.

실행: python test_client.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import wiki_store

DB = "test_wiki_agent.db"
os.environ["WIKI_AGENT_DB"] = DB
wiki_store.DB_PATH = DB
if os.path.exists(DB):
    os.remove(DB)

wiki_store.init_db(seed=True)
print("DB initialized + seeded.\n")

# 1) MCP search_wiki 도구가 호출할 함수와 동일한 경로를 직접 호출
queries = [
    "how to retry transient failures",
    "protect database from too many connections",
    "stop hammering a broken service",
]
for q in queries:
    hits = wiki_store.search_wiki(q, k=2)
    top = hits[0]["entry_id"] if hits else "—"
    print(f"Q: {q}\n   top={top}  " +
          ", ".join(f"{h['entry_id']}:{h['score']}" for h in hits))

# 2) 대화 턴 + 피드백 로깅 (서빙 경로 시뮬레이션)
wiki_store.log_turn("c_1", 1, queries[0], "Use exponential backoff...",
                    ["wiki_0001"], escalated=False)
wiki_store.submit_feedback("c_1", 1, "up")

# 3) 적재 확인 — 이 로그가 이후 피드백 파이프라인의 입력이 된다
conn = wiki_store._conn()
print("\n-- logged --")
print("retrieval_log   :", conn.execute("SELECT COUNT(*) FROM retrieval_log").fetchone()[0])
print("conversation_log:", conn.execute("SELECT COUNT(*) FROM conversation_log").fetchone()[0])
print("feedback        :", conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0])

assert conn.execute("SELECT COUNT(*) FROM retrieval_log").fetchone()[0] == len(queries)
assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
print("\nALL CHECKS PASSED ✅")
