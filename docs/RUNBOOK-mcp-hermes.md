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

## Step 6 — 자동 트리거 (Hermes cron으로 `run_update_cycle.py` 주기 실행)

에이전트가 직접 위키를 갱신하는 게 아니다 — 에이전트는 여전히 `search_wiki`/
`submit_feedback`만 호출하고, **별도의 신뢰된 프로세스**(`scripts/run_update_cycle.py`)를
스케줄러가 주기적으로 실행시키는 구조다. 이렇게 둬야 "에이전트는 KB에 직접 못 씀"
제약이 자동화 이후에도 깨지지 않는다.

> ⚠️ Step 3과 동일하게, cron/schedule 서브커맨드 이름·문법은 Hermes 버전마다 다르다.
> 등록 전 `hermes --help`로 한 번 확인할 것. 아래는 v0.16.0 기준 실제 동작 확인된 명령.

`hermes cron create`는 임의 쉘 명령(`--cmd`)을 직접 받지 않고, **`~/.hermes/scripts/` 아래의
스크립트 파일**만 가리킬 수 있다. `--no-agent`를 주면 에이전트 LLM 루프를 거치지 않고
스크립트 자체가 곧 job이 되어 stdout이 그대로 전달된다 — "에이전트가 직접 갱신하지 않는다"는
위 원칙을 CLI 차원에서도 강제하는 모드.

```bash
# 1) 래퍼 스크립트를 ~/.hermes/scripts/ 아래에 둔다 (절대경로 cd + WIKI_AGENT_DB 고정)
cat > ~/.hermes/scripts/wiki_agent_update_cycle.sh <<'EOF'
#!/bin/bash
set -e
cd "/ABS/PATH/wiki-agent"
export WIKI_AGENT_DB="/ABS/PATH/wiki-agent/wiki_agent.db"
exec "/ABS/PATH/wiki-agent/venv/bin/python" scripts/run_update_cycle.py
EOF
chmod +x ~/.hermes/scripts/wiki_agent_update_cycle.sh

# 2) cron 등록 (--no-agent: 스크립트 자체가 job, 에이전트 루프 미사용)
hermes cron create "0 2 * * *" --name wiki-agent-update-cycle \
  --script wiki_agent_update_cycle.sh --no-agent

# 3) 실제로 실행되려면 게이트웨이(스케줄러 데몬)가 떠 있어야 한다 — 등록만으로는 안 돈다
hermes gateway install      # macOS: launchd 유저 서비스로 설치+기동
hermes cron status          # "Gateway is running — cron jobs will fire automatically" 확인
```

- `hermes gateway install`이 설치하는 launchd 서비스는 `LimitLoadToSessionType: Aqua` —
  즉 **그 macOS 계정으로 로그인해 있는 동안에만** 돈다(로그아웃/재부팅 후 미로그인 상태에는
  실행 안 됨). 항상 켜진 서버에서 돌리려면 Linux + `sudo hermes gateway install --system`
  (RUNBOOK 안내 그대로) 같은 환경이 더 적합하다.
- `WIKI_AGENT_DB`는 Step 3에서 MCP 서버에 설정한 것과 **반드시 같은 절대경로**여야 한다.
  다르면 빈 DB로 돌아 retrieval_log가 없는 무의미한 사이클이 된다.
- 매 사이클이 LLM 호출(curate/judge/eval correctness)을 발생시키므로, 스케줄 주기는
  트래픽과 비용을 보고 정한다(너무 짧으면 레이트리밋/비용 문제).
- 자동으로 돌아도 안전장치는 코드 변경 없이 그대로 작동한다: gate.py 5단계
  (provenance/daily_cap/source 다양성/중복/grounding), `promote_if_better`의 골드셋
  회귀 + `gap_recall` 게이트, `agent_generated` 미검증 source 차단 — 전부 스케줄 실행
  여부와 무관하게 매 사이클마다 동일하게 평가된다.
- 격리는 아래 "보안 메모"의 권장 사항(전용 프로필/컨테이너)을 그대로 따른다.

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

**사이클 자동 트리거**도 실제로 구성·기동했다(Step 6): Hermes Agent를 로컬에 설치하고
(`~/.hermes/hermes-agent`, uv 가상환경), wiki-agent MCP 서버를 `search_wiki`/
`submit_feedback` 2개 도구로 연결, `~/.hermes/scripts/wiki_agent_update_cycle.sh`
래퍼 + `hermes cron create ... --no-agent` 로 매일 02:00 `scripts/run_update_cycle.py`가
직접 실행되게 등록, `hermes gateway install`로 launchd 유저 서비스까지 띄워 cron이 실제로
발동하는 상태(`hermes cron status` → "Gateway is running")까지 확인됨. 단, 이 launchd
서비스는 로그인 세션 동안만 동작(위 Step 6 주의 참고) — 항상 켜진 서버 운영이 필요하면
별도 환경(Linux + 시스템 서비스)으로 옮겨야 한다.

## 다음에 깊게 팔 후보

1. **공개 환경에서의 안전한 자가 갱신**: 갱신 파이프라인 자체는 동작하지만, shadow로 쌓아두고
   사람이 승인해야 active로 가는 워크플로는 아직 없음 — 지금은 평가 회귀만으로 자동 승격됨.
