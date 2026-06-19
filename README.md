# wiki-agent

자가 갱신형(self-updating) 에이전트 RAG. 대화/검색 로그가 위키 지식베이스를
스스로 갱신하고, 그 효과를 eval 하니스로 정량 증명하는 프로젝트.

## 핵심 아이디어

1. **서빙**: MCP 서버가 `search_wiki`/`submit_feedback`만 노출. 검색·대화·피드백은
   전부 SQLite에 로그로 적재된다(이 로그가 갱신 파이프라인의 연료).
2. **평가**: 모든 변경(검색 알고리즘 교체, 위키 갱신 등)은 `eval/run_eval.py`의
   recall@k / mrr / correctness 로 before/after를 비교해야 한다 — 감(感)이 아니라
   숫자로 회귀 여부를 판단.
3. **피드백 파이프라인**: `retrieval_log`/`feedback` 로그를 마이닝(mine) →
   patch 초안 생성(curate) → 오염 게이트(gate) 통과 → `shadow`로만 반영 →
   eval 회귀가 없을 때만 `active` 승격(promote). 회귀가 있으면 아무 것도
   커밋하지 않는다("롤백은 애초에 커밋하지 않음으로 구현").

## 프로젝트 구조

```
core/
  wiki_store.py       지식 저장소(SQLite+FTS5). MCP가 호출하는 단일 진입점.
  retrieval.py         BM25 + dense 임베딩 + RRF 융합 + cross-encoder rerank
  pipeline/
    ingest.py          retrieval_log/feedback 정규화·집계
    mine.py             "gap"(빈도↑ + 검색 신뢰도↓) 탐지
    curate.py           gap → 위키 엔트리 patch 초안(LLM, provenance=curated_from_logs)
    gate.py              오염 게이트: provenance/일일상한/출처다양성/중복/grounding 5단계
    reindex.py           재색인 지점(현재는 영속 임베딩 캐시가 없어 no-op)
    promote.py           shadow 후보를 시뮬레이션 평가 후 회귀 없을 때만 active 승격
eval/
  run_eval.py          recall@k/mrr/correctness 평가 하니스
  gold_set.jsonl        20문항 골드셋(동결)
  baseline.json         기준 점수(명시적 --save-baseline 없이는 보존)
serving/
  mcp_server.py        MCP stdio 서버 (search_wiki, submit_feedback만 노출)
scripts/
  run_update_cycle.py  피드백 파이프라인 1사이클 오케스트레이션
tests/                 pytest (기본 오프라인/무료, 느린 통합 테스트는 RUN_SLOW_TESTS=1로 가드)
docs/                  설계/구현/운영 문서(필요할 때 직접 읽을 것)
```

## 빠른 시작

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY 채우기
```

## 명령

| 목적 | 명령 |
|---|---|
| 서빙 로직 검증 | `python test_client.py` (`ALL CHECKS PASSED ✅` 기대) |
| 단위 테스트(오프라인, 기본) | `pytest` |
| 느린 통합 테스트 포함(실제 모델 로딩) | `RUN_SLOW_TESTS=1 pytest` |
| 검색 품질 평가 | `python eval/run_eval.py [--k 5] [--save-baseline]` |
| MCP 서버(stdio) | `python serving/mcp_server.py` |
| 피드백 파이프라인 1사이클 | `python scripts/run_update_cycle.py [--gold path] [--k 5]` |

## HARD CONSTRAINTS

- mine/curate/gate는 직접 구현한다(LLM 스킬/도구로 떠넘기지 않음).
- 갱신 patch는 항상 `shadow`로만 반영된다.
- eval 회귀가 없을 때만 `active`로 승격하고, 회귀 시 아무 것도 커밋하지 않는다.
- `agent_generated` 출처의 엔트리는 검증된 source 없이는 절대 승격하지 않는다.
- 에이전트는 MCP를 통해 KB에 직접 쓸 수 없다(쓰기 도구 비노출).
- DB 경로는 `WIKI_AGENT_DB`(절대경로) 환경변수로 주입한다(하드코딩 금지).

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
  `eval/baseline.json`과 before/after로 비교한다. 모든 변경은 이 숫자로 검증한다. (`eval/`)
- **하이브리드 검색** — 기존 BM25 키워드 검색에 dense 임베딩 + RRF 융합 +
  cross-encoder rerank를 추가. 골드셋 기준 mrr 0.935→0.975, correctness 0.35→0.40으로
  개선(recall@5는 이미 1.0으로 천장). (`core/retrieval.py`)
- **피드백 파이프라인(1사이클)** — `retrieval_log`/`feedback` 로그에서 검색이
  약한 주제를 찾아(mine) LLM으로 위키 엔트리 초안을 만들고(curate), 오염 게이트를
  통과한 것만 `shadow` 상태로 반영한다. 평가 회귀가 없을 때만 `active`로 승격하고,
  회귀가 있으면 아무 것도 커밋하지 않는다. `python scripts/run_update_cycle.py` 한 번
  실행으로 이 전체 흐름이 동작하는 것을 확인했다. (`core/pipeline/`)

**아직 안 한 것**

- **사이클 자동 트리거** — 지금은 `run_update_cycle.py`를 수동으로 1회 실행하는
  것까지만 구현했다. Hermes cron 등으로 주기적으로 자동 실행하는 오케스트레이션은
  없다.
- **여러 사이클에 걸친 효과 증명** — 1회 실행이 동작하는 것만 확인했고, 위키가
  실제로 시간이 지나며 좋아지는지(사이클별 eval 점수 추이, 승격/거부 통계 누적)를
  보여주는 대시보드나 기록은 아직 없다.
