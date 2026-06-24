# Wiki Agent: 자가 갱신형 에이전트 RAG 시스템 설계서

> 대화 로그를 분석해 "LLM이 큐레이션하는 위키"를 자동으로 갱신하고,
> 그 결과 RAG 성능이 갱신 사이클마다 측정 가능하게 향상되는 self-improving 지식 시스템.
>
> **포트폴리오 메시지**: "RAG를 만들었다"가 아니라 "대화 로그 → 지식베이스 자동 갱신 파이프라인을
> 설계·운영하고, 성능 향상을 지표로 증명했다."

---

## 0. 이 프로젝트가 증명하는 것 (왜 이렇게 설계하나)

| 어필 포인트 | 이 설계에서 보여주는 부분 |
|---|---|
| 데이터 엔지니어링 | 로그 수집 → 정제 → 지식 추출 → 재색인 파이프라인(DAG) 전체 |
| RAG 성능 개선 | 평가 하니스 기반 before/after를 사이클마다 우상향 그래프로 증명 |
| 동적 갱신 | 정적 RAG가 아니라 운영 중 로그가 KB를 바꾸는 피드백 루프 |
| 인프라 강점 | 오케스트레이션·컨테이너·IaC·관측(observability)·증분 색인 운영 |
| 엔지니어링 성숙도 | 피드백 오염(drift) 방지 설계 — "만든 사람"과 "운영 아는 사람"의 차이 |

> **권장 도메인**: 특정 OSS 프레임워크의 공식 문서 + GitHub Issues/Discussions를 코퍼스로.
> 이유: 이슈/토론은 시간이 지나며 "새 사실"과 "정정"이 자연 발생 → 동적 갱신을 보여주기에 최적.
> (대안: 사내 기술문서, 고객지원 KB)

---

## 1. 전체 아키텍처

```
사용자
  │ 질문
  ▼
① SERVING (실시간)
    Agent(tool-calling)
    retrieve(query) ──────────────▶ Wiki Store (④)
    generate(answer + citations) ─▶ 사용자
  │
  │ 모든 상호작용 로깅
  ▼
② LOG STORE (append-only)
    conversations / retrievals / feedback
  │
  ▼
③ FEEDBACK LOOP PIPELINE (배치/스케줄)
    1. Ingest
    2. Mine          gap / fact / correction
    3. Curate        LLM
    4. Quality Gate
    5. Reindex
    6. Eval & Promote   canary → prod
  │
  │ 회귀 없을 때만 승격
  ▼
④ WIKI STORE
    버전 · 출처 · 신뢰도 메타데이터
    + EVAL GOLD SET (회귀 탐지 기준)
  │
  └──▶ ①의 retrieve 대상으로 순환 (재색인) — 루프가 닫힌다
```

핵심: **Serving 경로**(실시간)와 **Feedback Loop**(배치)를 분리한다. 위키는 버전이 있는
정형 지식 저장소이고, 벡터 인덱스는 거기서 파생된다.

---

## 2. 데이터 모델 (스키마부터 고정)

### 2.1 Wiki Entry — 검색의 단위 (raw 청크 ❌, 큐레이션된 엔트리 ⭕)
```json
{
  "entry_id": "wiki_0421",
  "topic": "How to configure retry backoff",
  "canonical_answer": "...정제된 핵심 답변...",
  "body_md": "...상세 본문(검색·생성 컨텍스트로 사용)...",
  "sources": [
    {"type": "doc", "url": "...", "verified": true},
    {"type": "conversation", "conv_id": "c_1832", "verified": false}
  ],
  "provenance": "doc_verified | curated_from_logs | agent_generated",
  "confidence": 0.0,            // 0~1, 출처/검증에 따라 산정
  "version": 3,
  "supersedes": "wiki_0421@v2",  // 충돌 해소 추적
  "created_at": "...",
  "updated_at": "...",
  "embedding_model": "bge-m3@v1",
  "status": "active | shadow | deprecated"
}
```

### 2.2 Conversation Log (append-only)
```json
{
  "conv_id": "c_1832",
  "turn_id": 4,
  "query": "사용자 질문 원문",
  "retrieved": [{"entry_id":"wiki_0421","score":0.82,"used_in_answer":true}],
  "answer": "에이전트 답변",
  "answer_citations": ["wiki_0421"],
  "agent_confidence": 0.71,
  "escalated": false,            // 답을 못 만들어 fallback 했는가
  "timestamp": "..."
}
```

### 2.3 Feedback Signal
```json
{
  "conv_id": "c_1832", "turn_id": 4,
  "explicit": {"thumb": "down"},               // 명시적
  "implicit": {"follow_up_rephrase": true,     // 암묵적 신호
               "repeated_question": false,
               "dwell_short": true}
}
```

### 2.4 Eval Gold Set (회귀 방지의 기준)
```json
{"q":"...", "must_contain":["...","..."], "gold_answer":"...",
 "gold_entry_ids":["wiki_0421"], "frozen": true}
```
> 골드셋은 **갱신 루프가 절대 건드리지 못하게** 동결한다. 이게 있어야 "개선"을 증명한다.

---

## 3. Serving 경로 (실시간)

1. **Agent**: tool-calling 루프. 도구 = `search_wiki(query, k)`, (옵션) `web_search`, `escalate()`.
2. **Retrieve**: 하이브리드 검색 권장 — BM25(키워드) + dense(임베딩) → **rerank**(cross-encoder).
   `status="active"` 엔트리만 검색, `confidence` 가중.
3. **Generate**: 검색 컨텍스트 기반 답변 + **인용(entry_id)** 강제. 근거 없으면 `escalate()`.
4. **Log**: 위 2.2/2.3 스키마로 모든 턴 기록. 이게 다음 사이클의 연료.

---

## 4. Feedback Loop 파이프라인 (이 프로젝트의 심장 = DE)

각 단계 = 오케스트레이터의 task. 일·주 단위 스케줄.

**Stage 1 — Ingest & Sessionize**
로그를 raw zone에서 읽어 대화 단위로 묶고, 암묵 신호 계산(재질문, 짧은 dwell, 동일 질문 반복).
저품질·중복·PII 필터링.

**Stage 2 — Mine (지식 신호 추출)** — 3종류로 분류
- **Gap**: 자주 묻는데(빈도↑) 답이 나쁜(escalate↑/👎↑) 주제 → *새 엔트리 후보*
- **Fact**: 대화에서 새로 검증 가능한 사실 등장 → *기존 엔트리 보강 후보*
- **Correction**: 기존 위키와 충돌하는 정정(사용자 지적, 더 최신 소스) → *버전 갱신 후보*

> 군집화(질문 임베딩 클러스터링)로 같은 주제를 묶어 빈도/실패율을 집계하면 Gap이 깔끔히 잡힌다.

**Stage 3 — Curate (LLM 큐레이션)**
후보별로 LLM이 write / merge / dedupe 수행. 출력은 항상 **구조화된 patch**(엔트리 생성·수정안)로,
바로 DB에 쓰지 않는다. patch에는 근거 소스와 변경 사유를 첨부.

**Stage 4 — Quality Gate** (→ 6장 상세)
오염 방지의 핵심. 통과한 patch만 `shadow` 상태로 반영.

**Stage 5 — Reindex (증분)**
변경된 엔트리만 재임베딩·업서트. 인덱스 버전 태깅. (인프라 강점 노출 지점)

**Stage 6 — Eval & Promote**
shadow 인덱스에 대해 골드셋 평가. 지표가 회귀 없이 개선되면 `shadow → active` 승격(canary).
회귀 시 자동 롤백. 모든 결과를 메트릭 스토어에 적재.

---

## 5. 충돌 해소 & 버전 관리

- 새 patch가 기존 엔트리와 모순 → **출처 권위 + 최신성 + confidence**로 승자 결정.
- 패자는 삭제가 아니라 `deprecated`로 강등하고 `supersedes` 링크 유지 (감사 추적).
- 동률·애매하면 **사람 검토 큐**로 보냄(전량 자동화 X — 이게 더 현실적이고 어필 포인트).

---

## 6. 피드백 오염(Drift / Knowledge Collapse) 방지 — 차별화 포인트

> 순진하게 에이전트 답변을 그대로 KB에 다시 넣으면 오류가 누적된다. 아래를 "설계로" 막았다고 보여주면
> 운영 성숙도가 드러난다.

1. **Provenance 등급제**: `doc_verified > curated_from_logs > agent_generated`.
   `agent_generated` 단독으로는 절대 `active` 승격 불가(반드시 검증 소스로 뒷받침).
2. **품질 게이트 (Stage 4)**:
   - 사실성 체크(LLM-as-judge + 소스 grounding 점수)
   - 중복/모순 탐지(기존 엔트리와 임베딩 유사도 + NLI 모순 판정)
   - 신규 엔트리 일일 상한(폭주 방지)
3. **Canary 평가 게이트 (Stage 6)**: 골드셋 회귀 시 자동 롤백 → 나쁜 갱신이 prod에 못 들어감.
4. **사람 검토 큐**: 낮은 confidence·충돌·고위험 토픽만 사람에게. (전량 아님)
5. **출처 다양성 요구**: 단일 대화 1건만으로 사실 승격 금지(N건 또는 문서 교차확인).

---

## 7. 오케스트레이션 & 자동화

**기본**: Airflow 또는 Dagster DAG로 Stage 1~6를 스케줄(예: 매일 02:00). 실패 알림·재시도·증분 처리.

**Hermes Agent / OpenClaw의 역할 (선택적 자동화 레이어)**
- Hermes는 Nous Research의 오픈소스 자율 에이전트로 내장 **cron 스케줄 자동화 + MCP 서버 연동 +
  세션 간 메모리/스킬 자기개선** 루프를 가진다. OpenClaw는 그 전신(`hermes claw migrate` 경로 존재).
- 현실적 사용처: 무거운 ETL은 Airflow가, **"언제 갱신을 돌릴지/이상 징후 모니터링/리포트 전달"** 같은
  자율 운영 레이어를 Hermes cron으로. RAG 검색을 Hermes의 **MCP 도구**로 노출하면 에이전트 부분도
  이 프레임워크로 통합 가능.
- 포폴 스토리: "에이전트가 자기 지식을 스스로 갱신·운영한다"가 Hermes의 self-improving 루프와
  테마가 맞아떨어진다. (단, 핵심 파이프라인 로직은 직접 구현해 DE 역량을 보이고, Hermes는 glue로.)

---

## 8. 평가 프레임워크 (포폴의 하이라이트)

**오프라인 지표 (골드셋)**
- Retrieval: `recall@k`, `MRR`, rerank 전후 비교
- Answer: groundedness(충실도), answer correctness(골드 대비), citation 정확도
- RAGAS 또는 커스텀 LLM-as-judge

**온라인/운영 지표 (로그 기반)**
- escalation(미응답) 비율 ↓, 재질문률 ↓, 👎 비율 ↓
- KB coverage(질문 클러스터 대비 답변 가능 비율) ↑, staleness ↓

**📈 머니 차트**: x축 = 갱신 사이클(0~N), y축 = recall@k / correctness / escalation율.
사이클이 돌수록 우상향(escalation은 우하향)하는 그래프 한 장이 이 프로젝트의 결론.

> 그래서 **Phase 0에서 평가 하니스를 가장 먼저** 만든다. before가 있어야 after를 증명한다.

---

## 9. 기술 스택 (인프라 강점이 보이게)

| 레이어 | 선택지 |
|---|---|
| 오케스트레이션 | Airflow / Dagster / Prefect |
| 벡터 DB | Qdrant / pgvector / Milvus (증분 업서트·버전 관리) |
| 검색 | BM25(OpenSearch/Elasticsearch) + dense + cross-encoder rerank |
| 임베딩 | bge-m3 / e5 / OpenAI 등 (모델 버전 태깅) |
| 위키/메타 | Postgres (엔트리·버전·provenance) |
| 로그 스토어 | object storage(raw) + Postgres/ClickHouse(정형) |
| 관측/트레이싱 | **Langfuse / Phoenix(Arize)** — 에이전트+RAG 트레이싱이 곧 로그 수집 |
| 평가 | RAGAS / 커스텀 harness |
| 에이전트 자동화 | Hermes Agent (cron + MCP) — 선택 |
| 배포 | Docker / K8s / Terraform (본인 강점) |

---

## 10. 단계별 로드맵 (MVP → 풀)

- **Phase 0 — 평가 하니스 & 골드셋** (먼저!): 30~50문항 골드셋, recall@k·correctness 측정 코드.
- **Phase 1 — 베이스라인 에이전트 RAG**: 정적 위키 + 하이브리드 검색 + 인용 답변. 베이스라인 점수 고정.
- **Phase 2 — 로깅 인프라**: 2.2/2.3 스키마로 모든 상호작용 적재. Langfuse 연동.
- **Phase 3 — 오프라인 피드백 루프(핵심)**: Stage 1~3 + 5. 수동 트리거로 1사이클 성공시키기.
- **Phase 4 — 오염 방지 게이트**: Stage 4 + 6(canary/롤백). provenance·confidence 도입.
- **Phase 5 — 자동화**: Airflow 스케줄 + (선택) Hermes cron/MCP. 무인 운영.
- **Phase 6 — 대시보드 & 증명**: 사이클별 지표 추적, 머니 차트 생성, README 서술.

> 시간이 빠듯하면 Phase 0·1·3·6만으로도 "동적 갱신 + 개선 증명"이라는 핵심 서사는 완성된다.
> Phase 4·5는 차별화 가산점.

---

> 위 11(README에 꼭 넣을 것)/12(스트레치 골) 섹션은 실제 README 작성 전 메모였고, 이제
> README(완료/구현예정)와 docs/demo-operations.md가 그 역할을 대신해 정리 시 제거함.

### 한 줄 요약
정적 RAG가 아니라 **"로그가 위키를 갱신 → 재색인 → 평가 게이트 → 승격"**의 닫힌 루프를 만들고,
오염을 막으면서 지표가 사이클마다 좋아지는 걸 그래프로 증명하는 프로젝트.
