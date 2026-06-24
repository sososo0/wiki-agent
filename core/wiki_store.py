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
from typing import List, Dict, Any, Optional, Tuple

import numpy as np

try:
    from core import retrieval
    from core.lru_cache import LRUCache
except ImportError:        # python core/wiki_store.py 로 직접 실행할 때
    import retrieval
    from lru_cache import LRUCache

DB_PATH = os.environ.get("WIKI_AGENT_DB", "wiki_agent.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_entry (
  entry_id    TEXT PRIMARY KEY,
  topic       TEXT NOT NULL,
  canonical   TEXT NOT NULL,
  body_md     TEXT,
  provenance  TEXT DEFAULT 'doc_verified',   -- doc_verified | curated_from_logs | curated_from_web | agent_generated
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

-- 사이클(scripts/run_update_cycle.py)마다 1행 — promote.promote_if_better()가
-- 본 "지금 active 상태"의 골드셋 지표를 시계열로 쌓아, 여러 사이클에 걸쳐 위키가
-- 실제로 좋아지는지(혹은 안 좋아지는지) 추이로 보여준다. notifications와 같은
-- 이유로 KB(wiki_entry)와 무관한 운영 데이터.
CREATE TABLE IF NOT EXISTS cycle_history (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                      REAL NOT NULL,
  mined                   INTEGER,
  shadow_count            INTEGER,
  promoted                INTEGER,   -- 0 | 1
  activated_count         INTEGER,
  recall_at_k             REAL,
  mrr                     REAL,
  correctness             REAL,
  escalation_correctness  REAL       -- NULL 가능(골드셋에 unanswerable 문항이 없으면)
);

-- KB(wiki_entry)가 아니라 그래프 화면(/static/graph.html) 표시용 파생 캐시 —
-- 검색/평가는 절대 이 테이블을 안 본다(원본 topic/canonical/body_md는 그대로
-- 영어로 남아 검색·평가에 쓰인다). entry_id+version으로 키를 잡아 콘텐츠가
-- 실제로 바뀔 때만 무효화된다(core/graph.py의 임베딩 캐시와 동일 철학).
-- 쓰기는 scripts/translate_wiki_labels.py(신뢰된 오프라인 스크립트)에서만.
CREATE TABLE IF NOT EXISTS translation_cache (
  entry_id   TEXT NOT NULL,
  lang       TEXT NOT NULL DEFAULT 'ko',
  version    INTEGER NOT NULL,
  topic      TEXT,
  canonical  TEXT,
  body_md    TEXT,
  updated_at REAL,
  PRIMARY KEY (entry_id, lang)
);

-- core/retrieval.py·core/graph.py가 매 쿼리/그래프 빌드마다 처음부터 다시 인코딩
-- 하던 dense 임베딩을 영속화한다(PersistentEmbeddingCache가 이 테이블을 읽고/쓴다).
-- entry_id만 PK — model이 바뀌면(WIKI_AGENT_EMBED_MODEL) get_embedding이 model
-- 불일치를 캐시미스로 취급하고 다음 set_embedding(INSERT OR REPLACE)이 새 모델
-- 벡터로 자연스럽게 덮어쓴다. translation_cache와 동일 철학(entry_id+version 키).
CREATE TABLE IF NOT EXISTS wiki_embedding (
  entry_id   TEXT PRIMARY KEY,
  model      TEXT NOT NULL,
  version    INTEGER NOT NULL,
  vector     BLOB NOT NULL,
  updated_at REAL
);

-- wiki_entry는 status로(거의 모든 list_*), retrieval_log/feedback은 ts로(--window-days
-- 윈도잉) 매번 필터링되는데 인덱스가 없으면 매번 풀스캔이다. 코퍼스/로그가 작을 때는
-- 안 보이지만 커지면 가장 먼저 느려질 지점 — CREATE INDEX는 멱등적이라 매 init_db()
-- 호출/기존 DB에도 안전하게 추가된다.
CREATE INDEX IF NOT EXISTS idx_wiki_entry_status_updated_at ON wiki_entry(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_ts ON retrieval_log(ts);
CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(ts);
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
    # timeout=5.0 -> SQLite의 busy_timeout(5000ms). 동시 쓰기가 겹치면 즉시
    # "database is locked"로 죽는 대신 5초간 재시도한다(이 모듈은 매 호출마다
    # 새 커넥션을 여는 구조라 매번 지정해야 함 — journal_mode와 달리 커넥션별 설정).
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
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
    # WAL은 DB 파일에 영구 저장되는 설정이라 프로세스 시작 시 1회면 충분(매 _conn()
    # 호출마다 안 해도 됨) — 쓰기 도중에도 읽기가 안 막히게 해서(쓰기끼리는 여전히
    # 1개만, SQLite 근본 한계) 동시 접근 시 락 경합을 크게 줄인다.
    conn.execute("PRAGMA journal_mode=WAL")
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


def get_entry(entry_id: str) -> Optional[Dict[str, Any]]:
    """status 무관하게 entry_id 하나를 조회. core/pipeline/reindex.py가 막 쓰여진
    엔트리(아직 status='shadow'일 수 있음)의 현재 version/텍스트를 읽어 임베딩을
    영속화하는 데 쓴다 — list_active_entries 등은 특정 status로만 필터링해 이
    용도에 안 맞는다."""
    conn = _conn()
    row = conn.execute(
        """SELECT entry_id, topic, canonical, body_md, provenance, confidence,
                  version, sources, tier, status
           FROM wiki_entry WHERE entry_id = ?""", (entry_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return {**dict(row), "sources": json.loads(row["sources"]) if row["sources"] else []}


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


def purge_old_logs(retention_days: float = 30) -> Dict[str, int]:
    """retrieval_log/feedback에서 retention_days보다 오래된 행을 지운다. --window-days
    (mine_gaps가 보는 범위, 기본 14일)는 읽기 필터일 뿐 삭제하지 않으므로 두 테이블은
    영원히 자란다 — 이 함수가 그 retention을 실제로 집행한다(scripts/purge_old_logs.py
    가 호출하는 신뢰된 오프라인 작업, 자동 스케줄에는 기본으로 안 묶음).

    conversation_log는 의도적으로 제외 — `/conversations` UI가 보여주는 사용자
    대화 기록이라 마이닝 입력 로그(retrieval_log/feedback)와는 보존 정책이 달라야
    한다고 판단."""
    cutoff = time.time() - retention_days * 86400
    conn = _conn()
    retrieval_deleted = conn.execute(
        "DELETE FROM retrieval_log WHERE ts < ?", (cutoff,)).rowcount
    feedback_deleted = conn.execute(
        "DELETE FROM feedback WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    conn.close()
    return {"retrieval_log_deleted": retrieval_deleted, "feedback_deleted": feedback_deleted}


def backup_db(dest_path: str) -> None:
    """SQLite Online Backup API(Connection.backup)로 현재 DB를 dest_path에 통째로
    복제한다. 단순 파일 복사(cp)는 WAL 모드에서 최근 커밋이 아직 -wal 파일에만 있을
    수 있어 일관성이 깨질 위험이 있는데, backup()은 그런 경우에도 일관된 스냅샷을
    보장한다. scripts/backup_db.py(신뢰된 오프라인 스크립트)에서만 호출."""
    conn = _conn()
    dest = sqlite3.connect(dest_path)
    conn.backup(dest)
    dest.close()
    conn.close()


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


def get_translations(entry_ids: List[str], lang: str = "ko") -> Dict[str, Dict[str, Any]]:
    """그래프 화면 표시용 번역 캐시 조회. {entry_id: {version, topic, canonical,
    body_md}} 반환 — 호출부(core/graph.py)가 version을 노드의 현재 version과
    비교해 콘텐츠가 그새 바뀌었으면 무시하고 영어로 폴백한다."""
    if not entry_ids:
        return {}
    conn = _conn()
    placeholders = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"""SELECT entry_id, version, topic, canonical, body_md
            FROM translation_cache WHERE lang = ? AND entry_id IN ({placeholders})""",
        (lang, *entry_ids)).fetchall()
    conn.close()
    return {r["entry_id"]: dict(r) for r in rows}


def set_translation(entry_id, version, topic, canonical, body_md, *,
                     lang="ko", conn=None) -> None:
    """번역 캐시 upsert(entry_id+lang 키). scripts/translate_wiki_labels.py
    (신뢰된 오프라인 스크립트)에서만 호출 — 데모 서빙 경로는 절대 안 씀."""
    own = conn is None
    conn = conn or _conn()
    conn.execute(
        """INSERT INTO translation_cache
           (entry_id, lang, version, topic, canonical, body_md, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(entry_id, lang) DO UPDATE SET
             version=excluded.version, topic=excluded.topic,
             canonical=excluded.canonical, body_md=excluded.body_md,
             updated_at=excluded.updated_at""",
        (entry_id, lang, version, topic, canonical, body_md, time.time()))
    if own:
        conn.commit()
        conn.close()


def get_embedding(entry_id: str, *, model: str = None) -> Optional[Tuple[int, np.ndarray]]:
    """영속화된 (version, vector)를 반환. model이 현재 설정(retrieval.EMBED_MODEL)과
    다른 행은 None(미스로 취급) — 임베딩 모델이 바뀌면 옛 모델 벡터를 쓰면 안 되므로,
    그냥 없던 것처럼 동작해 호출부(PersistentEmbeddingCache)가 재인코딩하게 한다."""
    model = model or retrieval.EMBED_MODEL
    conn = _conn()
    row = conn.execute(
        "SELECT model, version, vector FROM wiki_embedding WHERE entry_id = ?",
        (entry_id,)).fetchone()
    conn.close()
    if row is None or row["model"] != model:
        return None
    vector = np.frombuffer(row["vector"], dtype=np.float32).copy()
    return row["version"], vector


def set_embedding(entry_id: str, version: int, vector: np.ndarray, *,
                   model: str = None, conn=None) -> None:
    """임베딩 캐시 upsert(entry_id 키). vector는 float32 bytes로 직렬화해 저장."""
    model = model or retrieval.EMBED_MODEL
    own = conn is None
    conn = conn or _conn()
    blob = np.asarray(vector, dtype=np.float32).tobytes()
    conn.execute(
        """INSERT INTO wiki_embedding (entry_id, model, version, vector, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(entry_id) DO UPDATE SET
             model=excluded.model, version=excluded.version,
             vector=excluded.vector, updated_at=excluded.updated_at""",
        (entry_id, model, version, blob, time.time()))
    if own:
        conn.commit()
        conn.close()


class PersistentEmbeddingCache:
    """core/lru_cache.LRUCache와 동일한 get()/__setitem__ 계약을 만족하는 드롭인
    대체물 — core/retrieval.py·core/graph.py는 무수정으로 그대로 받아 쓸 수 있다.

    메모리(LRUCache)에서 미스가 나면 DB(wiki_embedding)를 먼저 보고, 거기도 없을
    때만 호출부가 embed_fn으로 재인코딩한다. 프로세스가 재시작되거나(데모/MCP
    재기동) 서로 다른 프로세스(데모 서빙 vs MCP 서버)가 같은 DB를 봐도 한 번
    인코딩된 엔트리는 재인코딩하지 않는다."""

    def __init__(self, maxsize: int = 2000, model: str = None):
        self._mem = LRUCache(maxsize=maxsize)
        self.model = model or retrieval.EMBED_MODEL

    def get(self, key, default=None):
        hit = self._mem.get(key)
        if hit is not None:
            return hit
        row = get_embedding(key, model=self.model)
        if row is None:
            return default
        self._mem[key] = row
        return row

    def __setitem__(self, key, value):
        self._mem[key] = value
        version, vector = value
        set_embedding(key, version, vector, model=self.model)

    def __len__(self):
        return len(self._mem)

    def __contains__(self, key):
        return key in self._mem


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


def add_cycle_history(
    mined: int, shadow_count: int, promoted: bool, activated_count: int,
    recall_at_k: float, mrr: float, correctness: float,
    escalation_correctness: float = None, *, conn=None,
) -> None:
    """갱신 사이클(scripts/run_update_cycle.py) 1회 실행 후의 골드셋 지표 1행을
    기록 — promoted면 candidate(새로 active가 된 상태), 아니면 base(그대로인 현재
    active 상태)의 지표를 넘겨받는다(호출부 책임). 데모의 "사이클 추이" 페이지
    (GET /cycle-history)가 시계열로 보여줌."""
    own = conn is None
    conn = conn or _conn()
    conn.execute(
        """INSERT INTO cycle_history
           (ts, mined, shadow_count, promoted, activated_count,
            recall_at_k, mrr, correctness, escalation_correctness)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (time.time(), mined, shadow_count, int(promoted), activated_count,
         recall_at_k, mrr, correctness, escalation_correctness))
    if own:
        conn.commit()
        conn.close()


def list_cycle_history(limit: int = 100) -> List[Dict[str, Any]]:
    """시간순(ts ASC)으로 반환 — list_notifications()는 최신순 피드용이라 DESC인
    것과 의도적으로 다름. 추이 차트는 과거->현재 순서로 그려야 자연스럽다."""
    conn = _conn()
    rows = conn.execute(
        """SELECT id, ts, mined, shadow_count, promoted, activated_count,
                  recall_at_k, mrr, correctness, escalation_correctness
           FROM cycle_history ORDER BY ts ASC LIMIT ?""",
        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
