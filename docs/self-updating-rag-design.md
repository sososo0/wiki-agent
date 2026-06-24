# Wiki Agent: 자가 갱신형 에이전트 RAG 설계

> 대화/검색 로그를 분석해 "LLM이 큐레이션하는 위키"를 자동으로 갱신하고,
> 그 결과 검색 성능이 갱신 사이클마다 측정 가능하게 향상되는 self-improving 지식 시스템.

이 문서는 처음 설계할 때 쓴 메모이며, 실제 구현은 더 단순하다(예: 별도
오케스트레이터·벡터 DB 없이 SQLite 한 파일 + Hermes cron, fact/correction 마이닝은
스코프 밖이라 gap 탐지만 구현). 현재 무엇이 실제로 동작하는지는
[README.md](../README.md)의 "현재 상태"를, 실제 코드 경로·운영 함정은
[docs/demo-operations.md](demo-operations.md)/[docs/RUNBOOK-mcp-hermes.md](RUNBOOK-mcp-hermes.md)를 참고.

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
    conversation_log / retrieval_log / feedback
  │
  ▼
③ FEEDBACK LOOP PIPELINE (배치/스케줄)
    1. Ingest
    2. Mine          gap 탐지
    3. Curate        LLM
    4. Quality Gate
    5. Reindex
    6. Eval & Promote   shadow → active
  │
  │ 회귀 없을 때만 승격
  ▼
④ WIKI STORE
    버전 · 출처 · 신뢰도 메타데이터
    + EVAL GOLD SET (회귀 탐지 기준)
  │
  └──▶ ①의 retrieve 대상으로 순환 — 루프가 닫힌다
```

**Serving 경로**(실시간)와 **Feedback Loop**(배치)를 분리한다. 위키는 버전이 있는
정형 지식 저장소이고, 검색 인덱스는 거기서 파생된다.

---

## 2. 데이터 모델

### Wiki Entry — 검색의 단위 (raw 청크가 아니라 큐레이션된 엔트리)
```json
{
  "entry_id": "wiki_0421",
  "topic": "How to configure retry backoff",
  "canonical": "...정제된 핵심 답변...",
  "body_md": "...상세 본문(검색·생성 컨텍스트로 사용)...",
  "sources": [
    {"type": "doc", "url": "...", "verified": true},
    {"type": "conversation", "conv_id": "c_1832", "verified": false}
  ],
  "provenance": "doc_verified | curated_from_logs | curated_from_web | agent_generated",
  "confidence": 1.0,
  "version": 3,
  "supersedes": "wiki_0420",
  "status": "active | shadow | deprecated | rejected"
}
```

### Conversation / Retrieval Log (append-only)
대화 턴, 검색된 entry_id+score, 사용자 피드백(👎+이유)을 각각 별도 테이블에 적재한다
(`core/wiki_store.py`). 이 로그가 다음 사이클의 마이닝 입력이다.

### Eval Gold Set (회귀 방지의 기준)
```json
{"q": "...", "must_contain": ["...","..."], "gold_answer": "...",
 "gold_entry_ids": ["wiki_0421"], "unanswerable": false}
```
골드셋(`eval/gold_set.jsonl`)은 갱신 루프가 절대 건드리지 못하게 동결한다 —
이게 있어야 "개선"을 사이클마다 증명할 수 있다.

---

## 3. Serving 경로

1. **Retrieve**: 하이브리드 검색(BM25 + dense 임베딩 + RRF) → cross-encoder rerank.
   `status="active"` 엔트리만 검색.
2. **Generate**: 검색 컨텍스트 기반 답변 + 인용(entry_id). 근거 없으면 모른다고 답함.
3. **Log**: 모든 턴을 기록 — 다음 사이클의 연료.

---

## 4. Feedback Loop 파이프라인

**Mine** — 정확히 같은 문구의 질문이 반복되고 검색 신뢰도가 낮은 주제를 "gap"으로
탐지(`core/pipeline/mine.py`). fact/correction 마이닝(대화에서 새 사실·정정을
추출)은 설계 단계에서 고려했으나 스코프 밖으로 남겨둠.

**Curate** — gap별로 LLM이 구조화된 patch(엔트리 생성/수정안)를 만든다
(`core/pipeline/curate.py`). 바로 DB에 쓰지 않고 게이트로 넘긴다.

**Quality Gate** (→ 5장) — 통과한 patch만 `shadow` 상태로 반영.

**Reindex** — 변경된 엔트리만 재임베딩(`core/pipeline/reindex.py`).

**Eval & Promote** — shadow 포함 상태로 골드셋을 재평가해 회귀가 없을 때만
`shadow → active` 승격, 있으면 그 사이클은 아무것도 커밋하지 않는다
(`core/pipeline/promote.py`의 `promote_if_better`) — "롤백은 애초에 커밋하지
않음으로 구현".

---

## 5. 피드백 오염(Drift) 방지

1. **Provenance 등급제**: `agent_generated`/`curated_from_web` 출처는 검증된
   source 없이 단독으로 `active` 승격 불가.
2. **품질 게이트**: 사실성(grounding) 체크, 기존 엔트리와의 중복/모순 탐지, 신규
   엔트리 일일 상한(`core/pipeline/gate.py`).
3. **Eval 게이트**: 골드셋 회귀 시 자동 롤백(커밋 안 함) — 나쁜 갱신이 active에
   못 들어감.
4. **출처 다양성**: 단일 출처 1건만으로 사실 승격 금지.

---

## 6. 평가

**오프라인 지표**: `recall@k`, `mrr`, LLM-as-judge 기반 correctness,
escalation_correctness(답을 모르는 질문에 올바르게 모른다고 했는지).

**머니 차트**: x축 = 갱신 사이클, y축 = recall@k/correctness — 사이클이 돌수록
우상향하는 그래프가 이 프로젝트의 결론(`/static/history.html`).

> 그래서 평가 하니스를 가장 먼저 만든다(`eval/run_eval.py`) — before가 있어야
> after를 증명한다.
