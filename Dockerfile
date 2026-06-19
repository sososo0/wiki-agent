FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# core/retrieval.py가 런타임에 받는 임베딩/rerank 모델을 빌드 타임에 미리 캐시한다.
# 그래야 컨테이너 기동 후 첫 검색 요청이 모델 다운로드로 막히지 않는다.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

COPY . .

# WIKI_AGENT_DB는 볼륨 마운트 경로를 가리키도록 실행 시 -e로 주입할 것
# (예: -e WIKI_AGENT_DB=/data/wiki_agent.db -v $(pwd)/data:/data).
# 볼륨 없이 띄우면 컨테이너 종료 시 DB가 사라진다 — 이번 데모 골격의 알려진
# 한계이며, Postgres 전환 전까지는 그대로 둔다.
ENV WIKI_AGENT_DB=/data/wiki_agent.db
EXPOSE 8000

CMD ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port", "8000"]
