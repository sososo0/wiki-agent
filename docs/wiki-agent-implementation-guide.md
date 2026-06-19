# wiki-agent 구현 가이드 — Hermes(글루) + 직접 구현한 DE 파이프라인

> 자가 갱신형 에이전트 RAG. 대화 로그가 "LLM이 큐레이션하는 위키"를 자동 갱신하고,
> RAG 성능이 사이클마다 측정 가능하게 향상되는 self-improving 지식 시스템.
> **빌드 순서대로** 정리했습니다.
>
> 원칙: **핵심 DE 로직(마이닝·큐레이션·오염 게이트·평가)은 직접 구현**, Hermes는
> 서빙 에이전트 + 스케줄 트리거 역할로만 사용.

---

## 0. 핵심 설계 결정 (왜 이 순서로 짓나)

1. **RAG를 MCP 서버로 감싼다.** 검색·로깅을 독립 MCP 서버로 만들면 Hermes에 종속되지 않고,
   "내가 짠 코드"임이 명확해진다. Hermes는 이 MCP 도구를 호출만 한다.
2. **DE 파이프라인은 Hermes 바깥의 순수 Python 잡.** Hermes cron은 `run_update_cycle.py`를
   트리거하기만 한다. → 면접에서 "그건 프레임워크가 한 거 아닌가요?" 방어.
3. **평가 하니스를 가장 먼저.** before가 있어야 after를 증명한다.

> ⚠️ Hermes는 버전 변동이 빠르다(v0.13+). 아래 cron/MCP 등록 문법은 *대표 예시*이며,
> 시작 전 공식 문서로 현재 CLI/설정 문법을 확인할 것.

---

## 1. 레포 구조

```
wiki-agent/
├── core/                    # ★ 직접 구현 (포폴 핵심)
│   ├── wiki_store.py        # 스키마/커넥션/CRUD (SQLite+FTS5, pydantic 없이 표준 라이브러리만)
│   ├── retrieval.py         # 하이브리드 검색(BM25+dense+RRF) + cross-encoder rerank
│   └── pipeline/            # ★ 피드백 루프
│       ├── ingest.py
│       ├── mine.py          # gap 탐지(fact/correction은 의도적으로 미구현 — mine.py 주석 참고)
│       ├── curate.py        # LLM → 구조화 patch
│       ├── gate.py          # 오염 방지 게이트
│       ├── reindex.py       # 현재는 no-op(영속 임베딩 캐시 없음)
│       └── promote.py       # shadow→active + 롤백(=커밋 안 함)
├── eval/
│   ├── gold_set.jsonl       # 동결
│   ├── run_eval.py          # recall@k, mrr, correctness(LLM-as-judge)
│   └── baseline.json        # 기준 점수(--save-baseline 없이는 보존)
├── serving/
│   └── mcp_server.py        # RAG를 MCP 도구로 노출 (search_wiki, submit_feedback)
├── demo/                    # MCP 외 진입점 — 사람이 직접 써보는 FastAPI 채팅 데모
│   ├── app.py
│   └── static/index.html
├── scripts/
│   └── run_update_cycle.py  # 파이프라인 1사이클 오케스트레이션
├── tests/                   # pytest (기본 오프라인, RUN_SLOW_TESTS=1로 느린 테스트 포함)
├── conftest.py
├── Dockerfile                # infra/ 디렉터리 없이 루트 Dockerfile 하나로 데모 컨테이너화
└── test_client.py            # mcp 없이 서빙 로직만 검증
```

> 레포 폴더명은 `wiki-agent`(하이픈), Python import 패키지는 하이픈을 못 쓰므로
> `core` · `serving` 같은 무하이픈 모듈명을 그대로 사용한다.

> 위 트리는 실제 구현 기준이다. 이 가이드를 처음 쓸 때 구상했던 `core/db.py`+`core/schemas.py`
> 분리, `hermes/`(Hermes 설정), `infra/`(docker-compose·terraform)는 실제로는 만들지
> 않았다 — MVP 스코프에서 `core/wiki_store.py` 하나로 합치고, Hermes 연결은 레포 밖
> `~/.hermes/config.yaml`로, 배포는 루트 `Dockerfile` 하나로 충분히 끝났기 때문이다.
> 아래 빌드 순서 설명의 파일 경로보다 위 트리를 신뢰할 것.

---

## 2. Step 1 — 데이터 레이어 (스키마부터 고정)

Postgres + pgvector 권장(엔트리 메타와 벡터를 한 곳에서 버전 관리). DDL 예:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE wiki_entry (
  entry_id      TEXT PRIMARY KEY,
  topic         TEXT NOT NULL,
  canonical     TEXT NOT NULL,
  body_md       TEXT,
  provenance    TEXT CHECK (provenance IN
                  ('doc_verified','curated_from_logs','agent_generated')),
  confidence    REAL DEFAULT 0,
  version       INT  DEFAULT 1,
  supersedes    TEXT,
  status        TEXT DEFAULT 'shadow'      -- shadow | active | deprecated
                  CHECK (status IN ('shadow','active','deprecated')),
  embedding     vector(1024),              -- bge-m3
  sources       JSONB,
  updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON wiki_entry USING hnsw (embedding vector_cosine_ops);

CREATE TABLE conversation_log (
  conv_id TEXT, turn_id INT, query TEXT, answer TEXT,
  retrieved JSONB, agent_confidence REAL, escalated BOOL,
  ts TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (conv_id, turn_id)
);

CREATE TABLE feedback (
  conv_id TEXT, turn_id INT, explicit JSONB, implicit JSONB
);
```

> 골드셋(`eval/gold_set.jsonl`)은 파일로 동결하고 파이프라인이 절대 수정 못 하게 한다.

---

## 3. Step 2 — 평가 하니스 (먼저!)

```python
# eval/run_eval.py
def evaluate(retriever, gold, k=5):
    recall, mrr, correct = 0, 0, 0
    for ex in gold:
        hits = retriever.search(ex["q"], k)          # [entry_id...]
        ids = [h.entry_id for h in hits]
        # retrieval
        if set(ex["gold_entry_ids"]) & set(ids): recall += 1
        for rank, eid in enumerate(ids, 1):
            if eid in ex["gold_entry_ids"]: mrr += 1/rank; break
        # answer correctness: LLM-as-judge(답변 vs gold_answer + must_contain)
        correct += judge_answer(generate(ex["q"], hits), ex)
    n = len(gold)
    return {"recall@k": recall/n, "mrr": mrr/n, "correctness": correct/n}
```

이 함수가 모든 사이클·shadow/active 비교의 단일 기준점이 된다.

---

## 4. Step 3 — 검색 모듈 (프레임워크 무관, 독립 테스트)

```python
# core/retrieval.py
class WikiRetriever:
    def search(self, query, k=5):
        dense = self.vector_search(query, k*4)        # pgvector cosine
        sparse = self.bm25_search(query, k*4)         # OpenSearch/pg_trgm
        fused = reciprocal_rank_fusion(dense, sparse) # 하이브리드
        reranked = self.cross_encoder.rerank(query, fused)[:k]
        # active 엔트리만, confidence 가중
        return [r for r in reranked if r.status == "active"]
```

여기까지는 Hermes 없이 `pytest`로 검증 가능 — 그래야 이후 문제 원인이 분리된다.

---

## 5. Step 4 — MCP 서버로 감싸기 + Hermes 연결 (서빙)

### 5.1 RAG를 MCP 도구로 노출 (+ 로깅을 여기서)
```python
# serving/mcp_server.py  (MCP 표준 서버)
from mcp.server import Server
server = Server("wiki-agent")

@server.tool()
def search_wiki(query: str, k: int = 5) -> dict:
    hits = retriever.search(query, k)
    log_retrieval(query, hits)          # ← conversation_log/retrieved 적재
    return {"results": [h.dict() for h in hits]}

@server.tool()
def submit_feedback(conv_id: str, turn_id: int, thumb: str):
    save_feedback(conv_id, turn_id, explicit={"thumb": thumb})
```

### 5.2 Hermes가 이 MCP 서버를 도구로 사용
Hermes는 MCP 서버 연동을 지원하므로, 위 서버를 등록하고 시스템 프롬프트로
"답변은 반드시 `search_wiki` 결과에 근거하고 entry_id를 인용, 근거 없으면 escalate"를 강제한다.

```bash
# (대표 예시 — 실제 문법은 현재 docs 확인)
hermes tools            # MCP 서버 등록/활성화
hermes model            # provider/model 선택
hermes                  # 대화 시작 → search_wiki 호출 확인
```

> 포인트: **에이전트의 "지능"은 Hermes가, "지식 접근"은 내 MCP 서버가** 담당. 역할이 깨끗이 갈린다.

---

## 6. Step 5 — 로깅

서빙 경로의 모든 턴을 `conversation_log`에 적재. 암묵 신호(재질문·동일질문 반복·짧은 dwell)는
세션 단위로 후처리(Step 1 ingest에서 계산). Langfuse/Phoenix로 트레이싱을 붙이면 수집과
관측을 동시에 해결.

---

## 7. Step 6 — 피드백 파이프라인 (★ 직접 구현, 프로젝트의 심장)

각 파일 = 순수 함수. Hermes 무관. 테스트 가능.

### 7.1 Ingest (`pipeline/ingest.py`)
raw 로그 → 대화 단위 묶기 → 암묵 신호 계산 → 저품질/PII/중복 필터.

### 7.2 Mine — gap 탐지 (`pipeline/mine.py`)
```python
def detect_gaps(logs, min_cluster=5):
    qs = [l.query for l in logs]
    embs = embed(qs)
    labels = HDBSCAN(min_cluster_size=min_cluster).fit_predict(embs)
    gaps = []
    for c in set(labels) - {-1}:
        members = [l for l, lab in zip(logs, labels) if lab == c]
        fail_rate = mean(m.escalated or thumb_down(m) for m in members)
        if len(members) >= min_cluster and fail_rate > 0.3:   # 자주 묻는데 실패↑
            gaps.append({"cluster": c, "freq": len(members),
                         "fail_rate": fail_rate,
                         "examples": [m.query for m in members[:5]]})
    return gaps
```

### 7.3 Mine — fact / correction
LLM로 대화를 훑어 "검증 가능한 새 사실"과 "기존 위키와 모순되는 정정"을 추출.
출력은 후보 목록(엔트리 매핑 + 근거 conv_id 포함).

### 7.4 Curate (`pipeline/curate.py`) — 직접 쓰지 말고 patch 생성
```python
CURATE_PROMPT = """너는 지식베이스 큐레이터다. 아래 후보로 위키 엔트리 patch를 만들어라.
출력은 JSON만: {{"op":"create|update|merge","entry_id":...,"canonical":...,
"body_md":...,"sources":[...],"reason":...}}
기존 엔트리: {existing}\n후보 근거: {candidate}"""

def curate(candidate):
    raw = llm(CURATE_PROMPT.format(...))
    patch = parse_json(raw)        # DB에 바로 쓰지 않음! 게이트로 넘김
    return patch
```

### 7.5 Gate (`pipeline/gate.py`) — ★ 오염 방지 (차별화 포인트)
```python
def passes_gate(patch, today_writes):
    # 1) provenance 규칙: 에이전트 생성 단독 승격 금지
    if patch.provenance == "agent_generated" and not has_verified_source(patch):
        return False
    # 2) 사실성: 답변이 근거 소스에 grounding 되는지 (LLM-as-judge)
    if grounding_score(patch) < 0.7: return False
    # 3) 모순/중복: 기존 엔트리와 NLI 모순 또는 과도 유사
    if nli_contradicts_existing(patch) or near_duplicate(patch):
        route_to_human_review(patch); return False
    # 4) 출처 다양성: 단일 대화 1건만으로 사실 승격 금지
    if patch.op != "update" and source_count(patch) < 2: return False
    # 5) 폭주 방지: 일일 신규 엔트리 상한
    if today_writes >= DAILY_CAP: return False
    return True
```
통과한 patch만 `status='shadow'`로 DB에 반영.

---

## 8. Step 7 — 재색인 + 평가 게이트/승격

```python
# pipeline/reindex.py : 변경된 엔트리만 재임베딩·업서트 (증분)
# pipeline/promote.py
def promote_if_better():
    base = evaluate(retriever_on('active'), gold)
    cand = evaluate(retriever_on('active+shadow'), gold)   # shadow 포함
    if cand["correctness"] >= base["correctness"] and \
       cand["recall@k"]   >= base["recall@k"]:             # 회귀 없음
        activate_shadow()                                   # shadow→active
    else:
        rollback_shadow()                                   # 나쁜 갱신 폐기
    log_metrics(cycle_id, base, cand)                       # money chart 데이터
```

---

## 9. Step 8 — 오케스트레이션 (Hermes cron이 트리거)

`scripts/run_update_cycle.py`가 Step 6~8을 순서대로 호출:
```python
def run_update_cycle():
    logs   = ingest()
    cands  = detect_gaps(logs) + mine_facts(logs) + mine_corrections(logs)
    today  = 0
    for c in cands:
        patch = curate(c)
        if passes_gate(patch, today):
            write_shadow(patch); today += 1
    reindex_changed()
    promote_if_better()
```

Hermes cron이 이 스크립트를 주기 실행(예: 매일 02:00). 무거운 ETL을 더 키울 거면 Airflow로
승격하되, MVP는 cron+스크립트로 충분.
```bash
# (대표 예시 — 현재 docs 확인) Hermes 스케줄 잡으로 등록
hermes cron add --schedule "0 2 * * *" --cmd "python scripts/run_update_cycle.py"
```

> Hermes의 Curator(주기적으로 약한 지식을 재작성하는 백그라운드 루프)와 발상이 동일하다.
> README에 "Hermes의 self-improving 루프에서 착안해 도메인 지식 갱신에 적용"으로 서술하면 좋다.

---

## 10. Step 9 — 증명 (money chart)

`promote.py`가 사이클마다 적재한 지표를 모아 1장으로:
- x = cycle, y = recall@k / correctness ↑, escalation율 ↓
- 예: recall@5 0.61→0.79, escalation 18%→7% (구체 수치로 README에)

---

## 11. 빌드 체크포인트 (순서 지키기)

| # | 완료 기준 |
|---|---|
| 1 | DB 스키마 + 골드셋 30~50문항 동결 |
| 2 | `run_eval.py`로 베이스라인 점수 출력 |
| 3 | `WikiRetriever`가 pytest 통과 (Hermes 없이) |
| 4 | Hermes가 MCP `search_wiki` 호출해 인용 답변 |
| 5 | 모든 턴이 `conversation_log`에 적재 |
| 6 | `run_update_cycle.py` 수동 1회 성공 (shadow 생성) |
| 7 | 게이트가 나쁜 patch를 실제로 차단/롤백 |
| 8 | cron 무인 운영 + 사이클별 지표 우상향 그래프 |

---

## 12. 흔한 함정

- **DE 로직을 Hermes 스킬로 떠넘기기** → 포폴 신호 소실. 마이닝·큐레이션·게이트는 직접 코드로.
- **평가 없이 갱신부터** → 개선을 증명 못 함. Step 2를 절대 건너뛰지 말 것.
- **에이전트 답변을 검증 없이 KB에 재투입** → 오염 누적. 게이트(7.5)가 방어선.
- **shadow/active 분리 생략** → 나쁜 갱신이 prod 직격. 승격 게이트는 필수.
- **자율 에이전트 격리 소홀** → 코드 실행 권한을 갖는 런타임이므로 컨테이너/권한 격리 필수.

---

## 부록 — README 첫 문단 초안

> **wiki-agent** — 사용자와 에이전트의 대화 로그를 분석해 지식베이스를 스스로 갱신하는
> self-improving RAG. 정적 RAG와 달리 운영 중 발생하는 질문·실패·정정을 마이닝해
> 위키를 갱신하고, 오염 방지 게이트와 평가 기반 승격으로 안전하게 반영한다.
> 서빙은 Hermes Agent(MCP) 위에, 핵심 갱신 파이프라인은 자체 구현.
