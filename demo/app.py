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
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import graph as graph_module
from core import wiki_store

DEMO_MODEL = os.environ.get("WIKI_AGENT_DEMO_MODEL", "claude-haiku-4-5")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    wiki_store.init_db(seed=True)
    yield


app = FastAPI(title="wiki-agent demo", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_client = None
_graph_embed_cache: dict = {}
_search_embed_cache: dict = {}


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
    키워드와 겹칠 때 더 구체적인 유형이 이긴다."""
    q = query.lower()
    if any(kw in q for kw in _COMPARISON_KEYWORDS):
        return {"hint": "comparison", "k": 8}
    if any(kw in q for kw in _TROUBLESHOOTING_KEYWORDS):
        return {"hint": "troubleshooting", "k": 6}
    if any(kw in q for kw in _HOWTO_KEYWORDS):
        return {"hint": "howto", "k": 6}
    if any(kw in q for kw in _DEFINITION_KEYWORDS):
        return {"hint": "definition", "k": 3}
    return {"hint": "general", "k": 5}


def _extract_json_object(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '{'~마지막 '}' 사이만 추출(eval/run_eval.py와 동일 패턴)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def generate(query: str, hits, model: str = DEMO_MODEL, hint: str = "general") -> Dict[str, Any]:
    """검색된 entry만 근거로 구조화 답변 생성. {"answer": str, "entry_ids_used":
    [entry_id...]} 형태로 반환 — entry_ids_used는 모델이 실제로 답변에 인용한
    항목만 모델 스스로 표시한 값이라, retrieved(검색된 전체 후보)와 달리 UI에서
    "실제 근거"만 보여줄 수 있다.

    위키 본문은 영어지만 데모는 한국어 사용자를 대상으로 하므로 질문 언어와
    무관하게 항상 한국어로 답하도록 강제한다(이전엔 질문과 같은 언어로
    답하게 했으나, 한국어 전용 UI에 맞춰 일관성을 위해 변경)."""
    if not hits:
        return {"answer": "관련된 위키 항목을 찾지 못했습니다.", "entry_ids_used": []}
    context = "\n".join(
        f"- [{h['entry_id']}] {h['topic']}: {h['canonical']}" for h in hits
    )
    guidance = _HINT_GUIDANCE.get(hint, "")
    prompt = (
        "Answer the question using ONLY the wiki entries below. "
        "If the entries don't answer the question, say so honestly instead "
        "of guessing. Always respond in Korean (한국어), regardless of the "
        "language of the question or the wiki entries. "
        f"{guidance}\n\n"
        f"Wiki entries:\n{context}\n\nQuestion: {query}\n\n"
        "Reply with JSON only, no other text, no code fences: "
        '{"answer": "<질문에 대한 한국어 답변, 마크다운 가능>", '
        '"entry_ids_used": ["<실제로 답변에서 인용한 entry_id만, 없으면 빈 배열>"]}'
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(_extract_json_object(text))
        if isinstance(data, dict) and "answer" in data:
            return {
                "answer": data["answer"],
                "entry_ids_used": data.get("entry_ids_used") or [],
            }
    except (json.JSONDecodeError, ValueError):
        pass
    # 모델이 JSON을 안 지켰을 때의 안전한 폴백 — 원문 그대로 답변으로 쓰고
    # 인용 항목은 검색된 전체 후보로 대체한다.
    return {"answer": text, "entry_ids_used": [h["entry_id"] for h in hits]}


def generate_title(query: str, model: str = DEMO_MODEL) -> str:
    """대화의 첫 질문을 짧은 제목으로 요약(첫 턴에만 호출, Claude/ChatGPT 스타일
    "이전 대화" 목록 표시용)."""
    prompt = (
        "Summarize the following user question as a short conversation "
        "title (6 words or fewer, same language as the question, no "
        "quotes, no trailing period).\n\nQuestion: " + query
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=30,
        messages=[{"role": "user", "content": prompt}],
    )
    title = next((b.text for b in resp.content if b.type == "text"), "").strip()
    return title or query[:40]


class ChatRequest(BaseModel):
    conv_id: str
    turn_id: int
    message: str


class FeedbackRequest(BaseModel):
    conv_id: str
    turn_id: int
    thumb: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def _title_task(conv_id: str, message: str) -> None:
    wiki_store.set_conversation_title(conv_id, generate_title(message))


@app.post("/chat")
def chat(req: ChatRequest, background_tasks: BackgroundTasks):
    question_info = classify_question(req.message)
    hits = wiki_store.search_wiki(req.message, k=question_info["k"], cache=_search_embed_cache)
    result = generate(req.message, hits, hint=question_info["hint"])
    answer = result["answer"]
    cited_ids = result["entry_ids_used"] or [h["entry_id"] for h in hits]
    wiki_store.log_turn(
        req.conv_id, req.turn_id, req.message, answer,
        [h["entry_id"] for h in hits],
    )
    if req.turn_id == 0:
        # 제목 생성은 답변과 무관하므로 응답을 블로킹하지 않고 응답 전송 후
        # 백그라운드로 돌린다(이전엔 동기 호출이라 매 첫 턴마다 추가 LLM
        # 호출 레이턴시를 그대로 사용자가 기다려야 했음).
        background_tasks.add_task(_title_task, req.conv_id, req.message)
    return {
        "answer": answer, "retrieved": hits,
        "cited_entry_ids": cited_ids, "question_type": question_info["hint"],
    }


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    wiki_store.submit_feedback(req.conv_id, req.turn_id, req.thumb)
    return {"ok": True}


@app.get("/graph")
def graph():
    """위키 그래프 시각화용 읽기 전용 파생 뷰(core/graph.py). KB 쓰기 없음.

    프로세스 생애 동안 유지되는 _graph_embed_cache를 넘겨 코퍼스가 커져도
    바뀌지 않은 엔트리는 매 요청마다 재인코딩하지 않게 한다(core/graph.py
    build_graph()의 cache 인자, entry_id+version 키로 자동 무효화)."""
    return graph_module.build_graph(cache=_graph_embed_cache)


@app.get("/history/{conv_id}")
def history(conv_id: str):
    """conv_id의 대화 로그를 그대로 반환 — 채팅 UI가 새로고침 후에도 이어서
    보여줄 수 있게 한다(읽기 전용, conversation_log 조회만)."""
    return {"turns": wiki_store.list_conversation(conv_id)}


@app.get("/conversations")
def conversations():
    """지금까지의 모든 대화 목록(미리보기 포함)을 반환 — 채팅 UI의 "이전 대화"
    패널이 conv_id 하나만 기억하는 대신 과거 대화 전체를 보여줄 수 있게 한다
    (읽기 전용, conversation_log 집계만)."""
    return {"conversations": wiki_store.list_conversations()}
