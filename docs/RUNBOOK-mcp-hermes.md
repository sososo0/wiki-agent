# wiki-agent — MCP 서버 & Hermes 연결 런북

서빙 레이어를 "실제 돌아가는 코드"까지 만든 부분이다. 핵심 로직(`core/wiki_store.py`)은
표준 라이브러리만 쓰므로 어디서나 돌고, Hermes는 이 MCP 서버를 도구로 호출만 한다.

```
wiki-agent/
├── core/wiki_store.py     # SQLite+FTS5 검색 + 로깅 (의존성 0)
├── serving/mcp_server.py  # FastMCP 서버 (search_wiki, submit_feedback)
├── test_client.py         # mcp 없이 로직 검증
└── requirements.txt       # mcp
```

> ⚠️ Hermes는 버전 변동이 빠르다(MCP 클라이언트는 v0.2.0+, 서버 모드는 v0.6.0+).
> 아래 명령은 현재 docs 기준이며, 시작 전 `hermes --help` / 공식 docs로 한 번 확인할 것.

---

## Step 1 — 로직부터 검증 (의존성 0, Hermes 불필요)

```bash
cd wiki-agent
python core/wiki_store.py     # 초기화+시드+샘플 검색 출력
python test_client.py         # 검색/로깅/피드백 end-to-end → "ALL CHECKS PASSED ✅"
```
여기서 막히면 Hermes 문제가 아니다. 먼저 이걸 통과시킨다.

---

## Step 2 — MCP 서버 단독 기동 확인

```bash
pip install -r requirements.txt          # mcp 설치
python serving/mcp_server.py             # stdio 서버 시작 → 입력 대기(정상). Ctrl+C로 종료
```
에러 없이 멈춰 있으면 정상(클라이언트가 stdin/stdout으로 말을 걸기 전까지 대기).

---

## Step 3 — Hermes에 연결

Hermes에 MCP 확장이 없다면 먼저 설치:
```bash
cd ~/.hermes/hermes-agent && uv pip install -e ".[mcp]"
```

### 방법 A) config.yaml (권장 — 재현 가능)
`~/.hermes/config.yaml`의 `mcp_servers:` 아래에 추가. **절대경로**를 쓰고,
DB 경로를 env로 고정해 에이전트의 검색 로그가 파이프라인이 읽는 DB와 같은 파일에 쌓이게 한다.

```yaml
mcp_servers:
  wiki-agent:
    command: python
    args: ["/ABS/PATH/wiki-agent/serving/mcp_server.py"]
    env:
      WIKI_AGENT_DB: "/ABS/PATH/wiki-agent/wiki_agent.db"

# 노출 도구를 최소화 (Hermes 철학: "필요한 최소 표면만")
mcp_server_filter:
  wiki-agent:
    mode: whitelist
    tools:
      - search_wiki
      - submit_feedback
```

### 방법 B) CLI
```bash
hermes mcp add wiki-agent \
  --command python \
  --args "/ABS/PATH/wiki-agent/serving/mcp_server.py"
```

### 연결 검증
```bash
hermes mcp test wiki-agent    # "✅ ..." 기대
hermes mcp list               # 등록 목록
# 설정을 바꿨으면 대화 중 /reload-mcp
```

---

## Step 4 — 에이전트가 RAG를 쓰게 만들기

`hermes chat` 후, search_wiki를 강제하는 지시를 시스템 프롬프트(또는 프로필)에 넣는다:

> 답변은 반드시 `search_wiki` 결과에 근거하라. 인용한 entry_id를 답변에 표기하고,
> 관련 결과가 없으면 지어내지 말고 모른다고 답하라.

확인 프롬프트 예:
```
지금 사용할 수 있는 MCP 도구가 뭐야?         # search_wiki/submit_feedback 보여야 함
실패한 요청을 안전하게 재시도하려면?           # search_wiki 호출 → wiki_0001 인용 기대
```

---

## Step 5 — 로그가 파이프라인으로 흐르는지 확인 (다음 단계의 연결고리)

에이전트가 검색할 때마다 `retrieval_log`가 쌓인다. 이게 피드백 파이프라인(설계서/구현가이드
Step 6)의 입력이다.

```bash
sqlite3 /ABS/PATH/wiki-agent/wiki_agent.db \
  "SELECT ts, query FROM retrieval_log ORDER BY ts DESC LIMIT 5;"
```
행이 쌓이면 서빙→로깅 경로 완성. 이제 `scripts/run_update_cycle.py`(마이닝→큐레이션→게이트)를
이 로그 위에 붙이면 자가 갱신 루프로 이어진다.

---

## 보안 메모

- stdio 로컬 서버 + 화이트리스트로 표면 최소화. KB 쓰기 도구(add_entry)는 의도적으로 미노출.
- 자율 에이전트는 코드 실행 권한을 갖는다 → 전용 프로필/격리(컨테이너) 권장.
- Hermes 카탈로그 MCP를 설치할 땐 매니페스트(source/bootstrap/transport.command)를 먼저 읽을 것.

---

## 구현 완료

이 런북 작성 당시엔 "다음에 깊게 팔 후보"였으나 이후 모두 구현됐다(상세는 README의
"현재 상태" 참고).

- **dense 검색**: `core/retrieval.py` — BM25 + dense 임베딩 + RRF + cross-encoder rerank.
- **피드백 파이프라인 1사이클**: `core/pipeline/` — gap 탐지 → 큐레이션 → 게이트 → shadow
  반영, `scripts/run_update_cycle.py`로 오케스트레이션.
- **평가 게이트**: `core/pipeline/promote.py` — 골드셋으로 base/candidate 평가 후 회귀
  없을 때만 승격(`promote_if_better`), 회귀 시 커밋 안 함.

이 RUNBOOK이 다루는 MCP/Hermes 서빙 경로 외에, 사람이 직접 써볼 수 있는 FastAPI 데모
웹앱(`demo/app.py`)도 별도 진입점으로 추가됐다. 로컬 배포 방법과 위키 자가 갱신을 직접
확인하는 절차는 README의 "로컬에서 데모 배포하기" / "위키 자가 갱신 확인하기" 참고.

## 다음에 깊게 팔 후보

1. **사이클 자동 트리거**: 지금은 `run_update_cycle.py`를 수동으로 1회 실행하는 것까지만
   구현됨. Hermes cron으로 주기 실행(예: `hermes cron add --schedule "0 2 * * *" --cmd
   "python scripts/run_update_cycle.py"`)하는 오케스트레이션은 아직 없음.
2. **공개 환경에서의 안전한 자가 갱신**: 갱신 파이프라인 자체는 동작하지만, shadow로 쌓아두고
   사람이 승인해야 active로 가는 워크플로는 아직 없음 — 지금은 평가 회귀만으로 자동 승격됨.
