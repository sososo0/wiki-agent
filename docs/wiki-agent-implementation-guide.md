# wiki-agent 구현 가이드 — 실제 구조

> Hermes는 서빙 에이전트 + cron 트리거 역할만 한다. 마이닝·큐레이션·오염
> 게이트·평가 같은 핵심 DE 로직은 Hermes와 무관하게 순수 Python으로 직접
> 구현돼 있고 pytest로 독립 검증된다.

이 문서는 원래 빌드 순서대로 쓴 설계 메모였다. 실제 구현은 처음 구상보다
단순하다(Postgres 대신 SQLite 한 파일, `core/db.py`+`core/schemas.py` 분리나
별도 `hermes/`/`infra/` 디렉터리 없이 `core/wiki_store.py` + 루트 `Dockerfile`
하나로 충분했음). 아래는 실제 구현 기준 구조와 각 단계가 어느 파일에
있는지에 대한 안내다.

## 레포 구조

```
wiki-agent/
├── core/
│   ├── wiki_store.py        # 스키마/CRUD (SQLite+FTS5, 표준 라이브러리만)
│   ├── retrieval.py         # 하이브리드 검색(BM25+dense+RRF) + cross-encoder rerank
│   ├── graph.py             # 위키 그래프 파생 뷰(읽기 전용, 클러스터링)
│   ├── lru_cache.py         # 임베딩 캐시용 LRU
│   └── pipeline/
│       ├── ingest.py        # 로그 → 세션화 → 필터
│       ├── mine.py          # gap 탐지(fact/correction은 의도적으로 미구현)
│       ├── curate.py        # LLM → 구조화 patch (로그 기반 또는 opt-in 웹 검색)
│       ├── gate.py          # 오염 방지 게이트
│       ├── reindex.py       # 변경된 엔트리만 재임베딩
│       ├── promote.py       # 골드셋 평가 → shadow→active 또는 롤백
│       ├── parse.py·chunk.py·dedupe.py   # 문서 ingestion용
├── eval/
│   ├── gold_set.jsonl        # 동결된 골드셋
│   ├── run_eval.py           # recall@k, mrr, correctness(LLM-as-judge)
│   ├── agentic_eval.py       # 멀티홉 진단(notify-only, 게이트 미연결)
│   └── baseline.json         # 기준 점수(--save-baseline 없이는 보존)
├── serving/mcp_server.py     # RAG를 MCP 도구로 노출(search_wiki, submit_feedback)
├── demo/                     # FastAPI 채팅 데모 + 그래프/추이 시각화
├── scripts/
│   ├── run_update_cycle.py   # 파이프라인 1사이클 오케스트레이션
│   ├── ingest_doc.py         # 문서 ingestion 오케스트레이션
│   ├── purge_old_logs.py·backup_db.py·translate_wiki_labels.py
├── tests/                    # pytest (RUN_SLOW_TESTS=1로 느린 통합 테스트 포함)
└── test_client.py            # MCP 없이 서빙 로직만 검증
```

> 레포 폴더명은 `wiki-agent`(하이픈)지만 Python import 패키지는 하이픈을 못 쓰므로
> `core`/`serving` 같은 무하이픈 모듈명을 그대로 쓴다.

## 단계별 구현 위치

| 단계 | 파일 | 비고 |
|---|---|---|
| 데이터 스키마 | `core/wiki_store.py` | SQLite+FTS5, WAL, busy_timeout |
| 평가 하니스 | `eval/run_eval.py` | 골드셋 기준 recall@k/mrr/correctness, 항상 먼저 만들 것 |
| 검색 | `core/retrieval.py` | Hermes 없이 단독 pytest 검증 가능 |
| MCP 서빙 | `serving/mcp_server.py` | `search_wiki`/`submit_feedback` 2개만 노출 |
| 로깅 | `core/wiki_store.py`의 `log_turn`/`submit_feedback` | 모든 턴이 다음 사이클의 입력 |
| Mine | `core/pipeline/mine.py` | 탐지 기준은 파일 상단 docstring 참고 |
| Curate | `core/pipeline/curate.py` | LLM 출력은 항상 구조화 patch, DB에 바로 안 씀 |
| Gate | `core/pipeline/gate.py` | 오염 방지 — 기준은 파일 상단 docstring 참고 |
| Reindex/Promote | `core/pipeline/reindex.py`/`promote.py` | 회귀 시 커밋 안 함(=롤백) |
| 오케스트레이션 | `scripts/run_update_cycle.py` | Hermes cron이 이 스크립트만 트리거 |

Hermes 연결(MCP 등록, cron 자동 트리거, 실제 겪은 운영 함정)은
[docs/RUNBOOK-mcp-hermes.md](RUNBOOK-mcp-hermes.md)에 정리했다 — Hermes CLI
문법은 버전마다 바뀌므로 그 문서의 경고를 먼저 확인할 것.

## 흔한 함정

- DE 로직(마이닝·큐레이션·게이트)을 프레임워크에 떠넘기지 않는다 — 직접 구현해야
  코드로 검증 가능하고, Hermes는 트리거 역할로만 둔다.
- 평가 없이 갱신부터 만들지 않는다 — before가 없으면 after를 증명 못 함.
- 에이전트 답변을 검증 없이 KB에 재투입하지 않는다 — 게이트가 막는 지점.
- shadow/active 분리를 생략하지 않는다 — 나쁜 갱신이 곧바로 active에 들어가면
  안 됨.
