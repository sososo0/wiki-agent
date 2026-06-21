# wiki-agent

자가 갱신형(self-updating) 에이전트 RAG. 대화/검색 로그가 위키 지식베이스를
스스로 갱신하고, 그 효과를 eval 하니스로 정량 증명하는 프로젝트.

## 핵심 아이디어

1. **서빙**: MCP 서버가 `search_wiki`/`submit_feedback`만 노출. 검색·대화·피드백은
   전부 SQLite에 로그로 적재(이 로그가 갱신 파이프라인의 연료).
2. **평가**: 모든 변경(검색 알고리즘 교체, 위키 갱신 등)은 `eval/run_eval.py`의
   recall@k / mrr / correctness 로 before/after 비교 필수 — 감(感)이 아니라
   숫자로 회귀 여부 판단.
3. **피드백 파이프라인**: `retrieval_log`/`feedback` 로그를 마이닝(mine) →
   patch 초안 생성(curate) → 오염 게이트(gate) 통과 → `shadow`로만 반영 →
   eval 회귀가 없을 때만 `active` 승격(promote). 회귀가 있으면 아무 것도
   커밋 안 함("롤백은 애초에 커밋하지 않음으로 구현").

## 프로젝트 구조

```
core/
├── wiki_store.py        지식 저장소(SQLite+FTS5). MCP가 호출하는 단일 진입점.
├── retrieval.py         BM25 + dense 임베딩 + RRF 융합 + cross-encoder rerank
└── pipeline/
    ├── ingest.py        retrieval_log/feedback 정규화·집계
    ├── mine.py          "gap"(빈도↑ + 검색 신뢰도↓) 탐지
    ├── parse.py         마크다운 파일/디렉터리 → 헤더 기준 섹션 리스트(문서 ingestion 입력단)
    ├── chunk.py         섹션 → max_chars 상한 청크 + to_doc_candidates()로 mine 출력과 같은 모양 변환
    ├── curate.py        gap/문서청크 → 위키 엔트리 patch 초안(LLM, curated_from_logs/doc_verified)
    ├── dedupe.py        문서청크 entry_id 결정 + chunk_hash 비교로 skip/create/update 분기(멱등성)
    ├── gate.py          오염 게이트: provenance/일일상한/출처다양성/중복/grounding 5단계
    │                    (grounding은 자기모순·기존 검증 엔트리와의 모순·환각을 직접 판정)
    ├── reindex.py       재색인 지점(현재는 영속 임베딩 캐시가 없어 no-op)
    └── promote.py       shadow 후보를 시뮬레이션 평가 후 회귀 없을 때만 active 승격
eval/
├── run_eval.py          recall@k/mrr/correctness 평가 하니스
├── gold_set.jsonl       20문항 골드셋(동결)
└── baseline.json        기준 점수(명시적 --save-baseline 없이는 보존)
serving/
└── mcp_server.py        MCP stdio 서버 (search_wiki, submit_feedback만 노출)
demo/                    MCP 외 진입점 — 사람이 직접 써보는 FastAPI 채팅 데모
├── app.py               search_wiki 검색 결과만 근거로 답변하는 고정 RAG 파이프라인
└── static/index.html    빌드 단계 없는 채팅 UI(순수 HTML+JS)
scripts/
├── run_update_cycle.py  피드백 파이프라인 1사이클 오케스트레이션
└── ingest_doc.py        문서 ingestion 파이프라인 오케스트레이션(parse→chunk→curate→gate→shadow→promote)
tests/                   pytest (기본 오프라인/무료, 느린 통합 테스트는 RUN_SLOW_TESTS=1로 가드)
docs/                    설계/구현/운영 문서(필요할 때 직접 읽을 것)
conftest.py
Dockerfile               데모 컨테이너 빌드 골격
test_client.py           mcp 없이 서빙 로직만 검증
```

## 빠른 시작

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY 채우기
```

> 시스템에 Python이 여러 개 설치되어 있으면(pyenv, Homebrew 등) `uvicorn: command not found`나
> `No module named ...` 오류가 날 수 있다. 항상 위 venv를 `source venv/bin/activate`로
> 활성화한 터미널에서 명령을 실행할 것 — 새 터미널 탭을 열 때마다 다시 활성화해야 한다.

## 로컬에서 데모 배포하기

데모는 단일 SQLite 파일(`WIKI_AGENT_DB`)을 데이터 저장소로 사용. **채팅을 보낸 DB와
갱신 사이클을 돌리는 DB가 반드시 같은 파일이어야** 위키 갱신 확인 가능 — 둘이 다르면
사이클이 빈 로그를 보고 `mined gaps: 0`.

### A. uvicorn으로 직접 실행

```bash
WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload
```

브라우저에서 http://localhost:8000 접속 → 채팅 UI에서 질문 전송.

### B. Docker로 실행

```bash
docker build -t wiki-agent-demo .
mkdir -p /tmp/wiki-agent-docker-data
docker run -d --name wiki-agent-demo \
  -p 8000:8000 \
  -e WIKI_AGENT_DB=/data/wiki_agent.db \
  --env-file .env \
  -v /tmp/wiki-agent-docker-data:/data \
  wiki-agent-demo
```

`--env-file .env`로 `ANTHROPIC_API_KEY` 로드(`.env`에 키가 채워져 있어야 함).
`-d --name`으로 백그라운드 실행 + 이름 고정 — `docker logs -f wiki-agent-demo`로 로그
확인, `docker stop wiki-agent-demo`로 종료.

`-v` 볼륨 마운트 없이 실행하면 컨테이너 종료 시 DB도 함께 삭제. 이후 같은 DB로
갱신 사이클을 돌리려면 컨테이너 밖에서 `WIKI_AGENT_DB=/tmp/wiki-agent-docker-data/wiki_agent.db`로
같은 파일을 지정(아래 "위키 자가 갱신 확인하기" 참고).

## 위키 자가 갱신 확인하기 (LLM Wiki + RAG 결합 데모)

이 프로젝트의 핵심 서사 — "대화 로그가 위키를 스스로 갱신한다" — 를 직접 눈으로
확인하는 절차. `core/pipeline/mine.py`의 `mine_gaps()`는 **정확히 같은 문구**의
질문이 **3번 이상**(`min_freq`) 반복되고, 그 질문의 검색 신뢰도(cross-encoder rerank
점수)가 **평균적으로 음수**(`score_threshold=0.0` 미만)일 때만 "위키에 없는 주제"로
탐지 — 둘 다 만족해야 함(빈도만 채우거나 점수만 낮은 경우는 제외).

0. 아래 명령의 `$DB`는 **위에서 데모를 어떻게 띄웠는지에 따라 다름** — 채팅이 실제로
   쓰는 파일과 다른 경로를 쓰면 사이클이 빈 로그를 보고 `mined gaps: 0`.
   - A(uvicorn)로 띄웠다면 `$DB` = `/tmp/demo.db`
   - B(Docker)로 띄웠다면 `$DB` = `/tmp/wiki-agent-docker-data/wiki_agent.db`
1. 데모 채팅(A 또는 B)에서, **현재 위키 5개 주제(재시도, rate limiting, connection
   pooling, circuit breaker, idempotency)와 무관한** 질문 하나를 골라 **정확히 같은
   문구로 3번 이상** 전송(복사-붙여넣기로, 패러프레이즈 금지). 예:
   `"How do I implement pagination for a large dataset?"`
2. 채팅에 쓴 것과 **같은** `$DB`로 갱신 사이클 1회 실행:
   ```bash
   WIKI_AGENT_DB=$DB python3 scripts/run_update_cycle.py
   ```
3. stdout에서 `mined gaps`(1 이상), `shadow written`(새 entry_id), `promote`
   (`promoted=True/False`, `activated=[...]`) 확인.
4. DB에서 직접 결과 확인:
   ```bash
   sqlite3 -separator " | " $DB \
     "select entry_id, topic, status, provenance from wiki_entry order by updated_at desc limit 10;"
   ```
   새 엔트리가 `status='shadow'`로만 있으면 게이트 통과 후 평가 회귀로 승격 전 상태,
   `status='active'`면 회귀 없이 승격까지 완료된 상태.
5. 같은 질문을 다시 데모 채팅에서 물어보면, 새로 승격된 엔트리를 근거로 답하는 것
   확인 가능.

**참고 — `promoted`는 실행마다 바뀔 수 있음.** `promote`가 비교하는 `correctness`
지표는 매 실행마다 실제 LLM 호출(`generate` + judge)로 채점하는 비결정적 지표라서,
같은 후보를 같은 사이클로 다시 돌려도 `correctness`가 ±1문항(골드셋 20문항 기준
0.05) 정도 흔들릴 수 있음. `recall@k`/`mrr`는 결정적이므로 그쪽이 변하면 진짜
회귀, `correctness`만 바뀌었다면 노이즈일 가능성이 높음 — 회귀 차단 자체는
설계대로 보수적으로 동작하는 것이라 승격 실패 ≠ 사이클 실패.

## 외부 문서로 위키 채우기 (문서 ingestion 파이프라인)

대화 로그 마이닝과는 별도로, 사람이 작성한 마크다운 문서를 직접 위키에 반영하는
경로. `core/pipeline/parse.py`가 ATX 헤더(`#`~`######`) 기준으로 문서를 섹션으로
나누고, `chunk.py`가 `max_chars`(기본 2000자) 상한으로 청크를 만든다. 각 청크는
`dedupe.py`가 `entry_id`(파일 경로+섹션 위치로 결정적)와 `chunk_hash`(내용 기반)를
비교해 콘텐츠가 그대로면 LLM 호출 없이 건너뛴다(skip) — 같은 문서를 몇 번 재실행해도
변경된 섹션만 비용이 든다. 게이트가 거부한 청크도 동일하게 `chunk_hash` 기준으로
기억해(`status="rejected"` 마커) 콘텐츠가 안 바뀌었으면 재실행 시 다시 큐레이션/judge에
돌리지 않는다(skip_rejected) — 문서 내용이 바뀌면(`chunk_hash` 변경) 자동으로 새 주소가
되어 다시 시도된다. 나머지는 기존 게이트/shadow/promote를 그대로 통과한다. provenance는
`doc_verified`(사람이 쓴 문서가 출처이므로 로그 마이닝의 `curated_from_logs`보다
신뢰도가 높음).

```bash
WIKI_AGENT_DB=/tmp/demo.db python scripts/ingest_doc.py docs/ --daily-cap 5
```

stdout에서 `parsed_files`/`skipped_chunks`/`skipped_rejected_chunks`/`llm_calls`/
`shadow_written`/`rejected`/`promote` 확인. 같은 명령을 다시 실행하면 `skipped_chunks` +
`skipped_rejected_chunks`가 전체 청크 수와 같고 `llm_calls: 0`, `shadow_written: []`,
`rejected: []`이어야 함(멱등성 — 게이트 거부분도 콘텐츠가 그대로면 재시도하지 않음).
문서 일부를 수정한 뒤
재실행하면 바뀐 섹션만 새 shadow 후보가 생긴다 — 이미 `active`인 엔트리의 내용이
바뀐 경우엔 같은 entry_id를 직접 덮어쓰지 않고 `{entry_id}_v{n}` + `supersedes`로
새 shadow를 만들어 게이트를 다시 거치게 한다(HARD CONSTRAINT: active 갱신도 반드시
shadow→eval→promote 경로를 탐).

**한계(1차 구현 범위 밖)**: 문서 구조가 바뀌어 섹션이 분리/병합되면 새 entry_id가
생기고 옛 entry_id는 자동으로 deprecated되지 않음(수동 정리 필요). `promote.py`의
회귀 체크는 골드셋 기준 recall@k/correctness만 보므로, 문서 ingestion 자체의 게이트
통과율 같은 별도 코퍼스 스케일링 지표는 아직 없음.

## 명령

| 목적 | 명령 |
|---|---|
| 서빙 로직 검증 | `python test_client.py` |
| 단위 테스트(오프라인, 기본) | `pytest` |
| 느린 통합 테스트 포함(실제 모델 로딩) | `RUN_SLOW_TESTS=1 pytest` |
| 검색 품질 평가 | `python eval/run_eval.py [--k 5] [--save-baseline] [--qualitative]` |
| 에이전틱 태스크 평가(진단용) | `python eval/agentic_eval.py [--max-turns 4]` |
| MCP 서버(stdio) | `python serving/mcp_server.py` |
| 피드백 파이프라인 1사이클 | `python scripts/run_update_cycle.py [--gold path] [--k 5]` |
| 문서 ingestion 파이프라인 | `python scripts/ingest_doc.py <path...> [--daily-cap N] [--min-sources 1]` |
| 데모 웹앱(채팅) | `WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload` |
| 위키 그래프 시각화 | 데모 실행 후 브라우저에서 `/static/graph.html` 접속 (`GET /graph`로 원본 JSON 확인 가능) |
| 데모 Docker 빌드/실행 | `docker build -t wiki-agent-demo .` 후 `docker run ...` |

### 서빙 로직 검증

```bash
python test_client.py
```

MCP를 거치지 않고 `core/wiki_store.py`의 검색·로깅·피드백 경로만 end-to-end로 검증.
정상이면 마지막 줄에 다음 출력:

```
ALL CHECKS PASSED ✅
```

### 단위 테스트

```bash
pytest                    # 오프라인(LLM/임베딩 모델은 스텁 주입), 기본. 무료·빠름
RUN_SLOW_TESTS=1 pytest   # 실제 sentence-transformers 모델까지 로딩하는 느린 테스트 포함
```

### 검색 품질 평가

```bash
python eval/run_eval.py                  # eval/baseline.json과 비교만(파일은 보존)
python eval/run_eval.py --k 10           # top-k를 바꿔서 평가
python eval/run_eval.py --save-baseline  # 이번 결과로 baseline.json을 덮어쓴다
```

예시 출력(검색 알고리즘을 바꾼 뒤 baseline과 비교한 경우):

```
gold set: 20 questions, k=5
  metric         before    after    delta
  recall@k        1.000    1.000   +0.000
  mrr              0.935    0.975   +0.040
  correctness      0.350    0.400   +0.050

baseline preserved (use --save-baseline to overwrite) -> eval/baseline.json
```

기본 `correctness`는 binary(yes/no) judge라 "왜 맞다/틀리다"의 정성적 근거가 없다.
`--qualitative`를 주면 같은 답변(재생성 없이 재사용)에 judge를 1회 더 불러
groundedness(근거 충실도)/completeness(필수 포인트 커버리지)/relevance(질문 적합도)를
1-5로 채점해 평균을 추가로 출력한다. `recall@k`/`correctness` 키와 계산식은 그대로라
`core/pipeline/promote.py`의 shadow→active 회귀 판정에는 영향 없음(옵트인 전용 확장).

```bash
python eval/run_eval.py --qualitative                              # rubric 평균만 stdout에 출력
python eval/run_eval.py --qualitative --qualitative-report out.json # 질문별 점수+rationale도 저장
```

### 에이전틱 태스크 평가(진단용)

`eval/run_eval.py`의 골드셋은 "질문 1개 → 검색 1회 → 정답 엔트리 1개"만 다루지만,
실제 서빙 에이전트(Hermes 등)는 `search_wiki`를 도구로 여러 번 호출해 서로 다른
엔트리를 조합해야 답할 수 있는 멀티홉 질문도 받는다. `eval/agentic_eval.py`는
`eval/agentic_gold_set.jsonl`(엔트리 2개 이상을 결합해야 풀리는 태스크)을 대상으로
ReAct 스타일 루프(검색할지/답할지 매 턴 결정, 최대 `--max-turns`회)를 돌려 이 능력만
별도로 측정한다.

```bash
python eval/agentic_eval.py               # max_turns=4(기본)로 멀티홉 태스크 평가
python eval/agentic_eval.py --max-turns 6 # 검색 턴 예산을 늘려서 평가
```

예시 출력:

```
agentic gold set: 6 multi-hop tasks, max_turns=4
  task_success_rate: 0.833
  avg_tool_calls:    2.000
  multihop_recall:   0.917

  [OK] (2 calls) 결제 요청이 타임아웃으로 실패했는데, 그대로 재시도해도 안전한가요?...
        gold=['wiki_0001', 'wiki_0005'] retrieved=['wiki_0001', 'wiki_0005']
  ...
```

**한계**: 진단/리포트 전용이며 `promote.py`의 승격 게이트에는 연결하지 않는다
(HARD CONSTRAINT: 게이트는 정확히 `"recall@k"`/`"correctness"` 키만 본다). baseline
저장/비교도 없다 — 멀티홉 능력의 변화를 사람이 수동으로 확인하기 위한 용도. seed
코퍼스가 5개 엔트리뿐이라 `k`가 작아도 검색 1~2회면 코퍼스 대부분이 잡혀
`multihop_recall`이 쉽게 1.0에 가까워진다(검색 단계의 한계가 아니라 코퍼스 크기의
한계) — 실제 신호는 `task_success_rate`(찾은 정보를 올바르게 종합했는지)에 더 있다.

### MCP 서버(stdio)

```bash
python serving/mcp_server.py
```

에러 없이 멈춰 있으면 정상 — Hermes 같은 MCP 클라이언트가 연결해 `search_wiki`/
`submit_feedback`을 호출하기 전까지 stdin/stdout으로 대기. 연결 절차는
[docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md) 참고.

### 피드백 파이프라인 1사이클

```bash
WIKI_AGENT_DB=/tmp/demo.db python scripts/run_update_cycle.py
```

`retrieval_log`/`feedback`을 마이닝→큐레이션→게이트→shadow 반영→평가 기반 승격까지
1회 실행. 예시 출력(실제 실행 결과):

```
mined gaps: 2
feedback: {'n': 10, 'down': 9, 'down_rate': 0.9}
daily_cap: 5
shadow written: ['wiki_gap_how_do_i_implement_pagination_for_a_larg_24eafc5c']
rejected: [{'entry_id': 'wiki_gap_api_6efe4691', 'reason': 'failed grounding/contradiction check'}]
promote: promoted=True activated=['wiki_gap_how_do_i_implement_pagination_for_a_larg_24eafc5c']
  base:      {'recall@k': 1.0, 'mrr': 0.975, 'correctness': 0.35}
  candidate: {'recall@k': 1.0, 'mrr': 0.975, 'correctness': 0.35}
```

`WIKI_AGENT_DB`는 채팅에서 쓴 DB와 **반드시 같은 파일**을 가리켜야 함 — 직접 새 gap을
만들어 이 출력을 재현해보는 절차는 위 "위키 자가 갱신 확인하기" 참고.

### 문서 ingestion 파이프라인

```bash
WIKI_AGENT_DB=/tmp/demo.db python scripts/ingest_doc.py docs/ --daily-cap 5
```

마크다운 파일/디렉터리를 파싱→청킹→큐레이션→게이트→shadow 반영→평가 기반 승격까지
1회 실행. 자세한 동작과 멱등성 확인 절차는 위 "외부 문서로 위키 채우기" 참고.

### 데모 웹앱 / Docker

```bash
WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload   # http://localhost:8000
```

```bash
docker build -t wiki-agent-demo .
docker run -d --name wiki-agent-demo \
  -p 8000:8000 \
  -e WIKI_AGENT_DB=/data/wiki_agent.db \
  --env-file .env \
  -v /tmp/wiki-agent-docker-data:/data \
  wiki-agent-demo
```

자세한 옵션과 주의사항(볼륨 마운트, DB 경로 일치)은 위 "로컬에서 데모 배포하기" 참고.

> **참고**: 답변 아래 "근거" 줄에 표시되는 entry_id는 현재 일반 텍스트일 뿐 클릭 가능한
> 링크는 아니다. 위키 항목 단건을 보여주는 상세 페이지/엔드포인트가 아직 없어서다 —
> 추후 `/static/graph.html`처럼 entry_id로 바로 이동하는 뷰가 추가되면 그때 링크화한다.

### 위키 그래프 시각화

`/static/graph.html`은 위키 엔트리를 라이프사이클 상태별(active/shadow/deprecated/
rejected)로 색칠한 노드와, 그 사이의 관계를 엣지로 그린 인터랙티브 그래프다.
"AI가 안전장치를 갖고 스스로 갱신하는 위키"라는 이 프로젝트의 핵심 서사(승격 대기 중인
후보, 게이트가 막은 콘텐츠, 과거 교체 이력)를 한 화면에서 보여준다. 데이터는
`core/graph.py`의 `build_graph()`(읽기 전용 파생 뷰, DB 쓰기 없음)가 만들고,
`GET /graph`가 그대로 JSON으로 반환한다.

```bash
WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload
# 브라우저로 http://localhost:8000/static/graph.html
```

`GET /graph` 응답 예시:

```json
{
  "nodes": [
    {"id": "wiki_0001", "topic": "Retry backoff strategy", "status": "active", ...},
    {"id": "wiki_gap_..._abcd", "topic": "...", "status": "shadow", "supersedes": null, ...}
  ],
  "edges": [
    {"source": "wiki_gap_..._abcd", "target": "wiki_0001", "type": "pending_update", "weight": 1.0},
    {"source": "wiki_0001", "target": "wiki_0005", "type": "similar", "weight": 0.62}
  ]
}
```

엣지 타입: `pending_update`(shadow 후보 → 교체하려는 active 대상, 승격 대기 중),
`superseded_by`(deprecated 엔트리 → 과거에 대체했던 대상), `similar`(임베딩 코사인
유사도 기반, 무방향).

**한계**: `supersedes` 컬럼은 마지막 한 단계만 가리켜서, 승격 이후 `superseded_by`
엣지로 "이게 무엇을 대체했는지"는 보이지만 v1→v2→v3 같은 다단계 버전 이력 전체를
보존하지는 않는다. `similar` 엣지는 사람이 정의한 토픽 분류가 아니라 코퍼스 원문의
임베딩 기반이라, 임베딩 모델이 바뀌면 엣지 구성도 바뀐다. 이 엔드포인트는 사람이 보는
데모 페이지 전용이며 MCP 서버에는 노출하지 않는다(에이전트가 그래프를 통해 KB 내부
상태를 추론/조작하는 경로를 새로 열지 않기 위함).

## HARD CONSTRAINTS

- 갱신 patch는 항상 `shadow`로만 반영.
- eval 회귀가 없을 때만 `active`로 승격, 회귀 시 아무 것도 커밋 안 함.
- `agent_generated` 출처의 엔트리는 검증된 source 없이는 절대 승격 안 함.
- 에이전트는 MCP를 통해 KB에 직접 쓰기 불가(쓰기 도구 비노출).
- DB 경로는 `WIKI_AGENT_DB`(절대경로) 환경변수로 주입(하드코딩 금지).

자세한 배경은 [CLAUDE.md](CLAUDE.md)를 참고.

## 문서

- 설계: [docs/self-updating-rag-design.md](docs/self-updating-rag-design.md)
- 구현 순서: [docs/wiki-agent-implementation-guide.md](docs/wiki-agent-implementation-guide.md)
- MCP-Hermes 서빙 연결: [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md)

## 현재 상태

**완료**

- **데이터/서빙 레이어** — SQLite+FTS5 지식 저장소, MCP 서버(`search_wiki`/`submit_feedback`),
  검색·대화·피드백 로깅까지 동작. (`core/wiki_store.py`, `serving/mcp_server.py`)
- **평가 하니스** — 골드셋 20문항 기준 recall@k/mrr/correctness를 계산하고, 기존
  `eval/baseline.json`과 before/after로 비교. 모든 변경은 이 숫자로 검증. 골드셋에
  KB가 답을 모르는 unanswerable 문항 5개도 포함해, "모를 때 모른다고 하는가"를
  escalation_correctness로 별도 측정. (`eval/`)
- **하이브리드 검색** — 기존 BM25 키워드 검색에 dense 임베딩 + RRF 융합 +
  cross-encoder rerank를 추가. 골드셋 기준 mrr 0.935→0.975, correctness 0.35→0.40으로
  개선(recall@5는 이미 1.0으로 천장). (`core/retrieval.py`)
- **피드백 파이프라인(1사이클)** — `retrieval_log`/`feedback` 로그에서 검색이
  약한 주제를 찾아(mine) LLM으로 위키 엔트리 초안을 만들고(curate), 오염 게이트를
  통과한 것만 `shadow` 상태로 반영. 평가 회귀가 없을 때만 `active`로 승격,
  회귀가 있으면 아무 것도 커밋 안 함. `python scripts/run_update_cycle.py` 한 번
  실행으로 이 전체 흐름 동작 확인. (`core/pipeline/`)
- **문서 ingestion 파이프라인** — 대화 로그가 아니라 사람이 작성한 마크다운 문서를
  직접 위키에 반영하는 경로. 헤더 기준 파싱(`parse.py`)→청킹(`chunk.py`)→LLM 큐레이션
  (`curate.curate_doc_chunk`, provenance=`doc_verified`)→기존 게이트/shadow/promote를
  그대로 재사용. `dedupe.py`가 `entry_id` 결정성 + `chunk_hash` 비교로 콘텐츠가 안 바뀐
  청크는 LLM 호출 없이 skip해 재실행 비용을 0으로 만듦(멱등성). `python
  scripts/ingest_doc.py <path>` 로 실행. (`core/pipeline/parse.py`, `chunk.py`,
  `dedupe.py`, `scripts/ingest_doc.py`)
- **데모 웹앱** — MCP 외에 사람이 직접 써볼 수 있는 진입점. FastAPI 백엔드가
  `search_wiki`로 먼저 검색하고 그 결과만 근거로 답변하는 고정 RAG 채팅
  엔드포인트(`/chat`)와 피드백 엔드포인트(`/feedback`)를 제공하고, 빌드 단계
  없는 정적 채팅 UI 한 장을 서빙. `Dockerfile`로 컨테이너 빌드/실행 가능.
  세션 상태는 서버에 두지 않고 클라이언트가 `conv_id`/`turn_id`를 들고 다님
  (아직 단일 SQLite 파일 기반이라 멀티유저용 아님). (`demo/`, `Dockerfile`)

**구현 예정**

- **Hermes 에이전트 기반 사이클 자동화** — 지금은 `run_update_cycle.py`를 사람이
  수동으로 1회 실행하는 것까지만 구현. 다음 단계는 Hermes 에이전트가 이
  오케스트레이션을 직접 맡는 것 — `hermes cron add --schedule "0 2 * * *" --cmd
  "python scripts/run_update_cycle.py"`로 주기 실행을 등록하고, 사이클 stdout
  요약(mined/shadow/rejected/promote 결과)을 에이전트가 파싱해 이상 신호(예:
  `down_rate` 급증, 연속 `promoted=False`) 감지 시 사람에게 알리는 흐름까지
  확장 예정. RAG 서빙은 이미 [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md)로
  Hermes에 MCP 도구로 연결되어 있어, 자동화도 같은 Hermes 위에 자연스럽게
  추가 가능 — 단, 에이전트에게는 여전히 읽기 도구(`search_wiki`)만 노출하고
  쓰기(`add_entry` 등)는 비노출이라는 HARD CONSTRAINT 유지.
- **공개 환경에서의 안전한 자가 갱신** — 자가 갱신 파이프라인 자체는 동작하지만,
  공개 데모에 그대로 연결하면 누구나 위키를 흔들 수 있음. shadow로 쌓아두고
  사람이 승인해야 active로 가는 승인 워크플로 추가 예정.
- **멀티유저화** — 데모는 지금 단일 프로세스·단일 SQLite 파일로 동작.
  Postgres 전환, rate limiting, 시크릿 분리(vault 등) 계획 중.
- **여러 사이클에 걸친 효과 증명** — 1회 실행 동작만 확인. Hermes로 사이클이
  자동 반복되면, 사이클별 eval 점수 추이와 승격/거부 통계를 누적해 위키가
  실제로 좋아지는지 보여주는 대시보드/기록 추가 예정.
