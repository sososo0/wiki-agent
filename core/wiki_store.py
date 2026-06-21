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
  tier        TEXT DEFAULT NULL,    -- basics | intermediate | advanced (난이도 분류)
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
  conv_id TEXT, turn_id INTEGER, thumb TEXT, reason TEXT DEFAULT NULL, ts REAL
);

-- 대화별 표시용 제목(첫 턴에 1회 생성). conversation_log의 원본 질문/답변과는
-- 별개로, "이전 대화" 목록 UI가 매번 다시 요약하지 않도록 캐시해 둔다.
CREATE TABLE IF NOT EXISTS conversation_meta (
  conv_id TEXT PRIMARY KEY, title TEXT, created_at REAL
);

-- 갱신 사이클(scripts/run_update_cycle.py) 결과 알림. KB(wiki_entry)가 아니라
-- 운영 알리미 데이터라 HARD CONSTRAINT(에이전트는 KB에 직접 못 씀)와 무관하다 —
-- 쓰기는 오직 신뢰된 오프라인 스크립트에서만 일어나고, 데모 서빙 경로(/chat)는
-- 절대 쓰지 않는다.
CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  level       TEXT NOT NULL,        -- info | warning | error
  title       TEXT NOT NULL,
  message     TEXT NOT NULL,
  created_at  REAL,
  read        INTEGER DEFAULT 0
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


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS는 기존 테이블에 새 컬럼을 추가해주지 않으므로,
    이미 데이터가 있는 DB에도 새 컬럼이 생기게 하는 보강 마이그레이션. 컬럼이
    이미 있으면 아무것도 하지 않는다(여러 번 호출해도 안전)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(wiki_entry)")}
    if "tier" not in cols:
        conn.execute("ALTER TABLE wiki_entry ADD COLUMN tier TEXT DEFAULT NULL")
    feedback_cols = {row["name"] for row in conn.execute("PRAGMA table_info(feedback)")}
    if "reason" not in feedback_cols:
        conn.execute("ALTER TABLE feedback ADD COLUMN reason TEXT DEFAULT NULL")


def init_db(seed: bool = True) -> None:
    conn = _conn()
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    if seed and conn.execute("SELECT COUNT(*) FROM wiki_entry").fetchone()[0] == 0:
        for eid, topic, canon, body in SEED_ENTRIES:
            add_entry(eid, topic, canon, body, conn=conn, status="active",
                      provenance="doc_verified", tier="basics")
    conn.commit()
    conn.close()


def add_entry(entry_id, topic, canonical, body_md, *, conn=None,
              status="shadow", provenance="curated_from_logs",
              confidence=1.0, sources=None, supersedes=None, tier=None) -> None:
    """엔트리 추가/교체. (MCP로는 노출하지 않는다 — 에이전트가 KB에 직접 쓰지 못하게.)

    tier: basics | intermediate | advanced | None(미분류). 호출부(curate.py가
    문서 파일명 또는 LLM 분류로 결정)가 넘기지 않으면 NULL로 남아 기존 동작과
    동일 — 이 컬럼이 없던 시절 만들어진 엔트리도 그대로 호환."""
    own = conn is None
    conn = conn or _conn()
    conn.execute(
        """INSERT INTO wiki_entry
           (entry_id, topic, canonical, body_md, provenance, confidence,
            status, sources, supersedes, tier, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(entry_id) DO UPDATE SET
             topic=excluded.topic, canonical=excluded.canonical,
             body_md=excluded.body_md, provenance=excluded.provenance,
             confidence=excluded.confidence, status=excluded.status,
             sources=excluded.sources, supersedes=excluded.supersedes,
             tier=excluded.tier,
             version=wiki_entry.version+1,
             updated_at=excluded.updated_at""",
        (entry_id, topic, canonical, body_md, provenance, confidence,
         status, json.dumps(sources or []), supersedes, tier, time.time()))
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
                  version, sources, tier
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


def search_wiki(query: str, k: int = 5, *, cache: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """BM25 + dense + RRF + rerank 하이브리드 검색 후 결과를 반환하고 retrieval_log에 적재.

    cache를 안 주면(기본값) 매 호출 새 dict로 항상 전체 재인코딩 — 기존 동작/테스트와
    동일(서로 다른 embed_fn을 쓰는 테스트들이 같은 entry_id+version을 캐시 충돌
    없이 공유할 수 있어야 함). 오래 사는 프로세스(데모 서버 등)는 자신이 들고 있는
    dict를 넘겨 코퍼스가 커져도 매 쿼리 재인코딩 비용이 늘지 않게 한다(core/retrieval.py
    hybrid_search의 cache 인자, core/graph.py와 동일 패턴)."""
    fetch_k = max(k * 4, 20)
    bm25_ids = _bm25_rank(query, fetch_k)
    entries = list_active_entries()
    results = retrieval.hybrid_search(
        query, entries, bm25_ids, k=k, fetch_k=fetch_k,
        cache=cache if cache is not None else {},
    )

    conn = _conn()
    conn.execute(
        "INSERT INTO retrieval_log (query, retrieved, ts) VALUES (?,?,?)",
        (query, json.dumps([{"entry_id": x["entry_id"], "score": x["score"]}
                            for x in results]), time.time()))
    conn.commit()
    conn.close()
    return results


def list_retrieval_log(since_ts: float = None) -> List[Dict[str, Any]]:
    """retrieval_log를 반환(피드백 파이프라인의 gap 마이닝 입력). since_ts를 주면
    그 이후 행만 반환 — 안 주면(기본값) 기존 동작과 동일하게 테이블 전체를 반환한다.
    윈도잉 없이 전체를 매 사이클 다시 마이닝하면 테이블이 무한히 쌓이는 동안 스캔
    비용도 계속 커지고, 한번 오염된(예: 평가 질문이 우연히 3번 이상 반복된) 쿼리가
    영원히 gap으로 재탐지된다 — scripts/run_update_cycle.py가 --window-days로 호출."""
    conn = _conn()
    if since_ts is None:
        rows = conn.execute("SELECT id, query, retrieved, ts FROM retrieval_log").fetchall()
    else:
        rows = conn.execute(
            "SELECT id, query, retrieved, ts FROM retrieval_log WHERE ts >= ?",
            (since_ts,)).fetchall()
    conn.close()
    return [{"id": r["id"], "query": r["query"],
             "retrieved": json.loads(r["retrieved"]) if r["retrieved"] else [],
             "ts": r["ts"]} for r in rows]


def list_feedback(since_ts: float = None) -> List[Dict[str, Any]]:
    """feedback을 반환(피드백 파이프라인의 집계 신호 입력). since_ts 의미는
    list_retrieval_log와 동일."""
    conn = _conn()
    if since_ts is None:
        rows = conn.execute(
            "SELECT conv_id, turn_id, thumb, reason, ts FROM feedback").fetchall()
    else:
        rows = conn.execute(
            "SELECT conv_id, turn_id, thumb, reason, ts FROM feedback WHERE ts >= ?",
            (since_ts,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_shadow_entries() -> List[Dict[str, Any]]:
    """status='shadow' 엔트리 전체를 반환 (promote 단계의 승격/병합 입력)."""
    conn = _conn()
    rows = conn.execute(
        """SELECT entry_id, topic, canonical, body_md, provenance, confidence,
                  version, sources, supersedes, tier
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
                  version, sources, supersedes, tier
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
                  version, sources, supersedes, tier
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


def list_conversation(conv_id: str) -> List[Dict[str, Any]]:
    """conv_id의 대화 턴 전체를 turn_id 순으로 반환(읽기 전용). 데모 채팅 UI가
    새로고침 후에도 conversation_log에 이미 쌓인 로그를 그대로 복원해 보여줄 수
    있게 한다 — log_turn이 매 턴 적재하는 데이터를 그대로 노출만 할 뿐 새 쓰기
    경로는 아니다."""
    conn = _conn()
    rows = conn.execute(
        """SELECT turn_id, query, answer, retrieved, escalated, ts
           FROM conversation_log WHERE conv_id = ? ORDER BY turn_id""",
        (conv_id,)).fetchall()
    conn.close()
    return [
        {**dict(r), "retrieved": json.loads(r["retrieved"]) if r["retrieved"] else [],
         "escalated": bool(r["escalated"])}
        for r in rows
    ]


def list_conversations(limit: int = 50) -> List[Dict[str, Any]]:
    """대화(conv_id) 단위로 묶어 최근 활동 순으로 반환(읽기 전용). 데모 채팅 UI가
    "이전 대화" 목록을 보여줄 수 있게 한다 — conv_id별 첫 질문(미리보기)·제목·턴 수·
    마지막 활동 시각만 집계할 뿐 conversation_log에 새로 쓰지 않는다. title은
    conversation_meta에 캐시된 값이 있으면 그걸 쓰고(set_conversation_title),
    없으면(과거 대화 등) 프론트엔드가 first_query로 대체해 표시한다."""
    conn = _conn()
    rows = conn.execute(
        """SELECT c.conv_id, COUNT(*) AS turn_count, MAX(c.ts) AS last_ts,
                  (SELECT query FROM conversation_log c2
                   WHERE c2.conv_id = c.conv_id ORDER BY c2.turn_id LIMIT 1) AS first_query,
                  m.title AS title
           FROM conversation_log c
           LEFT JOIN conversation_meta m ON m.conv_id = c.conv_id
           GROUP BY c.conv_id
           ORDER BY last_ts DESC
           LIMIT ?""",
        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_conversation_title(conv_id: str, title: str) -> None:
    """대화 제목 캐시(최초 1회, demo/app.py가 첫 턴에만 호출). conversation_log
    행 자체는 건드리지 않는 별도 메타데이터라 채팅 로그 스키마와 독립적이다."""
    conn = _conn()
    conn.execute(
        """INSERT INTO conversation_meta (conv_id, title, created_at) VALUES (?,?,?)
           ON CONFLICT(conv_id) DO UPDATE SET title = excluded.title""",
        (conv_id, title, time.time()))
    conn.commit()
    conn.close()


def submit_feedback(conv_id, turn_id, thumb, reason=None):
    """reason: 👎일 때 데모 UI가 고정 후보(예: "근거 부족") 중 고른 짧은 이유.
    LLM 호출 없이 정적 후보를 그대로 저장만 한다 — 비용 없는 신호 보강."""
    conn = _conn()
    conn.execute(
        "INSERT INTO feedback (conv_id, turn_id, thumb, reason, ts) VALUES (?,?,?,?,?)",
        (conv_id, turn_id, thumb, reason, time.time()))
    conn.commit()
    conn.close()


def add_notification(level: str, title: str, message: str, *, conn=None) -> None:
    """갱신 사이클(scripts/run_update_cycle.py) 결과 알림 1건 추가. level은
    info|warning|error — 호출부가 의미를 정하고 여기서는 검증하지 않는다(그
    판정 로직은 run_update_cycle.py에 둬서 LLM 없이 단위 테스트하기 쉽게 함).
    데모의 종모양 알림 UI(GET /notifications)가 이걸 읽는다."""
    own = conn is None
    conn = conn or _conn()
    conn.execute(
        "INSERT INTO notifications (level, title, message, created_at, read) VALUES (?,?,?,?,0)",
        (level, title, message, time.time()))
    if own:
        conn.commit()
        conn.close()


def list_notifications(limit: int = 50) -> List[Dict[str, Any]]:
    """최신순으로 반환(읽기 전용) — 데모 종모양 패널이 그대로 보여줌."""
    conn = _conn()
    rows = conn.execute(
        """SELECT id, level, title, message, created_at, read
           FROM notifications ORDER BY created_at DESC LIMIT ?""",
        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_unread_notifications() -> int:
    conn = _conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE read = 0").fetchone()[0]
    conn.close()
    return n


def mark_notification_read(notification_id: int) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,))
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
