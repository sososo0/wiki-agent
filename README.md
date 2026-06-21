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
├── gold_set.jsonl       58문항 골드셋(53개 답변 가능 + 5개 의도적 unanswerable)
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
1. 데모 채팅(A 또는 B)에서, **현재 위키 8개 주제(재시도, rate limiting, connection
   pooling, circuit breaker, idempotency, timeout, backpressure, bulkhead isolation —
   각 주제마다 basics/intermediate/advanced 난이도별로 갈라져 active 엔트리만 400개
   이상)와 무관한** 질문 하나를 골라 **정확히 같은 문구로 3번 이상** 전송(복사-붙여넣기로,
   패러프레이즈 금지). 예:
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
같은 후보를 같은 사이클로 다시 돌려도 `correctness`가 ±1문항(골드셋 58문항 기준
약 0.02) 정도 흔들릴 수 있음. `recall@k`/`mrr`는 결정적이므로 그쪽이 변하면 진짜
회귀, `correctness`만 바뀌었다면 노이즈일 가능성이 높음 — 회귀 차단 자체는
설계대로 보수적으로 동작하는 것이라 승격 실패 ≠ 사이클 실패.

**주의 — 이 절차를 반복 실행하면 골드셋의 unanswerable 문항이 오염될 수 있음.**
`eval/run_eval.py`(또는 `--qualitative` 등)를 자주 돌리면 그 평가 질문들도
`search_wiki` 호출을 거치면서 `retrieval_log`에 그대로 쌓인다. 골드셋의 5개
unanswerable 문항(KB에 정답이 없는 질문)이 **정확히 같은 문구로 3번 이상**
쌓이면, `mine_gaps`가 이를 진짜 gap으로 오인해 `curate`가 LLM 자기 지식만으로
그럴듯한(하지만 검증되지 않은) 답을 만들어낼 수 있다 — 실제로 이 프로젝트에서
한 번 발생했고, `promote_if_better`가 `escalation_correctness`(1.0→0.0) 하락을
정확히 잡아내 active 승격을 막아냈다(안전장치가 설계대로 동작한 사례). 이후
`run_update_cycle.py`에 마이닝 윈도우(`--window-days`, 기본 14일)와 거부된 gap
기억(`skipped_rejected_gaps`)을 추가해 — 한 번 게이트가 거부한 질문은 같은 사이클
내내 다시 LLM을 호출하지 않고, 윈도우 밖(14일 이상 지난) 오래된 오염도 자연히
빠진다(아래 "피드백 파이프라인 1사이클" 참고). 다만 윈도우 안에서 처음 오염되는
경우 자체는 막지 못하므로, 평가를 자주 돌렸다면 갱신 사이클 실행 전에
`retrieval_log`를 점검하는 습관은 여전히 권장.

**한계 — 외부 검색을 하지 않는다.** `mine`이 찾은 gap을 `curate.py`가 위키 엔트리
초안으로 만들 때, LLM은 그 gap의 질문 원문만 보고 **자기 학습 지식으로** 초안을
쓴다 — 실제로 웹을 검색해서 근거 자료를 가져오지 않는다(`default_llm_fn`이 받는
입력은 `query_examples`뿐, 검색 도구가 없음). 데모 채팅(`/chat`)도 마찬가지로
KB에 없는 질문에는 "모른다"고 답할 뿐 그 자리에서 검색하지 않는다. 이는 의도된
제약이다: (1) 검색 결과를 그대로 신뢰하면 `agent_generated`/`curated_from_logs`
출처에 적용되는 "검증된 source 없이는 active 승격 금지" HARD CONSTRAINT를 약화시킬
위험이 있고, (2) 검색 호출 자체가 추가 비용이라 [API 호출 횟수 제한](#api-호출-횟수-제한)
취지와 충돌한다. 실제로 추가하려면 `curate.py`에 검색 도구를 주입하고 가져온 URL을
provenance로 남기되, 그래도 shadow → eval 회귀 검증 → promote 경로는 그대로
거치게 하는 별도 설계가 필요하다.

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
| 피드백 파이프라인 1사이클 | `python scripts/run_update_cycle.py [--gold path] [--k 5] [--window-days 14]` |
| 문서 ingestion 파이프라인 | `python scripts/ingest_doc.py <path...> [--daily-cap N] [--min-sources 1]` |
| 데모 웹앱(채팅) | `WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload` |
| 위키 그래프 시각화 | 데모 실행 후 브라우저에서 `/static/graph.html` 접속 (`GET /graph`로 원본 JSON 확인 가능) |
| 데모 Docker 빌드/실행 | `docker build -t wiki-agent-demo .` 후 `docker run ...` |
| 갱신 사이클 알림 확인 | 데모 실행 후 헤더의 🔔 아이콘 클릭 (`GET /notifications`로 원본 JSON 확인 가능) |
| 사이클 자동 실행 상태 확인 | `hermes cron status` / `hermes cron list` (설정은 [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md) Step 6) |

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
WIKI_AGENT_DB=/tmp/demo.db python scripts/run_update_cycle.py [--window-days 14]
```

`retrieval_log`/`feedback`을 마이닝→큐레이션→게이트→shadow 반영→평가 기반 승격까지
1회 실행. 예시 출력(실제 실행 결과):

```
mined gaps: 2
skipped (already-rejected gaps): []
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

`retrieval_log`/`feedback`에는 retention 정책이 없어 테이블이 무한히 쌓이는데, 두
가지 장치로 그 비용을 억제한다:

- **마이닝 윈도우(`--window-days`, 기본 14일)** — `mine_gaps`가 보는 로그를 최근
  N일로 제한한다. 안 그러면 매 사이클이 점점 커지는 전체 히스토리를 다시 스캔하고,
  오래전에 우연히 오염된 쿼리(예: 평가를 반복 실행해 골드셋의 unanswerable 문항이
  retrieval_log에 3번 이상 쌓인 경우 — 위 "주의" 콜아웃 참고)가 영원히 gap으로
  재탐지된다. `--window-days 0`을 주면 전체 히스토리를 보는 이전 동작으로 되돌릴 수
  있다.
- **거부된 gap 기억(`skipped_rejected_gaps`)** — 게이트가 한 번 거부한 gap은
  `status="rejected"` 마커(`wiki_gap_..._rej`, `core/pipeline/curate.rejected_gap_entry_id`)로
  기억해, 같은 질문이 윈도우 안에서 다시 마이닝돼도 curate/judge LLM 호출을 반복하지
  않는다(문서 ingestion의 `dedupe.py` skip_rejected와 동일한 목적 — 문서는
  `chunk_hash`로, 여긴 `norm_query`로 "콘텐츠 불변"을 판단). 따로 만료시키지 않아도
  되는 이유: 나중에 진짜 답이 될 콘텐츠가 생기면(문서 ingestion 등) 검색 점수가
  양수로 돌아서 `mine_gaps` 자체가 더는 그 질문을 gap으로 뽑지 않는다.

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

### API 호출 횟수 제한

데모는 불특정 다수가 들어와 찍어볼 수 있는 공개 엔드포인트인데, 채팅 1턴마다
Anthropic API(`claude-haiku-4-5`)를 호출하므로 **비용 부담** 때문에 호출 횟수
자체를 세 단계로 캡핑한다(`demo/app.py`의 `_consume_call_budget`):

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `WIKI_AGENT_DEMO_DAILY_CALL_LIMIT` | `50` | 프로세스 전체의 일일 LLM 호출 한도(날짜가 바뀌면 자동 리셋) |
| `WIKI_AGENT_DEMO_PER_CONV_CALL_LIMIT` | `10` | 대화(conv_id) 1건이 쓸 수 있는 최대 호출 수 |
| `WIKI_AGENT_DEMO_PER_IP_DAILY_LIMIT` | `20` | IP 1개가 하루에 쓸 수 있는 최대 LLM 호출 수 |

`conv_id`는 클라이언트가 `crypto.randomUUID()`로 만드는 값이라, 대화당 한도만
있으면 공격자가 매 요청마다 새 `conv_id`를 보내는 것만으로 한도를 트리비얼하게
우회할 수 있다 — IP 한도가 그 우회를 막는 실질적인 방어선이다(자세한 보안 설계는
아래 "악의적/봇 트래픽 방어" 참고).

한도에 도달하면 **Anthropic API를 호출하지 않고** "오늘 API 호출 한도에 도달해
답변을 생성할 수 없습니다. 잠시 후 다시 시도해 주세요."를 그대로 응답한다.
`search_wiki`는 로컬 임베딩(비용 없음)이라 한도와 무관하게 항상 수행되며
`retrieval_log`도 계속 쌓인다 — 막히는 건 LLM 호출(`generate`/`generate_title`)뿐이라
gap 마이닝 신호는 끊기지 않는다. 카운터는 메모리에만 있어 서버를 재시작하면
초기화된다(데모 규모에서는 DB까지 갈 필요가 없다고 판단).

### 악의적/봇 트래픽 방어

위 LLM 호출 한도 외에, 배포된 데모를 노린 남용을 막기 위한 별도 방어선
(`demo/app.py`):

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `WIKI_AGENT_DEMO_BURST_LIMIT` | `8` | IP 1개가 `BURST_WINDOW_SECONDS` 동안 보낼 수 있는 최대 요청 수 |
| `WIKI_AGENT_DEMO_BURST_WINDOW_SECONDS` | `10` | 위 버스트 한도의 슬라이딩 윈도우 길이(초) |
| `WIKI_AGENT_DEMO_MAX_BODY_BYTES` | `20480`(20KB) | 이보다 큰 요청 본문은 파싱하지 않고 즉시 413 |
| `WIKI_AGENT_DEMO_TRUST_PROXY` | `0` | `1`이면 `X-Forwarded-For`의 첫 IP를 신뢰. **리버스 프록시 뒤에 배포할 때만 켤 것** — 프록시가 없는데 켜면 누구나 그 헤더를 위조해 IP 기준 한도를 전부 우회할 수 있다 |

버스트 한도는 `@app.middleware("http")`로 **모든 엔드포인트**(`/chat`뿐 아니라
`/graph`/`/history`/`/conversations`까지)에 적용된다 — 검색·그래프 연산도 로컬
임베딩/rerank 연산이라 서버 CPU 비용이 있어서, LLM 호출과 무관하게 짧은 시간에
폭주하는 요청 자체를 막아야 한다. 차단된 요청은 `logging` 모듈로 stdout에
남는다(남용 패턴 사후 확인용, 새 의존성 없음).

`ChatRequest`/`FeedbackRequest`는 `pydantic.Field`로 길이/형식 제약을 둔다
(`message` 1~2000자, `conv_id`는 UUID 형식, `turn_id` 0~100000) — 위반 시 FastAPI가
자동으로 422를 반환한다. SQL 인젝션은 `core/wiki_store.py` 전체가 파라미터
바인딩(`?`)을 쓰므로 원래부터 안전하다.

**한계(이번 범위 밖)**: `/conversations`/`/history/{conv_id}`는 인증이 없어 누구나
모든 사용자의 대화 미리보기를 볼 수 있다 — 단일 SQLite 파일 기반의 멀티유저
미지원 한계(위 "로컬에서 데모 배포하기" 참고)의 연장선이라 이번 보안 작업
범위에는 넣지 않았다. 고치려면 `conv_id` 생성 시 서버가 무작위 `owner_token`을
같이 발급해 `conversation_meta`에 저장하고, 조회 시 그 토큰을 요구하는 방식이
필요하다(데이터 모델 변경이 있는 별도 작업).

### 되묻기(clarify) + 피드백 이유

질문이 검색된 위키 항목들의 여러 해석에 걸쳐 모델이 추측해야 하는 모호한 경우(예:
"타임아웃을 어떻게 설정해?" → connect/read/total 중 어느 것인지), `generate()`는
바로 답을 추측하는 대신 선택지가 있는 명확화 질문을 반환한다 — opencode의
Question System([Permission and Question System | sst/opencode | DeepWiki](https://deepwiki.com/sst/opencode/2.5-permission-and-question-system))처럼
실행을 멈추고 사용자의 선택(또는 직접 입력)을 받아 재개하는 패턴을 참고했다.
**추가 LLM 호출 없이** 같은 1회 호출 안에서 `{"type": "answer", ...}` 또는
`{"type": "clarify", "question", "options"}` 중 하나로 응답하므로 위 호출 한도와
충돌하지 않는다.

**서버가 명확화 대기 상태를 들고 있는다(`demo/app.py`의 `_pending_clarifications`).**
opencode의 Question System(실행 스레드를 `Deferred`로 블로킹하고 `reply()`가 오면
같은 실행을 이어가는 구조)과 동일한 개념을, 우리의 stateless HTTP 구조에 맞게
구현했다: `clarify` 응답이 나갈 때 서버가 `{"query": 원본 질문, "created_at": ...}`를
`conv_id` 키로 메모리에 저장한다. 사용자가 옵션을 고르면 클라이언트는 **선택한
텍스트만**(문자열 조합 없이) `force_answer: true`로 다시 `/chat`에 보내고, **서버가**
저장해둔 원본 질문과 합쳐 검색·`generate(force_answer=True)`를 1회 더 호출한다
(모호한 질문 1건당 최대 2회 호출로 상한 유지). pending 상태는 10분
(`PENDING_CLARIFY_TTL_SECONDS`) 안에 답하지 않으면 만료되어 다음 메시지를 새
질문으로 처리한다 — 진짜 실행 일시정지/재개는 아니지만(여전히 평범한 두 번의
독립된 HTTP 요청이다), 컨텍스트의 출처가 클라이언트가 아니라 서버라는 점이
이전 버전과의 핵심 차이다.

👎 피드백은 클릭 즉시 전송하는 대신 짧은 이유 후보("근거 부족"/"주제와 무관"/
"너무 추상적"/"사실과 다름"/"이유 없이 제출")를 보여주고 고른 이유와 함께
`/feedback`을 호출한다(정적 후보라 LLM 호출 없음). `feedback` 테이블에 `reason`
컬럼으로 저장되며, 현재는 신호를 쌓아두는 것까지만 하고 `mine.py`/daily_cap
조정 등 파이프라인 활용은 별도 작업으로 남겨뒀다.

> **참고**: 답변 아래 "근거" 줄의 entry_id는 클릭 가능한 링크다. 위키 항목 단건을 보여주는
> 전용 상세 페이지는 아직 없지만, `/static/graph.html?focus=<entry_id>`로 이동하면 해당
> entry_id의 노드를 그래프에서 자동으로 선택·포커스하고 우측 패널에 상세 정보(topic/
> canonical/body_md/provenance 등)를 띄워준다 — 사실상 그래프 뷰가 위키 항목의 "상세
> 페이지" 역할을 한다.

### 갱신 사이클 알림(🔔)

`scripts/run_update_cycle.py`는 이제 Hermes cron으로 매일 새벽 2시 자동 실행되는데
([docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md) Step 6), 로그 파일에만
결과가 남으면 아무도 안 보면 그냥 지나간다. 그래서 사이클이 끝날 때마다
`run_cycle()`이 이미 들고 있는 구조화된 결과 dict(mined/feedback/shadow_written/
promote)에서 **직접**(로그 텍스트를 나중에 파싱하지 않고) 알림을 0~2건 만들어
`notifications` 테이블에 적재한다(`summary_notifications()`):

| 레벨 | 발생 조건 | 예시 |
|---|---|---|
| `info` | 매 사이클 항상 1건 | "gap 2개 발견, shadow 1개 반영, 승격됨(1개 active)" |
| `warning` | 최근 피드백 5건 이상 & 👎 비율 > 50% | "피드백 부정 비율이 높음 — daily_cap이 보수적으로 낮춰짐" |
| `warning` | shadow가 새로 생겼는데 승격은 안 됨(회귀 감지) | "회귀로 승격 차단됨 — shadow 1개가 active로 못 감" |
| `error` | 사이클 자체가 예외로 죽음 | 예외 메시지 그대로(그대로 재raise도 함 — hermes cron 실패 상태 보존) |

이 테이블에 쓰는 건 오직 `run_update_cycle.py`(신뢰된 오프라인 스크립트)뿐이고
KB(`wiki_entry`)와는 무관해 "에이전트는 KB에 직접 못 씀" HARD CONSTRAINT와도
충돌하지 않는다. 데모는 읽기/읽음처리만 노출:

- `GET /notifications` → `{"notifications": [...], "unread_count": N}`
- `POST /notifications/{id}/read`

헤더의 🔔 아이콘이 안 읽은 개수를 빨간 배지로 보여주고, 클릭하면 최신순 드롭다운이
열린다(레벨별 색 점 + 제목 + 메시지 + 시간). 60초 간격 `setInterval` 폴링으로
배지를 갱신(SSE/웹소켓 없이 데모 규모에 맞는 단순한 방식). 직접 알림을 만들어
확인하려면:

```bash
WIKI_AGENT_DB=/tmp/demo.db python3 -c "
from core import wiki_store
wiki_store.add_notification('warning', '테스트 알림', '확인용 메시지')
"
```

**한계**: 사이클 1회 단위 요약만 보여줄 뿐, 여러 사이클에 걸친 누적 추이(예: eval
점수가 시간이 지나며 개선되는지)는 보여주지 않는다 — 위 "현재 상태 → 구현 예정"의
"여러 사이클에 걸친 효과 증명" 참고.

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

좌측에는 모든 위키 항목을 topic 알파벳순으로 정렬한 검색 가능한 목록이 있다. 항목을
클릭하면 그래프에서 해당 노드가 선택·포커스되고 우측 상세 패널이 갱신된다 — URL에
`?focus=<entry_id>`를 붙여 직접 진입해도 동일하게 동작한다(위 "출처 링크화" 참고).

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
  검색·대화·피드백 로깅까지 동작. MCP 서버는 `demo/app.py`와 동일하게 entry_id+version
  키 임베딩 캐시(`_search_embed_cache`)를 모듈 전역으로 들고 다녀, 에이전트가
  검색할 때마다 활성 엔트리 전체(400여 개)를 재인코딩하지 않는다. (`core/wiki_store.py`,
  `serving/mcp_server.py`)
- **평가 하니스** — 골드셋 58문항(53개 답변 가능 + 5개 의도적 unanswerable) 기준
  recall@k/mrr/correctness를 계산하고, 기존 `eval/baseline.json`과 before/after로
  비교. 모든 변경은 이 숫자로 검증. unanswerable 문항으로 "모를 때 모른다고 하는가"를
  escalation_correctness로 별도 측정 — 최근 측정값(`eval/baseline.json`, k=5):
  recall@k 0.98 / mrr 0.78 / correctness 0.89 / escalation_correctness 1.0. (`eval/`)
- **하이브리드 검색** — 기존 BM25 키워드 검색에 dense 임베딩 + RRF 융합 +
  cross-encoder rerank를 추가. 도입 당시 20문항 골드셋 기준 mrr 0.935→0.975,
  correctness 0.35→0.40으로 개선(당시 recall@5는 이미 1.0으로 천장 — 이후 코퍼스가
  seed 5개 → active 400여 개로 늘면서 현재 recall@k는 위 0.98 수준). (`core/retrieval.py`)
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
  (아직 단일 SQLite 파일 기반이라 멀티유저용 아님). 질문이 모호하면 추가 LLM
  호출 없이 같은 호출 안에서 선택지가 있는 명확화 질문으로 되묻고(opencode의
  Question System 참고), 👎 피드백은 짧은 이유 후보와 함께 저장한다. 비용
  부담 때문에 일일/대화당 LLM 호출 횟수에 하드 캡을 둠(`demo/` "API 호출
  횟수 제한" 참고). (`demo/`, `Dockerfile`)
- **갱신 사이클 자동 실행(Hermes cron)** — `scripts/run_update_cycle.py`를 사람이
  수동으로 실행하는 대신, Hermes cron(`hermes cron create "0 2 * * *" --no-agent`)으로
  매일 새벽 2시 자동 실행. `--no-agent` 스크립트 job에는 (config.yaml에 노출되지
  않는) 하드코딩된 120초 타임아웃이 있어, 골드셋이 커진 지금은 평가 단계만으로도
  넘기기 쉽다 — 실제 작업은 백그라운드로 던지고 래퍼는 즉시 종료하는 방식으로
  우회했고, `hermes cron run`으로 직접 트리거해 끝까지 도는 것까지 확인. 이 검증
  과정에서 반복 평가 실행이 골드셋의 unanswerable 문항을 `retrieval_log`에 오염시켜
  `mine_gaps`가 가짜 gap으로 오인하고 `curate`가 그럴듯한 오답을 만든 사례가 실제로
  발생했는데, `promote_if_better`가 골드셋 회귀(escalation_correctness 1.0→0.0)로
  정확히 막아내는 것까지 실증됨 — 자동화돼도 게이트/회귀 차단은 코드 변경 없이 그대로
  적용된다(에이전트는 여전히 KB에 직접 못 쓰고, 별도의 신뢰된 프로세스가 스케줄러로
  도는 구조 그대로). 설정 절차는 [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md)
  Step 6 참고. (저장소 밖 호스트 설정: `~/.hermes/scripts/wiki_agent_update_cycle.sh`)
- **갱신 사이클 알림(🔔)** — 자동 실행되는 사이클을 아무도 안 보면 그냥 지나가는
  문제를 보완. `run_cycle()`이 이미 들고 있는 구조화된 summary(mined/feedback/
  shadow_written/promote)에서 **로그 텍스트를 나중에 파싱하지 않고 그 자리에서
  직접** 알림 0~2건을 판정해 `notifications` 테이블에 기록 — 사이클 요약 1건(info)은
  항상, 피드백 부정 비율 급증이나 회귀로 인한 승격 차단 시 경고(warning)가 추가되고,
  사이클 자체가 예외로 죽으면 에러(error)로 남되 그대로 재raise한다(hermes cron 실패
  상태도 보존). 데모는 `GET /notifications`/`POST /notifications/{id}/read`로
  읽기/읽음처리만 노출하고(쓰기는 오직 `run_update_cycle.py`), 헤더의 🔔 아이콘이
  안 읽은 개수를 배지로 보여주며 60초 폴링으로 갱신한다. (`core/wiki_store.py`,
  `scripts/run_update_cycle.py`, `demo/`)

**구현 예정**

- **공개 환경에서의 안전한 자가 갱신** — 자가 갱신 파이프라인 자체는 동작하지만,
  공개 데모에 그대로 연결하면 누구나 위키를 흔들 수 있음. shadow로 쌓아두고
  사람이 승인해야 active로 가는 승인 워크플로 추가 예정.
- **멀티유저화** — 데모는 지금 단일 프로세스·단일 SQLite 파일로 동작.
  IP/대화 기준 rate limiting은 이미 적용됐지만(위 "악의적/봇 트래픽 방어" 참고),
  Postgres 전환·`/conversations`·`/history` 인증(`owner_token`)·시크릿 분리(vault 등)는
  계획 중.
- **여러 사이클에 걸친 효과 증명** — Hermes cron으로 사이클은 이제 매일 자동
  반복되지만, 사이클별 eval 점수 추이와 승격/거부 통계를 누적해 위키가 실제로
  좋아지는지 보여주는 대시보드/기록은 아직 없음(현재 알림은 사이클 1회 단위 요약만
  보여줄 뿐 누적 추이는 아님) — 추가 예정.
