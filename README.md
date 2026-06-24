# wiki-agent

자가 갱신형(self-updating) 에이전트 RAG. 대화/검색 로그가 위키 지식베이스를
스스로 갱신하고, 그 효과를 eval 하니스로 정량 증명하는 프로젝트.

## 핵심 아이디어

1. **서빙**: MCP 서버가 `search_wiki`/`submit_feedback`만 노출. 검색·대화·피드백은
   전부 SQLite에 로그로 적재(이 로그가 갱신 파이프라인의 연료).
2. **평가**: 모든 변경은 `eval/run_eval.py`의 recall@k / mrr / correctness 로
   before/after 비교 필수 — 감(感)이 아니라 숫자로 회귀 여부 판단.
3. **피드백 파이프라인**: 로그 마이닝(mine) → patch 초안 생성(curate) → 오염
   게이트(gate) 통과 → `shadow`로만 반영 → eval 회귀가 없을 때만 `active` 승격
   (promote). 회귀가 있으면 아무 것도 커밋 안 함("롤백은 애초에 커밋하지 않음으로 구현").

## 프로젝트 구조

```
core/
├── wiki_store.py        지식 저장소(SQLite+FTS5). MCP가 호출하는 단일 진입점.
├── graph.py             위키 그래프 파생 뷰(읽기 전용, 클러스터링 포함)
├── retrieval.py         BM25 + dense 임베딩 + RRF 융합 + cross-encoder rerank
└── pipeline/            mine → curate → gate → promote (+ 문서 ingestion용 parse/chunk/dedupe)
eval/
├── run_eval.py          recall@k/mrr/correctness 평가 하니스
├── gold_set.jsonl       58문항 골드셋(53개 답변 가능 + 5개 의도적 unanswerable)
└── baseline.json        기준 점수(명시적 --save-baseline 없이는 보존)
serving/mcp_server.py     MCP stdio 서버 (search_wiki, submit_feedback만 노출)
demo/                     MCP 외 진입점 — 사람이 직접 써보는 FastAPI 채팅 데모 + 그래프/추이 시각화
scripts/
├── run_update_cycle.py   피드백 파이프라인 1사이클 오케스트레이션
├── ingest_doc.py         문서 ingestion 파이프라인 오케스트레이션
└── translate_wiki_labels.py  그래프 화면용 한글 번역 캐시 생성(원본 영어는 안 건드림)
tests/                    pytest (기본 오프라인/무료, 느린 통합 테스트는 RUN_SLOW_TESTS=1)
docs/                     설계/구현/운영 상세 문서(필요할 때 직접 읽을 것)
```

## 빠른 시작

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt   # 로컬 개발: 테스트까지 포함
cp .env.example .env   # ANTHROPIC_API_KEY 채우기
```

> 배포(Docker)에는 `requirements.txt`만 설치한다 — `requirements-dev.txt`(pytest 등)는
> 이미지 용량과 무관한 로컬 개발/테스트 전용 도구라 런타임 이미지에 넣지 않는다.

> Python이 여러 개 설치돼 있으면 새 터미널 탭마다 `source venv/bin/activate`를
> 다시 실행할 것 — 안 하면 `uvicorn: command not found`/`No module named ...` 오류.

## 데모 실행

데모는 단일 SQLite 파일(`WIKI_AGENT_DB`)을 데이터 저장소로 쓴다. **채팅을 보낸 DB와
갱신 사이클을 돌리는 DB가 반드시 같은 파일이어야** 위키 갱신을 확인할 수 있다.

```bash
WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload   # http://localhost:8000
```

또는 Docker(`-v`로 볼륨을 마운트해야 컨테이너 종료 후에도 DB가 보존됨):

```bash
docker build -t wiki-agent-demo .
docker run -d --name wiki-agent-demo -p 8000:8000 \
  -e WIKI_AGENT_DB=/data/wiki_agent.db --env-file .env \
  -v /tmp/wiki-agent-docker-data:/data wiki-agent-demo
```

## 위키 자가 갱신 직접 확인하기

"대화 로그가 위키를 스스로 갱신한다"는 이 프로젝트의 핵심 서사를 5분 안에 확인하는 절차.
`mine_gaps()`는 **정확히 같은 문구**의 질문이 **3번 이상** 반복되고 검색 신뢰도가
평균 음수일 때만 "위키에 없는 주제"로 탐지한다.

1. 데모 채팅에서 현재 위키 주제(재시도/rate limiting/circuit breaker 등, active 400여개)와
   **무관한** 질문을 골라 **정확히 같은 문구로 3번** 전송(복사-붙여넣기, 패러프레이즈 금지).
2. 채팅과 **같은** `$DB`로 갱신 사이클 실행:
   ```bash
   WIKI_AGENT_DB=$DB python scripts/run_update_cycle.py
   ```
3. stdout에서 `mined gaps`(1 이상), `shadow written`, `promote`(`promoted`/`activated`) 확인.
4. `sqlite3 $DB "select entry_id, status from wiki_entry order by updated_at desc limit 5;"`로
   직접 확인 — `shadow`면 게이트는 통과했지만 승격 전, `active`면 회귀 없이 승격 완료.
5. 같은 질문을 다시 물어보면 새로 승격된 엔트리를 근거로 답하는지 확인.

`--use-web-search`를 주면 gap 큐레이션이 Anthropic `web_search` 도구로 실제 웹 근거를
찾아 `curated_from_web` + 검증된 source로 기록한다(기본 off, 검색 호출 비용 때문에
opt-in). 반복 실행 시 골드셋 오염 등 주의할 점은 [docs/demo-operations.md](docs/demo-operations.md) 참고.

## 외부 문서로 위키 채우기

사람이 쓴 마크다운 문서를 직접 위키에 반영하는 경로(대화 로그 마이닝과 별도). 콘텐츠가
안 바뀐 섹션은 LLM 호출 없이 skip(멱등성) — 같은 문서를 몇 번 재실행해도 변경분만 비용 발생.

```bash
WIKI_AGENT_DB=/tmp/demo.db python scripts/ingest_doc.py docs/ --daily-cap 5
```

## 명령

| 목적 | 명령 |
|---|---|
| 서빙 로직 검증 | `python test_client.py` |
| 단위 테스트 | `pytest` (`RUN_SLOW_TESTS=1 pytest`로 실제 모델 로딩 포함) |
| 검색 품질 평가 | `python eval/run_eval.py [--k 5] [--save-baseline] [--qualitative]` |
| 에이전틱 태스크 평가(진단용) | `python eval/agentic_eval.py [--max-turns 4]` |
| MCP 서버(stdio) | `python serving/mcp_server.py` |
| 피드백 파이프라인 1사이클 | `python scripts/run_update_cycle.py [--window-days 14] [--use-web-search]` |
| 문서 ingestion 파이프라인 | `python scripts/ingest_doc.py <path...> [--daily-cap N]` |
| 그래프 한글 번역 캐시 생성 | `python scripts/translate_wiki_labels.py` |
| 로그 retention 정리(수동/cron) | `python scripts/purge_old_logs.py [--retention-days 30]` |
| 데모 웹앱(채팅) | `WIKI_AGENT_DB=/tmp/demo.db uvicorn demo.app:app --reload` |
| 위키 그래프 / 갱신 추이 시각화 | 데모 실행 후 `/static/graph.html` / `/static/history.html` |
| 사이클 자동 실행 상태 확인 | `hermes cron status` (설정은 [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md)) |

각 명령의 자세한 동작·예시 출력·설계 이유·한계는
[docs/demo-operations.md](docs/demo-operations.md)에 정리했다. 평가가 정확히 무엇을
어떻게 측정하는지(코퍼스가 80배 커져도 회귀를 못 잡은 실제 사례 포함)도 거기 있다.

## HARD CONSTRAINTS

- 갱신 patch는 항상 `shadow`로만 반영.
- eval 회귀가 없을 때만 `active`로 승격, 회귀 시 아무 것도 커밋 안 함.
- `agent_generated`/`curated_from_web` 출처의 엔트리는 검증된 source 없이는 절대 승격 안 함.
- 에이전트는 MCP를 통해 KB에 직접 쓰기 불가(쓰기 도구 비노출).
- DB 경로는 `WIKI_AGENT_DB`(절대경로) 환경변수로 주입(하드코딩 금지).

자세한 배경은 [CLAUDE.md](CLAUDE.md)를 참고.

## 문서

- 설계: [docs/self-updating-rag-design.md](docs/self-updating-rag-design.md)
- 구현 순서: [docs/wiki-agent-implementation-guide.md](docs/wiki-agent-implementation-guide.md)
- MCP-Hermes 서빙 연결: [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md)
- 데모 운영 디테일(요청 제한/보안/알림/그래프/평가 사례): [docs/demo-operations.md](docs/demo-operations.md)

## 현재 상태

**완료**

- **데이터/서빙 레이어** — SQLite+FTS5 지식 저장소, MCP 서버, 검색·대화·피드백 로깅.
- **평가 하니스** — 골드셋 58문항 기준 recall@k 0.98 / mrr 0.78 / correctness 0.89 /
  escalation_correctness 1.0(k=5), before/after baseline 비교.
- **하이브리드 검색** — BM25 + dense 임베딩 + RRF 융합 + cross-encoder rerank.
- **피드백 파이프라인** — mine → curate(로그 기반 또는 opt-in 웹 검색) → gate →
  shadow → 평가 기반 promote. 1사이클 = `scripts/run_update_cycle.py`.
- **문서 ingestion 파이프라인** — 마크다운 문서를 같은 게이트/shadow/promote로 반영,
  content-hash 기반 멱등성.
- **데모 웹앱** — FastAPI 채팅(`/chat`) + 피드백(`/feedback`), 명확화 질문 되묻기,
  비용 캡(일일/대화/IP), 그래프(`/static/graph.html`)·갱신 추이(`/static/history.html`)
  시각화, 갱신 알림(🔔). `Dockerfile`로 컨테이너 빌드.
- **갱신 사이클 자동 실행** — Hermes cron으로 매일 새벽 2시 자동 트리거(설정은
  [docs/RUNBOOK-mcp-hermes.md](docs/RUNBOOK-mcp-hermes.md)).
- **인증/보안** — 대화 조회를 `owner_token`으로 소유자 범위 제한, 알림 읽음 처리
  관리자 토큰 게이트, stored XSS 수정.
- **데이터 파이프라인 스케일링** — 인덱스, LRU 임베딩 캐시(`core/lru_cache.py`),
  로그 retention(`scripts/purge_old_logs.py`), WAL+busy_timeout, 그래프 클러스터링을
  O(n²)에서 거의 선형으로(`core/graph.py`). 자세한 내용은
  [docs/demo-operations.md](docs/demo-operations.md).

**구현 예정**

- **공개 환경에서의 승인 워크플로** — 지금은 평가 회귀만으로 자동 승격. shadow를
  사람이 승인해야 active로 가는 단계 추가 예정.
- **멀티유저화** — 단일 SQLite 파일·단일 프로세스(WAL로 읽기-쓰기 경합은 줄였지만
  쓰기끼리는 여전히 1개만). Postgres 전환은 계획 중.
- **데이터 파이프라인 스케일링 (남은 항목)** — curate 호출이 daily_cap에 안 묶임 등
  — 자세한 목록은 [docs/demo-operations.md](docs/demo-operations.md).
