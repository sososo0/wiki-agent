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

## Step 5 — 보안 그룹 확인 (AWS 콘솔에서)

이 인스턴스의 보안 그룹 인바운드 규칙에 **TCP 8000**이 열려 있는지 확인할 것
(0.0.0.0/0으로 전체 공개하거나, 필요하면 특정 IP만). 22(SSH)는 이미 접속에
썼으니 열려 있을 것이다.

## Step 6 — 동작 확인

```bash
curl http://localhost:8000/                 # 인스턴스 안에서
# 바깥에서: 브라우저로 http://<EC2_PUBLIC_IP>:8000/
docker logs -f wiki-agent                    # 문제 생기면 로그 확인
```

## Step 7 — 갱신 사이클 cron 등록 (launchd 대체)

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

## Step 8 — (선택) 로그 retention / DB 백업도 같은 패턴으로

데이터 삭제는 되돌릴 수 없어 cron에 조용히 끼워넣지 않는 게 원칙이지만(README/
demo-operations.md 참고), 원하면 같은 방식으로 별도 cron 줄을 추가:

```
# 매주 일요일 03:00, 30일 지난 retrieval_log/feedback 삭제
0 3 * * 0 docker exec -w /app -e WIKI_AGENT_DB=/data/wiki_agent.db wiki-agent python scripts/purge_old_logs.py

# 매일 04:00, DB 스냅샷 백업
0 4 * * * docker exec -w /app -e WIKI_AGENT_DB=/data/wiki_agent.db wiki-agent python scripts/backup_db.py
```

## 코드 갱신 시(재배포)

```bash
cd ~/wiki-agent
git pull
docker build -t wiki-agent:v1 -f Dockerfile .
docker stop wiki-agent && docker rm wiki-agent
# Step 4의 docker run 명령을 그대로 다시 실행 — 같은 -v 볼륨이라 DB는 유지됨
```
