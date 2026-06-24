# 데모 운영 디테일

README가 너무 길어져서 분리한 문서. 명령 한 줄 요약은 README의 "명령" 표를 보고,
"왜 이렇게 만들었는지/한계가 뭔지"가 필요할 때만 이 문서를 읽는다.

## 평가가 점검하는 4가지와 그 근거

"어떤 기준으로 문서가 검색/저장되는지, 실제로 답변에 유의미하게 쓰이는지"를 점검할 때
보통 떠올리는 4가지 질문과, 이 프로젝트가 그걸 각각 어떻게 측정하는지:

| 점검 | 측정 위치 | 현재 수치(k=5, 골드셋 58문항) |
|---|---|---|
| 1. 어떤 질문에 어떤 문서가 검색됐는지 | `eval/run_eval.py`의 `recall@k`/`mrr` | recall@k 0.98 / mrr 0.78 |
| 2. 그 문서가 실제 답변에 쓰였는지 | `demo/app.py`의 `generate()`가 반환하는 `entry_ids_used`(검색됐다 ≠ 인용했다) | 매 턴 `conversation_log.retrieved`에 기록 |
| 3. 답변이 기대 내용과 맞는지 | `judge_answer()`의 binary correctness + `--qualitative`의 groundedness/completeness/relevance 1-5 rubric | correctness 0.89 |
| 4. 문서가 많아져도 검색이 잘 되는지 | 코퍼스 5개 → 109개 → active 402개로 늘었는데도 `promote.promote_if_better()`가 매 갱신마다 회귀 재확인 | recall@k 0.98 유지(80배 성장에도) |

**한계 — 2번은 측정만 하고 교차검증하지 않는다.** `entry_ids_used`는 기록되지만 "검색은
됐는데 한 번도 인용 안 되는 문서가 있는지"는 별도 지표로 계산하지 않는다(코드 변경
없이 한계로만 남김).

**4번의 실제 사례 — 코퍼스를 늘리려다 게이트가 막은 적이 있다.** 2026-06-23에 신규
주제 3개(60개 청크)를 ingestion했는데, 새 콘텐츠의 캐시/TTL 어휘가 골드셋의
unanswerable 문항과 겹쳐 `escalation_correctness`가 떨어지는 회귀가 매번 감지돼
active 승격이 막혔다. `--k`를 5→8로 올리면 `recall@k`는 회복되지만
`escalation_correctness`는 더 떨어지는 트레이드오프만 확인됐다(파라미터 조정으로
해결 안 되는 진짜 회귀). 강제 승격 없이 60개 전부 `shadow`로만 남겼다 —
`/static/graph.html`에서 직접 확인 가능.

## 위키 그래프 시각화

`/static/graph.html`은 위키 엔트리를 라이프사이클 상태별(active/shadow/deprecated/
rejected)로 색칠한 노드와 관계 엣지로 그린 인터랙티브 그래프(`core/graph.py`의
`build_graph()`, 읽기 전용, `GET /graph`가 JSON 반환). 엣지 타입: `pending_update`
(shadow→교체 대상), `superseded_by`(deprecated→과거 대상), `similar`/`cluster_similar`
(임베딩 코사인 유사도, 무방향). 노드가 많아지면 임베딩 기준 k-means로 클러스터의
대표(backbone) 노드만 먼저 보여주고 클릭하면 그 안의 멤버만 펼쳐진다.

좌측 검색 가능한 목록에서 항목 클릭 → 그래프 포커스 + 우측 상세 패널.
`?focus=<entry_id>`로 직접 진입해도 동일 동작(채팅 답변의 "근거" 링크가 이 방식).

**한계**: `supersedes`는 마지막 한 단계만 가리켜 다단계 버전 이력 전체는 못 봄.
`similar` 엣지는 임베딩 모델이 바뀌면 구성도 바뀜. MCP에는 노출하지 않음(에이전트가
그래프로 KB 내부 상태를 추론/조작하는 경로를 새로 열지 않기 위함).

## API 호출 횟수 제한 / 봇 트래픽 방어

데모는 공개 엔드포인트라 채팅 1턴마다 Anthropic API를 호출하므로 비용 캡을 둔다
(`demo/app.py`의 `_consume_call_budget`):

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `WIKI_AGENT_DEMO_DAILY_CALL_LIMIT` | `50` | 프로세스 전체 일일 LLM 호출 한도 |
| `WIKI_AGENT_DEMO_PER_CONV_CALL_LIMIT` | `10` | 대화 1건당 최대 호출 수 |
| `WIKI_AGENT_DEMO_PER_IP_DAILY_LIMIT` | `20` | IP 1개의 일일 최대 호출 수(`conv_id`는 클라이언트가 임의로 새로 만들 수 있어, 대화당 한도만으론 트리비얼하게 우회 가능 — IP 한도가 실질 방어선) |
| `WIKI_AGENT_DEMO_BURST_LIMIT` / `_BURST_WINDOW_SECONDS` | `8` / `10` | IP 1개가 윈도우 동안 보낼 수 있는 최대 요청 수(모든 엔드포인트 적용 — `/graph`/`/history` 등 로컬 연산도 CPU 비용이 있어서) |
| `WIKI_AGENT_DEMO_MAX_BODY_BYTES` | `20480` | 초과 시 파싱 없이 413 |
| `WIKI_AGENT_DEMO_TRUST_PROXY` | `0` | `1`이면 `X-Forwarded-For` 신뢰. **리버스 프록시 뒤에서만 켤 것**(아니면 누구나 헤더 위조로 IP 한도 우회 가능) |

한도 도달 시 API를 호출하지 않고 고정 문구로 응답. `search_wiki`(로컬 임베딩, 비용
없음)는 한도와 무관하게 항상 수행되어 `retrieval_log`는 계속 쌓인다 — 막히는 건
LLM 호출(`generate`)뿐이라 gap 마이닝 신호는 끊기지 않음. 카운터는 메모리뿐이라
재시작하면 초기화(데모 규모에서는 DB까지 갈 필요 없다고 판단).

`ChatRequest`/`FeedbackRequest`는 `pydantic.Field`로 길이/형식 제약(message 1~2000자,
conv_id는 UUID 형식 등) — 위반 시 자동 422. SQL 인젝션은 전체 파라미터 바인딩(`?`)이라
원래부터 안전.

**한계(범위 밖)**: `/conversations`/`/history/{conv_id}`는 인증이 없어 누구나 모든
대화를 볼 수 있음 — 단일 SQLite 파일 기반 멀티유저 미지원의 연장선. 고치려면
`conv_id` 발급 시 `owner_token`을 같이 만들어 `conversation_meta`에 저장하고 조회 시
요구하는 방식이 필요(데이터 모델 변경, 별도 작업).

## 되묻기(clarify) + 피드백 이유

질문이 모호하면(예: "타임아웃을 어떻게 설정해?" → connect/read/total 중 어느 것인지)
`generate()`가 바로 추측하지 않고 선택지가 있는 명확화 질문을 반환한다(opencode의
Question System 패턴 참고). **추가 LLM 호출 없이** 같은 1회 호출 안에서
`{"type":"answer"}` 또는 `{"type":"clarify","question","options"}`로 응답.

서버가 명확화 대기 상태를 메모리에 들고 있는다(`_pending_clarifications`,
`conv_id` 키로 원본 질문 저장). 사용자가 옵션을 고르면 클라이언트는 선택 텍스트만
`force_answer:true`로 다시 보내고, 서버가 원본 질문과 합쳐 1회 더 호출(모호한 질문
1건당 최대 2회 호출 상한). pending은 10분(`PENDING_CLARIFY_TTL_SECONDS`) 후 만료.

👎 피드백은 클릭 즉시가 아니라 짧은 이유 후보(근거 부족/주제와 무관/너무 추상적/
사실과 다름/이유 없음)를 보여주고 고른 이유와 함께 전송(정적 후보, LLM 호출 없음).
`feedback.reason` 컬럼에 저장 — 현재는 신호 적재까지만, 파이프라인 활용(daily_cap
조정 등)은 별도 작업.

## 갱신 사이클 알림(🔔) / 추이(📈)

`run_update_cycle.py`가 Hermes cron으로 매일 새벽 2시 자동 실행되는데, 로그 파일에만
남으면 아무도 안 본다. 그래서 사이클이 끝날 때마다 `run_cycle()`이 이미 들고 있는
구조화된 결과(mined/feedback/shadow_written/promote)에서 **직접**(텍스트 파싱 없이)
알림 0~2건을 만들어 `notifications` 테이블에 적재(`summary_notifications()`):

| 레벨 | 발생 조건 |
|---|---|
| `info` | 매 사이클 항상 1건 |
| `warning` | 최근 피드백 5건 이상 & 👎 비율 > 50% |
| `warning` | shadow가 새로 생겼는데 회귀로 승격 안 됨 |
| `error` | 사이클 자체가 예외로 죽음(그대로 재raise — hermes cron 실패 상태 보존) |

쓰기는 오직 `run_update_cycle.py`뿐이고 KB(`wiki_entry`)와 무관해 "에이전트는 KB에
직접 못 씀" 제약과 충돌하지 않는다. 헤더 🔔가 안 읽은 개수를 배지로 보여주고 60초
폴링으로 갱신(`GET /notifications`, `POST /notifications/{id}/read`).

알림이 "사이클 1건의 텍스트 요약"이라면, `cycle_history` 테이블은 사이클마다 "그
시점에 실제로 active인" 골드셋 지표(recall@k/mrr/correctness/escalation_correctness)를
구조화된 행으로 남긴다 — `/static/history.html`에서 표 + `<canvas>` 추이 차트로 확인
(새 라이브러리 추가 없음). 사이클이 1개뿐이면 차트 대신 "2개 이상 필요" 안내.

## 위키 자가 갱신 확인 시 주의할 점

- **`promoted`는 실행마다 바뀔 수 있음**: `correctness`는 매번 실제 LLM 호출로
  채점하는 비결정적 지표라 ±1문항(58문항 기준 약 0.02) 흔들릴 수 있음. `recall@k`/
  `mrr`는 결정적이라 그쪽이 변하면 진짜 회귀.
- **반복 실행하면 골드셋 unanswerable 문항이 오염될 수 있음**: eval을 자주 돌리면
  그 평가 질문도 `retrieval_log`에 쌓이고, 5개 unanswerable 문항이 3번 이상 같은
  문구로 쌓이면 `mine_gaps`가 진짜 gap으로 오인할 수 있다 — 실제로 한 번 발생했고
  `promote_if_better`가 `escalation_correctness`(1.0→0.0) 하락을 잡아내 승격을
  막아냈다. 이후 마이닝 윈도우(`--window-days`, 기본 14일)와 거부된 gap 기억으로
  완화했지만, 윈도우 안에서 처음 오염되는 경우 자체는 못 막으므로 평가를 자주
  돌렸다면 사이클 전 `retrieval_log` 점검 권장.

## 문서 ingestion 한계

문서 구조가 바뀌어 섹션이 분리/병합되면 새 entry_id가 생기고 옛 entry_id는 자동
deprecated되지 않음(수동 정리 필요). `promote.py`의 회귀 체크는 골드셋 기준
recall@k/correctness만 보므로, ingestion 자체의 게이트 통과율 같은 별도 스케일링
지표는 없음.

## 데이터 파이프라인 스케일링

지금 규모(active 400여 엔트리, 로그 수백~수천 행)에선 아래 항목들이 안 보이지만,
처음 3개는 실제로 해소해뒀다(나머지는 여전히 다음에 손볼 지점):

- ~~인덱스 없음~~ → **해소**: `wiki_entry(status, updated_at)`, `retrieval_log(ts)`,
  `feedback(ts)`에 `CREATE INDEX IF NOT EXISTS`(`core/wiki_store.py` SCHEMA, 기존 DB에도
  안전하게 적용).
- ~~임베딩 캐시가 무한히 자람~~ → **해소**: `core/lru_cache.py`의 `LRUCache`(표준
  라이브러리만 사용)로 `demo/app.py`/`serving/mcp_server.py`의 캐시를 교체, 기본
  2000개 한도(`WIKI_AGENT_EMBED_CACHE_MAX`로 조정 가능) — 넘으면 가장 오래 안 쓰인
  entry_id부터 evict.
- ~~로그 테이블에 retention 정책 없음~~ → **해소**: `scripts/purge_old_logs.py`
  (`wiki_store.purge_old_logs`)가 retrieval_log/feedback의 30일(기본,
  `--retention-days`로 조정) 이전 행을 삭제. `conversation_log`은 `/conversations`
  UI가 보여주는 사용자 대화 기록이라 의도적으로 제외. **cron에는 자동으로 안 묶음** —
  데이터 삭제는 되돌릴 수 없어서, 기존 `run_update_cycle.py` cron에 조용히 끼워넣지
  않고 운영자가 명시적으로 별도 job(`hermes cron create ... --script
  purge_old_logs.sh`)으로 추가하길 권장.
- ~~SQLite 동시 쓰기 충돌~~ → **해소**: `core/wiki_store.py`의 `init_db()`가
  `PRAGMA journal_mode=WAL`을 켜서 쓰기 중에도 읽기가 안 막히고, `_conn()`이
  `timeout=5.0`(busy_timeout 5000ms)을 줘서 쓰기끼리 겹쳐도 즉시 "database is
  locked"로 죽는 대신 재시도한다. 쓰기끼리 동시에 1개만 가능한 SQLite 근본 한계
  자체는 그대로(멀티유저화하려면 결국 Postgres 전환 필요, 아래 항목).
- ~~그래프 similar 엣지 계산이 O(n²)~~ → **해소**: `core/graph.py`의 `build_graph()`가
  클러스터링을 먼저 한 뒤 "similar" 엣지를 클러스터 내부에서만 계산(전체 n×n 대신
  ~target_cluster_size개끼리만 비교). 실측: n=50,000에서 유사도 행렬 계산만 0.07초
  → 클러스터 한정 0.15초로 별 차이 없어 보이지만, **옛 방식은 그 행렬 자체가
  19.5GB**라 실제로는 메모리 부족으로 못 돌았을 상황.
- ~~`compute_clusters`(k-means)도 사실상 O(n²)~~ → **해소**: 클러스터 개수
  `k`가 `n/target_cluster_size`로 n에 비례해 늘어나면 centroid 할당 행렬곱
  (`vecs @ centroids.T`)이 `O(n·k) = O(n²/target_cluster_size)`라 사실상
  2차였다(실측 n=50,000에서 약 37초). 1) 클러스터별 멤버를 모으던 Python
  `for` 루프를 `np.add.at`/정렬 기반으로 벡터화 + 2) `max_clusters`(기본 500)로
  k 자체에 상한을 둬 n이 커져도 `O(n·max_clusters)`(선형)로 묶었다 — 결과:
  n=50,000 37초→4.15초, n=200,000도 16.6초(선형 확인, 상한 전이면 2차라 훨씬
  느렸을 것). **트레이드오프**: 코퍼스가 `target_cluster_size × max_clusters`
  (기본 12×500=6,000)를 넘으면 클러스터당 멤버 수가 12개보다 커진다(예:
  20만개면 클러스터당 평균 400개) — backbone 클릭 시 펼쳐지는 화면이 더 커짐.
- **`curate()` 호출이 daily_cap에 안 묶임** — `gate.py`는 비용 큰 grounding judge를
  daily_cap 뒤로 미루지만, gap 루프는 게이트 통과 전 매 gap마다 무조건 `curate()`를
  호출 — gap이 50개면 daily_cap 5여도 curate는 50번 다 나감.
- **"정확히 같은 문구 3번 이상"** — 같은 의미가 다양한 문구로 흩어지면 탐지율 ↓
  (의도된 단순화, paraphrase 클러스터링은 스코프 밖).
- **SQLite 단일 파일 = 단일 writer** — WAL로 읽기-쓰기 경합은 줄였지만 쓰기끼리는
  여전히 1개만 가능 — 멀티유저화와 같은 근본 원인.
