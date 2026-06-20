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

import os
import re
import sys
from pathlib import Path

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


_HANGUL_RE = re.compile(r"[가-힣]")


def _is_korean(text: str) -> bool:
    return bool(_HANGUL_RE.search(text))


def generate(query: str, hits, model: str = DEMO_MODEL) -> str:
    """검색된 entry만 근거로 답변 생성 (eval/run_eval.py generate()와 동일 패턴).

    위키 본문은 영어지만 질문은 한국어로도 들어올 수 있어(core/retrieval.py가
    다국어 임베딩으로 영어 본문을 한국어 질문에 매칭) 답변은 질문과 같은
    언어로 하도록 명시한다 — 그렇지 않으면 모델이 컨텍스트 언어(영어)를
    따라가는 경향이 있다."""
    if not hits:
        return ("관련된 위키 항목을 찾지 못했습니다." if _is_korean(query)
                 else "I don't have information to answer this.")
    context = "\n".join(
        f"- [{h['entry_id']}] {h['topic']}: {h['canonical']}" for h in hits
    )
    prompt = (
        "Answer the question using ONLY the wiki entries below. Cite the "
        "entry_id you relied on. If the entries don't answer the question, "
        "say you don't know. Always answer in the same language the "
        "question was asked in, regardless of the language of the wiki "
        "entries.\n\n"
        f"Wiki entries:\n{context}\n\nQuestion: {query}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")


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
    hits = wiki_store.search_wiki(req.message, k=5, cache=_search_embed_cache)
    answer = generate(req.message, hits)
    wiki_store.log_turn(
        req.conv_id, req.turn_id, req.message, answer,
        [h["entry_id"] for h in hits],
    )
    if req.turn_id == 0:
        # 제목 생성은 답변과 무관하므로 응답을 블로킹하지 않고 응답 전송 후
        # 백그라운드로 돌린다(이전엔 동기 호출이라 매 첫 턴마다 추가 LLM
        # 호출 레이턴시를 그대로 사용자가 기다려야 했음).
        background_tasks.add_task(_title_task, req.conv_id, req.message)
    return {"answer": answer, "retrieved": hits}


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
