# wiki-agent — EC2(Amazon Linux) 배포 런북

이미 떠 있는 EC2 Amazon Linux 인스턴스에 데모를 배포한다. EventBridge 같은 AWS
관리형 스케줄러는 쓰지 않고, 로컬 launchd가 하던 "매일 새벽 2시에 갱신 사이클
실행" 역할을 인스턴스의 일반 Linux cron(`cronie`)으로 그대로 옮긴다 — 별도 AWS
서비스 없이 인스턴스 안에서 전부 끝난다. HTTPS/도메인 없이
`http://<EC2 퍼블릭 IP>:8000`로 바로 접속하는 구성.

## Step 1 — 인스턴스 접속, Docker 설치

기본 SSH 사용자는 Amazon Linux AMI 기준 `ec2-user`(Ubuntu의 `ubuntu`가 아님).
패키지 매니저도 `apt` 대신 `dnf`(Amazon Linux 2023 기준 — AL2라면 `yum`+
`amazon-linux-extras install docker`로 대체).

```bash
ssh -i <key.pem> ec2-user@<EC2_PUBLIC_IP>

sudo dnf update -y
sudo dnf install -y docker git cronie
sudo systemctl enable --now docker
sudo systemctl enable --now crond
sudo usermod -aG docker $USER
exec su - $USER   # 그룹 적용을 위해 재로그인(또는 그냥 다시 ssh)
```

## Step 2 — 코드 받고 이미지 빌드

```bash
git clone https://github.com/sososo0/wiki-agent.git
cd wiki-agent
docker build -t wiki-agent:v1 -f Dockerfile .
```

> 빌드 중 torch wheel 설치 + 임베딩/rerank 모델 다운로드가 있어 메모리/CPU를
> 좀 쓴다 — t3.micro(1GB RAM)면 버거울 수 있고, t3.small(2GB) 이상을 권장.

## Step 3 — 비밀값(.env) 준비

```bash
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-여기에-실제-키
EOF
chmod 600 .env
```

`.env`는 `.gitignore`에 이미 있어 커밋되지 않는다. 절대 git에 추가하지 말 것.

## Step 4 — 컨테이너 실행 (영속 볼륨 + 재시작 정책)

```bash
mkdir -p ~/wiki-agent-data

docker run -d --name wiki-agent --restart unless-stopped \
  -p 8000:8000 \
  -e WIKI_AGENT_DB=/data/wiki_agent.db \
  --env-file .env \
  -v ~/wiki-agent-data:/data \
  wiki-agent:v1
```

- `-v ~/wiki-agent-data:/data`: SQLite DB가 인스턴스의 EBS(루트 볼륨)에 영속된다 —
  컨테이너를 지우고 다시 만들어도 DB는 그대로 남음.
- `--restart unless-stopped` + `systemctl enable docker`(Step 1에서 이미 실행):
  인스턴스가 재부팅돼도 Docker 데몬과 컨테이너가 자동으로 다시 뜬다.

## Step 5 — 위키 콘텐츠 시딩 (최초 1회)

빌드(의존성 설치)와 서빙(컨테이너 기동)에는 DB도 `ANTHROPIC_API_KEY`도 끼어들 틈이
없다 — DB는 런타임 볼륨(`/data`)이라 빌드 시점엔 존재하지 않고, 키는 이미지에
구워넣지 않고 `--env-file .env`로 런타임에만 주입되기 때문이다. 그래서 컨테이너가
떠 있다고 위키 콘텐츠가 자동으로 채워지지 않는다 — `data/corpus/`의 문서를 위키로
변환하는 ingestion은 사람이 한 번 명시적으로 실행하는 별도 단계다.

`scripts/ingest_doc.py`는 청크마다 LLM(curate)을 호출하므로 시간이 걸리고
(`data/corpus/` 전체면 청크 수백 개 단위) 비용도 든다. SSH 세션이 끊겨도 끝까지
돌도록 백그라운드로 실행한다. 기본 `--daily-cap`은 20이라 대량 초기 시딩에는
부족하므로 올려준다.

```bash
cd ~/wiki-agent
nohup docker exec -w /app wiki-agent python scripts/ingest_doc.py data/corpus --daily-cap 500 > ~/ingest_corpus.log 2>&1 &
disown
```

진행 상황 확인:

```bash
tail -f ~/ingest_corpus.log
```

먼저 `--dry-run`(LLM 호출/DB 쓰기 없음, 무료)으로 create/update/skip 분류만
미리 보고 싶다면:

```bash
docker exec -w /app wiki-agent python scripts/ingest_doc.py data/corpus --dry-run
```

> 멱등적이다(`core/pipeline/dedupe.py`가 `chunk_hash`로 변경 없는 청크를 skip) —
> DB는 영속 볼륨에 남으므로, "코드 갱신 시(재배포)" 절차로 컨테이너를 새로 띄워도
> 이 단계를 다시 실행할 필요는 없다(문서 자체가 바뀌었을 때만 다시 돌리면 됨).

## Step 6 — 그래프 한글 번역 캐시 생성 (선택)

`/static/graph.html`은 기본이 원문(영어) 표시다. 한글로 보려면
`scripts/translate_wiki_labels.py`를 실행해 `translation_cache` 테이블에 번역을
미리 만들어둬야 한다 — 원본 `wiki_entry`(topic/canonical/body_md)는 검색/평가가
의존하므로 건드리지 않고, 표시용 캐시만 따로 둔다.

```bash
docker exec -w /app wiki-agent python scripts/translate_wiki_labels.py
```

`entry_id`+`version` 기준으로 캐시 적중하면 재번역하지 않으므로(Step 5의 ingestion과
동일한 멱등 철학), 위키 콘텐츠가 새로 추가되거나 바뀔 때마다(Step 5 재실행 후, 또는
Step 9의 갱신 사이클 이후) 다시 돌려줘야 새 항목도 한글로 보인다. 캐시가 없거나
오래된 항목은 프론트가 자연스럽게 영어로 폴백한다.

## Step 7 — 보안 그룹 확인 (AWS 콘솔에서)

이 인스턴스의 보안 그룹 인바운드 규칙에 **TCP 8000**이 열려 있는지 확인할 것
(0.0.0.0/0으로 전체 공개하거나, 필요하면 특정 IP만). 22(SSH)는 이미 접속에
썼으니 열려 있을 것이다.

## Step 8 — 동작 확인

```bash
curl http://localhost:8000/                 # 인스턴스 안에서
# 바깥에서: 브라우저로 http://<EC2_PUBLIC_IP>:8000/
docker logs -f wiki-agent                    # 문제 생기면 로그 확인
```

## Step 9 — 갱신 사이클 cron 등록 (launchd 대체)

`docker exec`은 `docker run` 때 준 환경변수(`WIKI_AGENT_DB`, `ANTHROPIC_API_KEY`)를
그대로 물려받으므로 따로 다시 안 줘도 된다. crontab 한 줄에 타임스탬프 로그 파일명을
직접 넣으면 `%` 이스케이프가 번거로워, 작은 래퍼 스크립트를 하나 둔다.

```bash
mkdir -p ~/wiki-agent/logs

cat > ~/wiki-agent/run_cycle_cron.sh <<'EOF'
#!/bin/bash
LOG="$HOME/wiki-agent/logs/update_cycle_$(date +%Y%m%d_%H%M%S).log"
docker exec -w /app wiki-agent python scripts/run_update_cycle.py > "$LOG" 2>&1
EOF
chmod +x ~/wiki-agent/run_cycle_cron.sh

crontab -e
```

crontab에 한 줄 추가(6시간마다 — 00:00/06:00/12:00/18:00):

```
0 */6 * * * /home/ec2-user/wiki-agent/run_cycle_cron.sh
```

`crond`는 Step 1에서 이미 설치+활성화했다(`systemctl status crond`로 확인 가능,
재부팅 후에도 자동 시작됨) — launchd처럼 "로그인 세션 동안만" 같은 제약이 없다,
인스턴스가 켜져 있으면 항상 동작.

> Hermes의 `--no-agent` 스크립트 job에 있던 "하드코딩된 120초 타임아웃"은 여기엔
> 없다 — 평범한 Linux cron이라 사이클이 오래 걸려도(LLM 호출 포함) 안전하게
> 끝까지 돈다.

## Step 10 — (선택) 로그 retention / DB 백업도 같은 패턴으로

데이터 삭제는 되돌릴 수 없어 cron에 조용히 끼워넣지 않는 게 원칙이지만(README/
demo-operations.md 참고), 원하면 같은 방식으로 별도 cron 줄을 추가:

```
# 매주 일요일 03:00, 30일 지난 retrieval_log/feedback 삭제
0 3 * * 0 docker exec -w /app -e WIKI_AGENT_DB=/data/wiki_agent.db wiki-agent python scripts/purge_old_logs.py

# 매일 04:00, DB 스냅샷 백업
0 4 * * * docker exec -w /app -e WIKI_AGENT_DB=/data/wiki_agent.db wiki-agent python scripts/backup_db.py
```

## Step 11 — 코드 갱신 시(재배포): GitHub Actions 자동화

`main`에 push되면(PR 머지 포함) `.github/workflows/deploy-ec2.yml`이 SSH로 이 박스에
접속해 `git pull --ff-only && bash scripts/deploy_ec2.sh`를 실행한다 — 아래 수동
절차를 그대로 원격에서 트리거하는 것뿐, 별도 빌드 서버나 컨테이너 레지스트리
(ECR 등)는 쓰지 않는다(이 박스 자체가 빌드 호스트).

`scripts/deploy_ec2.sh`가 하는 일(Step 2/4와 동일한 순서):
1. 새 이미지를 먼저 빌드(`docker build`) — 실패하면 여기서 멈추고 기존 컨테이너는
   그대로 떠 있다(다운타임 없음).
2. 빌드 성공 후에만 기존 컨테이너 stop/rm, 새 컨테이너 run(`-v ~/wiki-agent-data:/data`
   동일 볼륨이라 DB는 그대로 유지).
3. `http://localhost:8000/`이 200을 반환할 때까지 최대 30초 재시도 — 실패하면
   워크플로가 실패 처리되어 Actions 탭에서 바로 보임(`docker logs wiki-agent`로
   원인 확인).

**자동화 범위 밖**: `run_update_cycle.py`(Step 9 cron), `ingest_doc.py`(Step 5),
`translate_wiki_labels.py`(Step 6)는 이 워크플로가 절대 호출하지 않는다 — 코드
배포와 위키 콘텐츠 파이프라인은 별개이며, 매 push마다 LLM을 호출하는 사고를
막기 위해 의도적으로 분리했다.

필요한 GitHub repo secrets(Settings → Secrets and variables → Actions):

| Secret | 값 |
|---|---|
| `EC2_HOST` | EC2 퍼블릭 IP |
| `EC2_USER` | `ec2-user` |
| `EC2_SSH_KEY` | Step 1에서 쓴 키페어의 **private key** 전체 내용(PEM) |

AWS IAM 자격증명은 필요 없다(이 인스턴스에 IAM role 자체가 없음, 메타데이터
엔드포인트 404로 이미 확인됨) — 워크플로는 평범한 SSH 접속만 한다.

수동으로 같은 절차를 돌리고 싶을 때(또는 워크플로 없이 디버그할 때), 박스에
SSH로 직접 들어가서:

```bash
cd ~/wiki-agent
git pull
bash scripts/deploy_ec2.sh
```

> Actions 탭에서 수동 재실행이 필요하면(예: `.env`만 바꾸고 코드는 안 바꼈을 때)
> "Run workflow" 버튼(`workflow_dispatch`)으로 커밋 없이 트리거 가능.
