"""
wiki-agent / demo / app.py

공개 데모 채팅 백엔드. core.wiki_store.search_wiki()로 먼저 검색하고 그 결과만
근거로 답변을 생성하는 고정 RAG 파이프라인이다(eval/run_eval.py의 generate()와
동일한 패턴을 재사용 — core/pipeline/curate.py 컨벤션처럼 eval/은 import하지
않고 패턴만 따른다). 세션 상태는 서버에 두지 않고 conv_id/turn_id를 클라이언트가
들고 다닌다(Postgres 전환 전까지 stateless로 유지).

KB에 직접 쓰는 경로는 열지 않는다 — search_wiki/log_turn/submit_feedback만
호출한다(HARD CONSTRAINT: 에이전트는 KB에 직접 못 씀).

실행: uvicorn demo.app:app --reload
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import graph as graph_module
from core import wiki_store

logger = logging.getLogger("wiki_agent.demo")

DEMO_MODEL = os.environ.get("WIKI_AGENT_DEMO_MODEL", "claude-haiku-4-5")
STATIC_DIR = Path(__file__).resolve().parent / "static"
EMBED_CACHE_MAX = int(os.environ.get("WIKI_AGENT_EMBED_CACHE_MAX", "2000"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    wiki_store.init_db(seed=True)
    yield


app = FastAPI(title="wiki-agent demo", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_client = None
# entry_id+version 키 임베딩 캐시 — 코퍼스/deprecated·rejected 엔트리가 쌓여도
# 메모리가 무한히 자라지 않게 LRU 한도를 둔다(core/lru_cache.py). 메모리 미스는
# DB(wiki_embedding)를 먼저 보고서야 재인코딩하므로, 프로세스 재시작이나
# serving/mcp_server.py와의 프로세스 간 공유에도 재인코딩 비용이 들지 않는다
# (core/wiki_store.PersistentEmbeddingCache).
_graph_embed_cache = wiki_store.PersistentEmbeddingCache(maxsize=EMBED_CACHE_MAX)
_search_embed_cache = wiki_store.PersistentEmbeddingCache(maxsize=EMBED_CACHE_MAX)

# API 호출 횟수 제한 — Anthropic API는 호출당 비용이 들고 데모는 불특정 다수가
# 찍어볼 수 있어, 비용 부담 때문에 LLM 호출(generate/generate_title) 자체를
# 세 단계로 캡핑한다: 프로세스 전체의 일일 한도 + 대화 1건이 독점하지 못하게
# 막는 대화당 한도 + IP 1개가 독점하지 못하게 막는 IP별 일일 한도. 대화당 한도만
# 있으면 conv_id가 클라이언트가 만드는 값(crypto.randomUUID())이라 공격자가 매
# 요청마다 새 conv_id를 보내는 것만으로 트리비얼하게 우회할 수 있다 — IP 한도가
# 그 우회를 막는 실질적인 방어선. 한도를 넘으면 Anthropic API를 호출하지 않고
# 바로 안내 메시지로 대체한다(검색은 로컬 임베딩이라 비용이 없으므로 그대로 수행해
# retrieval_log는 계속 쌓인다 — gap 마이닝 신호 유지). 자세한 배경은 README
# "API 호출 횟수 제한" 참고.
DAILY_CALL_LIMIT = int(os.environ.get("WIKI_AGENT_DEMO_DAILY_CALL_LIMIT", "50"))
PER_CONV_CALL_LIMIT = int(os.environ.get("WIKI_AGENT_DEMO_PER_CONV_CALL_LIMIT", "10"))
PER_IP_DAILY_CALL_LIMIT = int(os.environ.get("WIKI_AGENT_DEMO_PER_IP_DAILY_LIMIT", "20"))
BUDGET_EXCEEDED_MESSAGE = (
    "오늘 API 호출 한도에 도달해 답변을 생성할 수 없습니다. 잠시 후 다시 시도해 주세요."
)

# 짧은 시간 폭주(버스트) 방어 — 위 일일/IP 한도는 "총량"만 막고, 초당 수십 건을
# 쏟아붓는 것 자체는 막지 않는다. LLM을 호출하지 않는 라우트(검색·그래프 연산)도
# 서버 CPU/로컬 임베딩 비용이 있으므로 /chat만이 아니라 미들웨어로 모든 엔드포인트에
# 적용한다.
BURST_LIMIT = int(os.environ.get("WIKI_AGENT_DEMO_BURST_LIMIT", "8"))
BURST_WINDOW_SECONDS = int(os.environ.get("WIKI_AGENT_DEMO_BURST_WINDOW_SECONDS", "10"))

# 리버스 프록시 뒤에 있을 때만 X-Forwarded-For를 신뢰한다 — 프록시가 없는데 그
# 헤더를 믿으면 누구나 자기 IP를 위조해 위 한도들을 전부 우회할 수 있다.
TRUST_PROXY = os.environ.get("WIKI_AGENT_DEMO_TRUST_PROXY", "0") == "1"

# 거대 payload로 서버를 묶어두는 시도를 막기 위한 요청 본문 크기 상한.
MAX_REQUEST_BODY_BYTES = int(os.environ.get("WIKI_AGENT_DEMO_MAX_BODY_BYTES", str(20 * 1024)))

_daily_budget = {"date": None, "count": 0}
_conv_call_counts: Dict[str, int] = {}
_ip_daily_counts: Dict[str, Dict[str, Any]] = {}
_ip_burst_log: Dict[str, list] = {}


def _client_ip(request: Request) -> str:
    """클라이언트 IP를 얻는다. TRUST_PROXY가 켜져 있을 때만 X-Forwarded-For의
    첫 번째 값을 신뢰한다(리버스 프록시가 그 헤더를 덮어써준다는 전제) — 기본값은
    `request.client.host`만 신뢰해 헤더 위조로 한도를 우회하지 못하게 한다."""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_burst_limit(ip: str) -> bool:
    """True면 통과, False면 BURST_WINDOW_SECONDS 안에 BURST_LIMIT을 넘겨 차단해야
    한다는 뜻. 고정 윈도우가 아니라 타임스탬프 목록을 들고 있다가 윈도우 밖의
    기록만 정리하는 슬라이딩 윈도우 — 윈도우 경계에서 burst가 두 배로 통과하는
    고정 윈도우의 허점을 피한다."""
    now = time.time()
    log = _ip_burst_log.setdefault(ip, [])
    cutoff = now - BURST_WINDOW_SECONDS
    while log and log[0] < cutoff:
        log.pop(0)
    if len(log) >= BURST_LIMIT:
        return False
    log.append(now)
    return True


@app.middleware("http")
async def _security_middleware(request: Request, call_next):
    """모든 엔드포인트에 적용되는 기본 방어선 — 본문 크기 상한 + IP별 버스트
    한도. LLM 호출 여부와 무관하게 검색/그래프 연산도 서버 CPU 비용이라 /chat에만
    걸면 안 된다. 라우트 핸들러보다 먼저 실행되므로 차단된 요청은 핸들러 코드를
    전혀 타지 않는다."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
        logger.warning("요청 본문 크기 초과: %s bytes, path=%s", content_length, request.url.path)
        return JSONResponse(status_code=413, content={"detail": "요청 본문이 너무 큽니다."})

    ip = _client_ip(request)
    if not _check_burst_limit(ip):
        logger.warning("버스트 한도 초과: ip=%s path=%s", ip, request.url.path)
        return JSONResponse(
            status_code=429,
            content={"detail": "요청이 너무 빠릅니다. 잠시 후 다시 시도해 주세요."},
            headers={"Retry-After": str(BURST_WINDOW_SECONDS)},
        )
    return await call_next(request)


# 되묻기(clarify)의 "진짜 하네스" 상태 — opencode의 Question System처럼 서버가
# 명확화 대기 상태(원본 질문)를 들고 있다가, 사용자가 답하면 그 컨텍스트를 그대로
# 이어받는다. 이전 버전은 클라이언트가 "원래 질문 — 선택"을 문자열로 직접 조립해
# 보내는 시뮬레이션이었는데, 그건 클라이언트가 그 형식을 정확히 안 지키면 모델이
# 맥락을 잃는 약점이 있었다 — 서버가 원본을 들고 있으면 그 문제가 없다. 프로세스
# 생애 동안만 유지(재시작하면 사라짐, 데모 규모에서는 DB까지 갈 필요 없음).
_pending_clarifications: Dict[str, Dict[str, Any]] = {}
PENDING_CLARIFY_TTL_SECONDS = 600  # 10분 안에 답하지 않으면 새 질문으로 취급


def _consume_call_budget(conv_id: str, ip: str) -> bool:
    """LLM 호출(generate/generate_title) 직전에 호출 — True면 예산을 1 차감하고
    호출을 허용, False면 한도 초과로 호출 자체를 막아야 한다는 뜻. IP 한도가
    conv_id 한도와 별도로 있는 이유: conv_id는 클라이언트가 매 요청마다 새로
    만들 수 있는 값이라(crypto.randomUUID()), 그것만으로는 같은 사람이 한도를
    무한히 우회할 수 있다."""
    today = time.strftime("%Y-%m-%d")
    if _daily_budget["date"] != today:
        _daily_budget["date"] = today
        _daily_budget["count"] = 0
    if _daily_budget["count"] >= DAILY_CALL_LIMIT:
        return False
    if _conv_call_counts.get(conv_id, 0) >= PER_CONV_CALL_LIMIT:
        return False
    ip_entry = _ip_daily_counts.setdefault(ip, {"date": today, "count": 0})
    if ip_entry["date"] != today:
        ip_entry["date"] = today
        ip_entry["count"] = 0
    if ip_entry["count"] >= PER_IP_DAILY_CALL_LIMIT:
        logger.warning("IP 일일 LLM 호출 한도 초과: ip=%s", ip)
        return False
    _daily_budget["count"] += 1
    _conv_call_counts[conv_id] = _conv_call_counts.get(conv_id, 0) + 1
    ip_entry["count"] += 1
    return True


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


# 질문 유형 힌트: 키워드 휴리스틱으로 질문 성격을 추정해 (1) generate()의 답변
# 스타일을 맞추고 (2) 검색 k를 동적으로 조절한다. 비교/troubleshooting류는
# 여러 위키 항목을 종합해야 하는 경우가 많아 k를 키우고, 단순 정의 질문은
# 적은 근거로도 충분해 k를 줄인다(잡음 섞인 근거가 늘면 답변 품질이 떨어짐).
_COMPARISON_KEYWORDS = ["vs", "compare", "comparison", "difference between", "차이", "비교"]
_TROUBLESHOOTING_KEYWORDS = [
    "safe", "danger", "risk", "fail", "error", "broken",
    "안전", "위험", "장애", "실패", "에러", "오류", "문제",
]
_HOWTO_KEYWORDS = [
    "how do i", "how should i", "how to", "configure", "implement", "set up",
    "어떻게", "설정", "구성", "구현",
]
_DEFINITION_KEYWORDS = ["what is", "what's a", "what are", "정의", "뭐야", "무엇", "무슨"]

_HINT_GUIDANCE = {
    "comparison": "사용자가 두 개 이상의 선택지를 비교해달라고 묻고 있으니, 차이점을 짧은 목록으로 명확히 대조하라.",
    "troubleshooting": "사용자가 위험/실패 시나리오에 대해 묻고 있으니, 안전한지 아닌지를 직접적으로 답하라.",
    "howto": "사용자가 설정/구현 방법을 묻고 있으니, 실무에 바로 적용할 수 있는 구체적인 가이드를 제시하라.",
    "definition": "사용자가 기초 개념의 정의를 묻고 있으니, 2~4문장으로 짧고 명확하게 답하라.",
    "general": "",
}


def classify_question(query: str) -> Dict[str, Any]:
    """질문 키워드로 유형(hint)과 동적 검색 k를 정한다. 순서는 더 구체적인
    유형(comparison/troubleshooting)을 먼저 검사해 일반적인 how-to/definition
    키워드와 겹칠 때 더 구체적인 유형이 이긴다.

    k 값은 코퍼스 규모(2026-06 기준 약 400개 엔트리)에 맞춰 보정한 값이다 —
    코퍼스가 109개였을 때 정한 더 작은 k(3~8)로는 토픽이 겹치는 엔트리가 많아져
    의도한 정답이 top-k 밖으로 밀려나는 경우가 실측으로 확인됨(예시 질문 버튼
    4/6개가 답을 못 찾음). 코퍼스가 더 커지면 이 값도 다시 올려야 한다."""
    q = query.lower()
    if any(kw in q for kw in _COMPARISON_KEYWORDS):
        return {"hint": "comparison", "k": 10}
    if any(kw in q for kw in _TROUBLESHOOTING_KEYWORDS):
        return {"hint": "troubleshooting", "k": 8}
    if any(kw in q for kw in _HOWTO_KEYWORDS):
        return {"hint": "howto", "k": 8}
    if any(kw in q for kw in _DEFINITION_KEYWORDS):
        return {"hint": "definition", "k": 6}
    return {"hint": "general", "k": 7}


def _extract_json_object(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '{'~마지막 '}' 사이만 추출(eval/run_eval.py와 동일 패턴)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def _recover_partial_answer(text: str):
    """max_tokens 한도로 JSON이 중간에 끊겨 _extract_json_object/json.loads가 실패해도,
    "answer" 필드값은 항상 JSON 맨 앞에 오므로(프롬프트가 그 순서로 요청) 정규식으로
    값만 복구한다 — 사용자에게 끊긴 중괄호/인용부호가 그대로 노출되는 것을 막는다."""
    match = re.search(r'"answer"\s*:\s*"', text)
    if not match:
        return None
    raw = text[match.end():]
    end = raw.find('",')
    value = raw[:end] if end != -1 else raw
    value = value.replace("\\\\", "\\").replace('\\"', '"').replace("\\n", "\n")
    return value.strip() or None


def generate(
    query: str, hits, model: str = DEMO_MODEL, hint: str = "general",
    force_answer: bool = False,
) -> Dict[str, Any]:
    """검색된 entry만 근거로 구조화 답변 생성 — 1회 호출로 두 응답 모양 중 하나를
    받는다(추가 호출 없이 같은 비용으로 "되묻기" 지원, opencode의 Question System
    참고: https://deepwiki.com/sst/opencode/2.5-permission-and-question-system):

    {"type": "answer", "answer": str, "entry_ids_used": [...]}
    {"type": "clarify", "question": str, "options": [str, ...]}

    검색된 항목들이 질문의 여러 해석에 걸쳐 모델이 추측해야 하는 모호한 경우에만
    clarify를 고르도록 프롬프트에 지시한다. force_answer=True면(사용자가 이미
    되묻기에 답한 후속 호출) clarify를 금지해 무한 되묻기를 막는다 — 모호한
    질문 1건당 최대 2회 호출(원 질문 1회 + 명확화 후 1회)로 상한선이 있다.

    위키 본문은 영어지만 데모는 한국어 사용자를 대상으로 하므로 질문 언어와
    무관하게 항상 한국어로 답하도록 강제한다(이전엔 질문과 같은 언어로
    답하게 했으나, 한국어 전용 UI에 맞춰 일관성을 위해 변경)."""
    if not hits:
        return {"type": "answer", "answer": "관련된 위키 항목을 찾지 못했습니다.", "entry_ids_used": []}
    context = "\n".join(
        f"- [{h['entry_id']}] {h['topic']}: {h['canonical']}" for h in hits
    )
    guidance = _HINT_GUIDANCE.get(hint, "")
    clarify_rule = (
        "Never ask a clarifying question — always answer directly, even if "
        "the question still seems ambiguous (this is a follow-up after the "
        "user already clarified once)."
        if force_answer else
        "If the wiki entries below cover several distinct interpretations of "
        "the question and you would otherwise have to guess which one the "
        "user means, ask a clarifying question instead of guessing — but only "
        "when genuinely ambiguous, not for every question."
    )
    prompt = (
        "Answer the question using ONLY the wiki entries below. "
        "If the entries don't answer the question, say so honestly instead "
        "of guessing. Always respond in Korean (한국어), regardless of the "
        "language of the question or the wiki entries. "
        f"{guidance} {clarify_rule}\n\n"
        f"Wiki entries:\n{context}\n\nQuestion: {query}\n\n"
        "Reply with JSON only, no other text, no code fences, one of these two shapes: "
        '{"type": "answer", "answer": "<질문에 대한 한국어 답변, 마크다운 가능>", '
        '"entry_ids_used": ["<실제로 답변에서 인용한 entry_id만, 없으면 빈 배열>"]} '
        'or {"type": "clarify", "question": "<한국어로 된 명확화 질문>", '
        '"options": ["<선택지를 한국어 문장으로 작성. 기술 고유명사(token bucket, '
        'connection pool 등)는 영어 표기를 유지하되, 위키 entry의 topic을 영어 '
        '그대로 복사하지 말고 짧은 한국어 설명을 덧붙일 것 — 예: \'Rate limiting '
        '(요청 빈도 제한)\'>", "..."]} (2-4개 선택지)'
    )
    try:
        resp = _anthropic_client().messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
    except Exception:  # noqa: BLE001 - API 호출 실패를 처리 안 된 500으로 노출하면 안 됨
        logger.exception("generate() Anthropic 호출 실패")
        return {
            "type": "answer",
            "answer": "답변 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            "entry_ids_used": [h["entry_id"] for h in hits],
        }
    try:
        data = json.loads(_extract_json_object(text))
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        if data.get("type") == "clarify" and not force_answer and data.get("question"):
            # options가 비어 있어도(모델이 옵션 없이 질문만 준 경우) 일반 answer로
            # 조용히 바꿔치기하면 안 된다 — 그러면 명확화 질문이라는 것 자체가
            # 사라지고 그냥 평범한 답변처럼 보여서 옵션 UI가 통째로 안 뜬다.
            # 프런트엔드는 옵션이 0개여도 자유 입력창은 항상 보여주므로, 빈
            # 배열이라도 type="clarify"를 유지하는 쪽이 사용자에게 더 명확하다.
            return {"type": "clarify", "question": data["question"], "options": data.get("options") or []}
        if "answer" in data:
            return {
                "type": "answer",
                "answer": data["answer"],
                "entry_ids_used": data.get("entry_ids_used") or [],
            }
        if data.get("type") == "clarify" and data.get("question"):
            # force_answer=True인데도 모델이 지시를 어기고 clarify를 반환한 경우(haiku가
            # "다시 묻지 마라"를 완벽히 따르지 않을 수 있음) — 다시 물어볼 수 없으니
            # 그 질문/선택지를 그대로 버리고 "끊겼다"는 메시지로 대체하면 안 된다.
            # 모델이 만들어낸 내용 자체는 멀쩩하므로 최선의 답변으로 재구성해 보여준다.
            answer = data["question"]
            if data.get("options"):
                answer += "\n\n" + "\n".join(f"- {o}" for o in data["options"])
            return {"type": "answer", "answer": answer, "entry_ids_used": [h["entry_id"] for h in hits]}

    # JSON 파싱 자체가 실패했거나(보통 max_tokens 한도로 중간에 끊김) 위 분기 중
    # 어디에도 해당하지 않는 경우 — 잘린 중괄호/인용부호를 그대로 보여주는 대신,
    # 복구 가능한 answer 텍스트를 우선 쓰고 그것도 없으면 명확한 안내 문구로 대체한다.
    recovered = _recover_partial_answer(text)
    answer = recovered or "답변 생성이 길어져 응답이 끊겼습니다. 다시 질문해 주세요."
    return {"type": "answer", "answer": answer, "entry_ids_used": [h["entry_id"] for h in hits]}


def generate_title(query: str, model: str = DEMO_MODEL) -> str:
    """대화의 첫 질문을 짧은 제목으로 요약(첫 턴에만 호출, Claude/ChatGPT 스타일
    "이전 대화" 목록 표시용). API 호출이 실패해도(백그라운드 태스크라 응답은
    이미 나갔지만) query[:40]로 조용히 폴백 — 제목은 필수가 아니므로."""
    prompt = (
        "Summarize the following user question as a short conversation "
        "title (6 words or fewer, same language as the question, no "
        "quotes, no trailing period).\n\nQuestion: " + query
    )
    try:
        resp = _anthropic_client().messages.create(
            model=model,
            max_tokens=30,
            messages=[{"role": "user", "content": prompt}],
        )
        title = next((b.text for b in resp.content if b.type == "text"), "").strip()
    except Exception:  # noqa: BLE001 - 제목 생성 실패가 백그라운드 태스크를 깨면 안 됨
        logger.exception("generate_title() Anthropic 호출 실패")
        title = ""
    return title or query[:40]


# conv_id는 보통 클라이언트가 crypto.randomUUID()로 만들지만, 엄격한 UUID
# 형식까지는 강제하지 않는다(테스트/디버깅용 conv_id처럼 사람이 읽기 좋은 값도
# 허용) — 그래도 영문/숫자/하이픈/언더스코어만 허용해 HTML/스크립트 태그가 섞인
# 값이나 임의 길이의 쓰레기 문자열이 DB(conversation_log/feedback)에 쌓이는 건
# 막는다.
_CONV_ID_RE = r"^[A-Za-z0-9_-]{1,64}$"


class ChatRequest(BaseModel):
    conv_id: str = Field(..., min_length=1, max_length=64, pattern=_CONV_ID_RE)
    turn_id: int = Field(..., ge=0, le=100_000)
    message: str = Field(..., min_length=1, max_length=2000)
    # 되묻기(clarify) 옵션을 고른 후 다시 보내는 후속 호출일 때 클라이언트가
    # true로 보냄. 서버가 이 conv_id에 대한 pending 명확화 상태를 들고 있으면
    # message는 사용자의 답변(선택한 옵션 또는 직접 입력)만 담고, 서버가 원본
    # 질문과 합쳐 generate()를 호출한다(클라이언트는 더 이상 문자열을 조립하지
    # 않음 — "진짜 하네스"의 핵심).
    force_answer: bool = False
    # 브라우저(localStorage)당 한 번 발급되는 토큰 — /conversations·
    # /history/{conv_id}가 다른 사용자의 대화를 보여주지 않게 범위를 제한하는 데
    # 쓴다(core/wiki_store.ensure_conversation_owner). 안 보내면(구버전 클라이언트)
    # 그 대화는 owner_token 없는 레거시로 남는다 — /history는 그대로 되지만
    # /conversations 목록에는 안 뜬다.
    owner_token: Optional[str] = Field(default=None, max_length=64, pattern=_CONV_ID_RE)


class FeedbackRequest(BaseModel):
    conv_id: str = Field(..., min_length=1, max_length=64, pattern=_CONV_ID_RE)
    turn_id: int = Field(..., ge=0, le=100_000)
    thumb: str = Field(..., pattern=r"^(up|down)$")
    # 👎일 때 데모 UI가 고정 후보 중 고른 짧은 이유(예: "근거 부족"). LLM 호출
    # 없이 정적 후보를 그대로 저장만 하는 비용 없는 신호 보강.
    reason: Optional[str] = Field(default=None, max_length=100)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def _title_task(conv_id: str, message: str) -> None:
    wiki_store.set_conversation_title(conv_id, generate_title(message))


def _pop_pending_clarification(conv_id: str) -> Optional[Dict[str, Any]]:
    """conv_id의 명확화 대기 상태를 꺼낸다(만료됐으면 버리고 None). pop이라
    한 번 쓰면 사라진다 — 같은 명확화에 두 번 답할 수 없게 막는다."""
    pending = _pending_clarifications.pop(conv_id, None)
    if pending is None:
        return None
    if time.time() - pending["created_at"] > PENDING_CLARIFY_TTL_SECONDS:
        return None
    return pending


@app.post("/chat")
def chat(req: ChatRequest, background_tasks: BackgroundTasks, request: Request):
    ip = _client_ip(request)

    # "진짜 하네스" 핵심: force_answer=True로 온 요청은 클라이언트가 문자열을
    # 조립해 보내는 게 아니라, 서버가 들고 있던 원본 질문(명확화를 유발했던 그
    # 질문)에 사용자의 답변(req.message)을 합친다. pending이 없거나 만료됐으면
    # (예: 사용자가 명확화 질문을 무시하고 전혀 다른 새 질문을 보냄) req.message를
    # 그대로 새 질문으로 처리 — 클라이언트가 force_answer를 잘못/늦게 보내도 안전.
    pending = _pop_pending_clarification(req.conv_id) if req.force_answer else None
    query = f"{pending['query']} — {req.message}" if pending else req.message

    question_info = classify_question(query)
    # 검색 자체는 로컬 임베딩이라 비용이 없으므로 예산과 무관하게 항상 수행한다
    # (retrieval_log가 계속 쌓여야 gap 마이닝 신호가 끊기지 않는다).
    hits = wiki_store.search_wiki(query, k=question_info["k"], cache=_search_embed_cache)

    if _consume_call_budget(req.conv_id, ip):
        result = generate(
            query, hits, hint=question_info["hint"], force_answer=bool(pending),
        )
    else:
        result = {"type": "answer", "answer": BUDGET_EXCEEDED_MESSAGE, "entry_ids_used": []}

    if result["type"] == "clarify":
        # 대화 이력에는 명확화 질문도 일반 답변처럼 그대로 남아야 새로고침/이전
        # 대화 복원 시 흐름이 보존된다. 아직 특정 엔트리를 인용한 게 아니므로
        # cited_entry_ids는 빈 배열. 서버가 원본 질문(query)을 pending으로
        # 저장해야 다음 턴에 이어받을 수 있다.
        answer = result["question"]
        cited_ids: list = []
        _pending_clarifications[req.conv_id] = {
            "query": query, "created_at": time.time(),
        }
    else:
        answer = result["answer"]
        cited_ids = result["entry_ids_used"] or [h["entry_id"] for h in hits]

    if req.owner_token:
        # 처음 한 번만 기록됨(이미 있으면 안 덮어씀) — 매 턴 호출해도 안전.
        wiki_store.ensure_conversation_owner(req.conv_id, req.owner_token)

    wiki_store.log_turn(
        req.conv_id, req.turn_id, req.message, answer,
        [h["entry_id"] for h in hits],
    )
    if req.turn_id == 0 and _consume_call_budget(req.conv_id, ip):
        # 제목 생성은 답변과 무관하므로 응답을 블로킹하지 않고 응답 전송 후
        # 백그라운드로 돌린다(이전엔 동기 호출이라 매 첫 턴마다 추가 LLM
        # 호출 레이턴시를 그대로 사용자가 기다려야 했음). 예산을 넘으면 그냥
        # 생성을 건너뛴다(제목은 필수가 아니므로 조용히 스킵).
        background_tasks.add_task(_title_task, req.conv_id, req.message)

    response = {
        "type": result["type"], "answer": answer, "retrieved": hits,
        "cited_entry_ids": cited_ids, "question_type": question_info["hint"],
    }
    if result["type"] == "clarify":
        response["question"] = result["question"]
        response["options"] = result["options"]
    return response


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    wiki_store.submit_feedback(req.conv_id, req.turn_id, req.thumb, reason=req.reason)
    return {"ok": True}


@app.get("/graph")
def graph():
    """위키 그래프 시각화용 읽기 전용 파생 뷰(core/graph.py). KB 쓰기 없음.

    프로세스 생애 동안 유지되는 _graph_embed_cache를 넘겨 코퍼스가 커져도
    바뀌지 않은 엔트리는 매 요청마다 재인코딩하지 않게 한다(core/graph.py
    build_graph()의 cache 인자, entry_id+version 키로 자동 무효화)."""
    return graph_module.build_graph(cache=_graph_embed_cache)


@app.get("/history/{conv_id}")
def history(conv_id: str, owner_token: Optional[str] = None):
    """conv_id의 대화 로그를 그대로 반환 — 채팅 UI가 새로고침 후에도 이어서
    보여줄 수 있게 한다(읽기 전용, conversation_log 조회만).

    owner_token이 기록돼 있는데(누군가 이 conv_id를 소유) 요청자가 보낸 값과
    다르면 404로 응답한다(타인 대화를 노출하는 대신 "없는 대화"처럼 보이게) —
    owner_token이 아예 기록 안 된(레거시) conv_id는 누구나 직접 conv_id를
    알면 그대로 조회 가능(기존 동작 보존)."""
    stored_owner = wiki_store.get_conversation_owner_token(conv_id)
    if stored_owner and stored_owner != owner_token:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    return {"turns": wiki_store.list_conversation(conv_id)}


@app.get("/conversations")
def conversations(owner_token: Optional[str] = None):
    """지금까지의 모든 대화 목록(미리보기 포함)을 반환 — 채팅 UI의 "이전 대화"
    패널이 conv_id 하나만 기억하는 대신 과거 대화 전체를 보여줄 수 있게 한다
    (읽기 전용, conversation_log 집계만).

    owner_token이 없으면 빈 목록을 반환한다(fail-closed) — 그 토큰으로
    ensure_conversation_owner가 등록한 대화만 보인다."""
    return {"conversations": wiki_store.list_conversations(owner_token=owner_token)}


@app.get("/cycle-history")
def cycle_history():
    """갱신 사이클별 골드셋 지표 추이(GET) — 읽기 전용(cycle_history 테이블에
    쓰는 건 scripts/run_update_cycle.py뿐, 데모 서빙 경로는 안 씀). 시간순으로
    반환되므로 프런트는 그대로 차트에 꽂으면 됨."""
    return {"cycles": wiki_store.list_cycle_history()}


@app.get("/notifications")
def notifications():
    """갱신 사이클(scripts/run_update_cycle.py) 결과 알림을 보여주는 종모양 UI의
    데이터 소스 — 읽기 전용(notifications 테이블에 쓰는 건 그 오프라인 스크립트
    뿐, 데모 서빙 경로는 절대 안 씀)."""
    return {
        "notifications": wiki_store.list_notifications(),
        "unread_count": wiki_store.count_unread_notifications(),
    }


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int):
    wiki_store.mark_notification_read(notification_id)
    return {"ok": True}
