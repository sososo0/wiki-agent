"""
wiki-agent / core / wiki_store.py

자가 갱신형 RAG의 "지식 접근" 레이어. 표준 라이브러리(sqlite3 + FTS5)만 사용하므로
외부 서비스/모델 없이 바로 실행된다. MCP 서버는 이 모듈을 호출하기만 한다.

- 검색: FTS5 BM25 + dense 임베딩 + RRF 융합 + cross-encoder rerank (status='active' 엔트리만)
  실제 랭킹 로직은 core/retrieval.py에 분리되어 있다(search_wiki는 BM25 후보 추출 +
  로깅만 담당).
- 로깅: 검색/대화/피드백을 DB에 적재 → 이후 피드백 파이프라인의 연료
"""

import os
import re
import json
import time
import sqlite3
from typing import List, Dict, Any

try:
    from core import retrieval
except ImportError:        # python core/wiki_store.py 로 직접 실행할 때
    import retrieval

DB_PATH = os.environ.get("WIKI_AGENT_DB", "wiki_agent.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_entry (
  entry_id    TEXT PRIMARY KEY,
  topic       TEXT NOT NULL,
  canonical   TEXT NOT NULL,
  body_md     TEXT,
  provenance  TEXT DEFAULT 'doc_verified',   -- doc_verified | curated_from_logs | agent_generated
  confidence  REAL DEFAULT 1.0,
  version     INTEGER DEFAULT 1,
  status      TEXT DEFAULT 'active',          -- active | shadow | deprecated
  sources     TEXT DEFAULT '[]',
  supersedes  TEXT DEFAULT NULL,    -- 이 엔트리(shadow candidate)가 교체하려는 대상 entry_id
  updated_at  REAL
);

-- 키워드 검색 인덱스(데모용). 프로덕션은 여기에 벡터 검색을 병행.
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
  entry_id UNINDEXED, topic, canonical, body_md,
  tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS retrieval_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  query     TEXT,
  retrieved TEXT,            -- JSON: [{entry_id, score}]
  ts        REAL
);

CREATE TABLE IF NOT EXISTS conversation_log (
  conv_id   TEXT, turn_id INTEGER, query TEXT, answer TEXT,
  retrieved TEXT, escalated INTEGER DEFAULT 0, ts REAL,
  PRIMARY KEY (conv_id, turn_id)
);

CREATE TABLE IF NOT EXISTS feedback (
  conv_id TEXT, turn_id INTEGER, thumb TEXT, ts REAL
);
"""

SEED_ENTRIES = [
    ("wiki_0001", "Retry backoff strategy",
     "Use exponential backoff with jitter for transient failures.",
     "Retry transient errors (timeouts, 5xx) with exponentially increasing "
     "delays and random jitter to avoid thundering-herd. Cap total attempts "
     "and total elapsed time. Do not retry non-idempotent writes blindly."),
    ("wiki_0002", "Rate limiting",
     "Token bucket is the common algorithm for API rate limiting.",
     "A token bucket refills at a fixed rate up to a capacity; each request "
     "consumes a token. Return HTTP 429 with a Retry-After header when empty. "
     "Apply limits per client key, not globally."),
    ("wiki_0003", "Connection pooling",
     "Reuse database connections via a bounded pool.",
     "Opening a connection per request is expensive. A pool keeps warm "
     "connections, bounded by max size to protect the database. Tune pool "
     "size to (cores * 2) as a starting point and watch for pool exhaustion."),
    ("wiki_0004", "Circuit breaker",
     "Stop calling a failing dependency to let it recover.",
     "After a failure threshold the breaker opens and fails fast for a "
     "cooldown, then half-opens to probe recovery. Prevents cascading "
     "failures and gives the downstream service room to heal."),
    ("wiki_0005", "Idempotency keys",
     "Use idempotency keys to make retried writes safe.",
     "Client sends a unique key with a write; the server stores the result "
     "keyed by it and returns the same result on retry. Essential when "
     "combining retries with non-idempotent operations like payments."),
]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(seed: bool = True) -> None:
    conn = _conn()
    conn.executescript(SCHEMA)
    if seed and conn.execute("SELECT COUNT(*) FROM wiki_entry").fetchone()[0] == 0:
        for eid, topic, canon, body in SEED_ENTRIES:
            add_entry(eid, topic, canon, body, conn=conn, status="active",
                      provenance="doc_verified")
    conn.commit()
    conn.close()


def add_entry(entry_id, topic, canonical, body_md, *, conn=None,
              status="shadow", provenance="curated_from_logs",
              confidence=1.0, sources=None, supersedes=None) -> None:
    """엔트리 추가/교체. (MCP로는 노출하지 않는다 — 에이전트가 KB에 직접 쓰지 못하게.)"""
    own = conn is None
    conn = conn or _conn()
    conn.execute(
        """INSERT INTO wiki_entry
           (entry_id, topic, canonical, body_md, provenance, confidence,
            status, sources, supersedes, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(entry_id) DO UPDATE SET
             topic=excluded.topic, canonical=excluded.canonical,
             body_md=excluded.body_md, provenance=excluded.provenance,
             confidence=excluded.confidence, status=excluded.status,
             sources=excluded.sources, supersedes=excluded.supersedes,
             version=wiki_entry.version+1,
             updated_at=excluded.updated_at""",
        (entry_id, topic, canonical, body_md, provenance, confidence,
         status, json.dumps(sources or []), supersedes, time.time()))
    conn.execute("DELETE FROM wiki_fts WHERE entry_id=?", (entry_id,))
    conn.execute(
        "INSERT INTO wiki_fts (entry_id, topic, canonical, body_md) VALUES (?,?,?,?)",
        (entry_id, topic, canonical, body_md))
    if own:
        conn.commit()
        conn.close()


def _fts_query(q: str) -> str:
    """사용자 입력을 FTS5 문법 오류 없이 안전하게 토큰 OR 질의로 변환."""
    toks = re.findall(r"[0-9A-Za-z\uac00-\ud7a3]+", q)
    return " OR ".join(toks) if toks else '""'


def list_active_entries() -> List[Dict[str, Any]]:
    """status='active' 엔트리 전체를 반환 (하이브리드 검색의 dense 랭킹 입력).

    sources도 포함한다 — 문서 ingestion의 dedupe(core/pipeline/dedupe.py)가
    이미 active로 승격된 엔트리의 chunk_hash를 비교해 콘텐츠 변경 여부를
    판단해야 하기 때문(검색 경로 자체는 sources를 쓰지 않으므로 영향 없음)."""
    conn = _conn()
    rows = conn.execute(
        """SELECT entry_id, topic, canonical, body_md, provenance, confidence,
                  version, sources
           FROM wiki_entry WHERE status = 'active'""").fetchall()
    conn.close()
    return [
        {**dict(r), "sources": json.loads(r["sources"]) if r["sources"] else []}
        for r in rows
    ]


def _bm25_rank(query: str, limit: int) -> List[str]:
    """BM25 랭킹만 entry_id 리스트로 반환 (로깅 없음)."""
    conn = _conn()
    rows = conn.execute(
        """SELECT e.entry_id
           FROM wiki_fts
           JOIN wiki_entry e ON e.entry_id = wiki_fts.entry_id
           WHERE wiki_fts MATCH ? AND e.status = 'active'
           ORDER BY bm25(wiki_fts) ASC
           LIMIT ?""",
        (_fts_query(query), limit)).fetchall()
    conn.close()
    return [r["entry_id"] for r in rows]


def search_wiki(query: str, k: int = 5) -> List[Dict[str, Any]]:
    """BM25 + dense + RRF + rerank 하이브리드 검색 후 결과를 반환하고 retrieval_log에 적재."""
    fetch_k = max(k * 4, 20)
    bm25_ids = _bm25_rank(query, fetch_k)
    entries = list_active_entries()
    results = retrieval.hybrid_search(query, entries, bm25_ids, k=k, fetch_k=fetch_k)

    conn = _conn()
    conn.execute(
        "INSERT INTO retrieval_log (query, retrieved, ts) VALUES (?,?,?)",
        (query, json.dumps([{"entry_id": x["entry_id"], "score": x["score"]}
                            for x in results]), time.time()))
    conn.commit()
    conn.close()
    return results


def list_retrieval_log() -> List[Dict[str, Any]]:
    """retrieval_log 전체를 반환 (피드백 파이프라인의 gap 마이닝 입력)."""
    conn = _conn()
    rows = conn.execute("SELECT id, query, retrieved, ts FROM retrieval_log").fetchall()
    conn.close()
    return [{"id": r["id"], "query": r["query"],
             "retrieved": json.loads(r["retrieved"]) if r["retrieved"] else [],
             "ts": r["ts"]} for r in rows]


def list_feedback() -> List[Dict[str, Any]]:
    """feedback 전체를 반환 (피드백 파이프라인의 집계 신호 입력)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT conv_id, turn_id, thumb, ts FROM feedback").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_shadow_entries() -> List[Dict[str, Any]]:
    """status='shadow' 엔트리 전체를 반환 (promote 단계의 승격/병합 입력)."""
    conn = _conn()
    rows = conn.execute(
        """SELECT entry_id, topic, canonical, body_md, provenance, confidence,
                  version, sources, supersedes
           FROM wiki_entry WHERE status = 'shadow'""").fetchall()
    conn.close()
    return [
        {**dict(r), "sources": json.loads(r["sources"]) if r["sources"] else []}
        for r in rows
    ]


def list_rejected_entries() -> List[Dict[str, Any]]:
    """status='rejected' 엔트리 전체를 반환. 문서 ingestion이 게이트 거부를
    chunk_hash 단위로 기억해(core/pipeline/dedupe.rejected_entry_id) 같은
    콘텐츠를 재실행마다 다시 LLM 큐레이션/judge에 돌리지 않게 하는 입력 —
    이 status는 active/shadow 어느 쪽에도 안 잡혀 검색·승격에 영향 없다."""
    conn = _conn()
    rows = conn.execute(
        """SELECT entry_id, topic, canonical, body_md, provenance, confidence,
                  version, sources, supersedes
           FROM wiki_entry WHERE status = 'rejected'""").fetchall()
    conn.close()
    return [
        {**dict(r), "sources": json.loads(r["sources"]) if r["sources"] else []}
        for r in rows
    ]


def list_deprecated_entries() -> List[Dict[str, Any]]:
    """status='deprecated' 엔트리 전체를 반환. promote.py가 shadow를 승격시키며
    supersedes 대상이 있던 shadow 자신의 entry_id를 이 status로 강등시킨다
    (promote.py:138-139) — supersedes 컬럼은 강등 후에도 그대로 남아 있어
    "과거에 어떤 active 엔트리를 대체하려 했는지" 이력으로 읽을 수 있다."""
    conn = _conn()
    rows = conn.execute(
        """SELECT entry_id, topic, canonical, body_md, provenance, confidence,
                  version, sources, supersedes
           FROM wiki_entry WHERE status = 'deprecated'""").fetchall()
    conn.close()
    return [
        {**dict(r), "sources": json.loads(r["sources"]) if r["sources"] else []}
        for r in rows
    ]


def count_entries(status: str, since_ts: float = None) -> int:
    """주어진 status의 엔트리 수(일일 신규 shadow 상한 계산용)."""
    conn = _conn()
    if since_ts is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM wiki_entry WHERE status = ?", (status,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM wiki_entry WHERE status = ? AND updated_at >= ?",
            (status, since_ts)).fetchone()
    conn.close()
    return row[0]


def set_entry_status(entry_id: str, status: str) -> None:
    """엔트리 status만 갱신(승격/강등에 사용, version은 올리지 않음)."""
    conn = _conn()
    conn.execute("UPDATE wiki_entry SET status = ? WHERE entry_id = ?", (status, entry_id))
    conn.commit()
    conn.close()


def log_turn(conv_id, turn_id, query, answer, retrieved_ids, escalated=False):
    conn = _conn()
    conn.execute(
        """INSERT OR REPLACE INTO conversation_log
           (conv_id, turn_id, query, answer, retrieved, escalated, ts)
           VALUES (?,?,?,?,?,?,?)""",
        (conv_id, turn_id, query, answer, json.dumps(retrieved_ids),
         int(escalated), time.time()))
    conn.commit()
    conn.close()


def submit_feedback(conv_id, turn_id, thumb):
    conn = _conn()
    conn.execute("INSERT INTO feedback (conv_id, turn_id, thumb, ts) VALUES (?,?,?,?)",
                 (conv_id, turn_id, thumb, time.time()))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # 단독 실행: 초기화 + 시드 + 샘플 검색으로 동작 확인
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db(seed=True)
    for q in ["how do I retry failed requests safely",
              "limit api calls per client",
              "make payment retries safe"]:
        hits = search_wiki(q, k=3)
        print(f"\nQ: {q}")
        for h in hits:
            print(f"  - {h['entry_id']} ({h['score']:>7}) {h['topic']}")
    print("\nretrieval_log rows:",
          _conn().execute("SELECT COUNT(*) FROM retrieval_log").fetchone()[0])
