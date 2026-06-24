# CLAUDE.md — wiki-agent

> 의도적으로 짧게 유지. 매 세션 로드되므로 "항상 참인 규칙·명령"만 둔다.
> 절차/단계별 플레이북은 여기 두지 말고 docs/ 를 참조(필요할 때만 읽힘).

자가 갱신형 에이전트 RAG. 대화 로그가 위키를 자동 갱신하고 RAG 성능 향상을 지표로 증명하는 프로젝트.

## 문서 (상세는 여기, 작업 시 해당 파일을 직접 읽을 것)
- 설계:   docs/self-updating-rag-design.md
- 구현순서: docs/wiki-agent-implementation-guide.md 
- 서빙연결: docs/RUNBOOK-mcp-hermes.md
- 데모 운영 디테일(요청제한/보안/알림/그래프/평가 사례): docs/demo-operations.md

## 이미 구현됨 — 재작성 금지, 위에서 빌드
core/wiki_store.py, serving/mcp_server.py, test_client.py (서빙 레이어 완료)
core/pipeline/parse.py·chunk.py·dedupe.py, scripts/ingest_doc.py (문서 ingestion 완료)
eval/run_eval.py(qualitative rubric 옵트인 포함), eval/agentic_eval.py(멀티홉 진단, 게이트 미연결)
core/graph.py(위키 그래프 읽기 전용 파생 뷰), demo/static/graph.html(시각화, MCP 미노출)

## HARD CONSTRAINTS (항상 적용)
- 평가 우선: 갱신 기능보다 eval 하니스 먼저.
- DE 로직(mine/curate/gate)은 Hermes 스킬/도구로 구현 위임 가능. 단, 적용 전 사람 코드리뷰 필수.
- 에이전트는 KB에 직접 못 씀: MCP로 쓰기 도구(add_entry 등) 노출 금지.
- 갱신 patch는 shadow로만 반영. eval 회귀 없을 때만 active 승격, 회귀 시 롤백.
- agent_generated/curated_from_web 엔트리는 검증 소스 없이 active 승격 금지.
- DB 경로는 WIKI_AGENT_DB(절대경로)로 주입. 하드코딩 금지.

## 명령
- 로직 검증: python test_client.py        # "ALL CHECKS PASSED" 기대
- 평가:     python eval/run_eval.py [--qualitative]
- 에이전틱 평가(진단용): python eval/agentic_eval.py
- MCP 서버: python serving/mcp_server.py   # stdio
- 문서 ingestion: python scripts/ingest_doc.py <path> [--daily-cap N]
- 그래프 시각화: 데모 실행 후 /static/graph.html (데이터: GET /graph)

## 작업 방식
- 큰 작업은 plan mode로 계획을 먼저 보여주고 승인 후 실행.
- 단계별 상세는 docs/wiki-agent-implementation-guide.md 를 그때 읽어서 따른다.
