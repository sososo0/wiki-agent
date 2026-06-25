#!/bin/bash
#
# wiki-agent / scripts / deploy_ec2.sh
#
# EC2 박스 위에서 직접 실행되는 재배포 스크립트(GitHub Actions가 SSH로 호출,
# 또는 사람이 직접 ssh 후 실행해도 동일하게 동작). docs/RUNBOOK-ec2-deploy.md의
# "코드 갱신 시(재배포)" 절차를 그대로 옮긴 것 — 새 이미지를 먼저 빌드하고,
# 빌드가 성공한 뒤에야 기존 컨테이너를 stop/rm한다(빌드 실패 시 기존 컨테이너는
# 그대로 떠 있어 다운타임 없음). git pull은 이 스크립트가 아니라 호출하는 쪽
# (워크플로 또는 사람)의 책임 — 이 스크립트는 "현재 체크아웃된 코드로 빌드+재기동"만
# 한다.
#
# run_update_cycle.py(cron)/ingest_doc.py/translate_wiki_labels.py는 절대
# 호출하지 않는다 — 코드 배포와 위키 콘텐츠 파이프라인은 분리되어 있다.
#
# 실행 위치 가정: ~/wiki-agent 안에서 실행(또는 REPO_DIR로 override).
# .env, ~/wiki-agent-data 볼륨은 이미 박스에 존재한다고 가정(런북 Step 3/4) —
# 이 스크립트가 만들거나 건드리지 않는다.
#
# 실행: bash scripts/deploy_ec2.sh

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/wiki-agent}"
IMAGE_TAG="${IMAGE_TAG:-wiki-agent:v1}"
CONTAINER_NAME="${CONTAINER_NAME:-wiki-agent}"
DATA_VOLUME="${DATA_VOLUME:-$HOME/wiki-agent-data}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/}"
HEALTH_RETRIES="${HEALTH_RETRIES:-10}"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-3}"

cd "$REPO_DIR"

echo "==> building ${IMAGE_TAG} from $(git rev-parse --short HEAD)"
docker build -t "$IMAGE_TAG" -f Dockerfile .

echo "==> build succeeded, swapping container"
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm "$CONTAINER_NAME" 2>/dev/null || true

docker run -d --name "$CONTAINER_NAME" --restart unless-stopped \
  -p 8000:8000 \
  -e WIKI_AGENT_DB=/data/wiki_agent.db \
  --env-file "$REPO_DIR/.env" \
  -v "$DATA_VOLUME:/data" \
  "$IMAGE_TAG"

echo "==> waiting for ${CONTAINER_NAME} to answer health check"
for i in $(seq 1 "$HEALTH_RETRIES"); do
  status="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || echo 000)"
  if [ "$status" = "200" ]; then
    echo "==> healthy (HTTP $status) after ${i} attempt(s)"
    exit 0
  fi
  echo "    attempt ${i}/${HEALTH_RETRIES}: HTTP ${status}, retrying in ${HEALTH_WAIT_SECONDS}s"
  sleep "$HEALTH_WAIT_SECONDS"
done

echo "!! health check failed after ${HEALTH_RETRIES} attempts: container did not return HTTP 200" >&2
echo "!! check: docker logs ${CONTAINER_NAME}" >&2
exit 1
