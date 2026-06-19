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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def generate(query: str, hits, model: str = DEMO_MODEL) -> str:
    """검색된 entry만 근거로 답변 생성 (eval/run_eval.py generate()와 동일 패턴)."""
    if not hits:
        return "I don't have information to answer this."
    context = "\n".join(
        f"- [{h['entry_id']}] {h['topic']}: {h['canonical']}" for h in hits
    )
    prompt = (
        "Answer the question using ONLY the wiki entries below. Cite the "
        "entry_id you relied on. If the entries don't answer the question, "
        "say you don't know.\n\n"
        f"Wiki entries:\n{context}\n\nQuestion: {query}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")


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


@app.post("/chat")
def chat(req: ChatRequest):
    hits = wiki_store.search_wiki(req.message, k=5)
    answer = generate(req.message, hits)
    wiki_store.log_turn(
        req.conv_id, req.turn_id, req.message, answer,
        [h["entry_id"] for h in hits],
    )
    return {"answer": answer, "retrieved": hits}


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    wiki_store.submit_feedback(req.conv_id, req.turn_id, req.thumb)
    return {"ok": True}
