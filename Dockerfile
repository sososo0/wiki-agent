# ---- builder: 의존성 설치 + 모델 프리캐시 ----
# 최종 이미지에는 여기서 만든 site-packages/모델 캐시만 복사하고, pip 자체의
# 캐시/메타데이터·apt 목록 같은 빌드 전용 부산물은 가져가지 않는다.
FROM python:3.11-slim AS builder

WORKDIR /app

# torch는 sentence-transformers(core/retrieval.py의 로컬 임베딩/rerank)의
# 의존성인데, 기본 pip 인덱스는 GPU(CUDA) 포함 빌드를 받아온다 — 이 데모 서버는
# GPU가 없으므로 수백 MB~GB의 순수 낭비. CPU-only wheel을 먼저 박아두면 이후
# requirements.txt 설치 시 이미 호환 버전이 깔려 있다고 보고 재설치하지 않는다.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# core/retrieval.py가 런타임에 받는 임베딩/rerank 모델을 빌드 타임에 미리 캐시한다.
# 그래야 컨테이너 기동 후 첫 검색 요청이 모델 다운로드로 막히지 않는다.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# torch/scipy/sklearn 등이 같이 끌고 오는 자기 패키지 내부 테스트 코드(런타임에
# 절대 안 import됨, site-packages 안에서만 실측 200MB+)와 바이트코드 캐시를
# 지운다. .so는 디버그 심볼을 strip해 추가로 줄인다(torch 등 컴파일된
# 라이브러리가 .so만 500MB — 실제로 실행되는 기계어 코드는 그대로, 심볼
# 테이블만 제거라 동작에 영향 없음).
RUN apt-get update && apt-get install -y --no-install-recommends binutils \
    && find /usr/local/lib/python3.11/site-packages \
         -type d \( -name test -o -name tests \) -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/site-packages \
         -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/site-packages -name "*.so" \
         -exec strip --strip-unneeded {} + 2>/dev/null || true \
    && apt-get purge -y --auto-remove binutils \
    && rm -rf /var/lib/apt/lists/*

# ---- runtime: builder의 결과물만 가져온 깨끗한 이미지 ----
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /root/.cache /root/.cache

COPY . .

# WIKI_AGENT_DB는 볼륨 마운트 경로를 가리키도록 실행 시 -e로 주입할 것
# (예: -e WIKI_AGENT_DB=/data/wiki_agent.db -v $(pwd)/data:/data).
# 볼륨 없이 띄우면 컨테이너 종료 시 DB가 사라진다 — 이번 데모 골격의 알려진
# 한계이며, Postgres 전환 전까지는 그대로 둔다.
ENV WIKI_AGENT_DB=/data/wiki_agent.db
EXPOSE 8000

CMD ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port", "8000"]
