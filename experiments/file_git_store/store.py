"""
wiki-agent / experiments / file_git_store / store.py

"SQLite 대신 파일+git을 DB처럼 쓸 수 있는가"를 검증하는 격리된 프로토타입.
core/wiki_store.py(운영 코드, CLAUDE.md상 재작성 금지)는 전혀 건드리지 않고,
완전히 새 디렉터리에서만 동작한다.

엔트리 1개 = `{entry_id}.json` 파일 1개. 파일쓰기(add_entry/set_status)와 git
커밋(commit_changes)을 의도적으로 분리한다 — "파일쓰기는 락프리, 커밋은 직렬화
지점"이라는 가설을 demo.py가 실측으로 검증하기 위함(SQLite의 단일 writer 락과
정확히 비교하려면 두 단계를 합쳐버리면 안 됨).

새 의존성 없음: YAML 대신 표준 라이브러리 json, git 연동은 subprocess로 git CLI
직접 호출(GitPython 등 추가 안 함).
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        # 커밋을 만들려면 author 정보가 있어야 하는데, CI/실험 환경엔 전역 git
        # config가 없을 수 있어 repo 로컬로 고정한다(사용자의 실제 git config를
        # 건드리지 않기 위해 --global 대신 repo-local로 설정).
        subprocess.run(["git", "config", "user.email", "experiment@local"],
                        cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "file-git-store-experiment"],
                        cwd=root, check=True, capture_output=True)


def _entry_path(root: Path, entry_id: str) -> Path:
    return root / f"{entry_id}.json"


def add_entry(
    root: Path, entry_id: str, topic: str, canonical: str, body_md: str, *,
    status: str = "shadow", provenance: str = "curated_from_logs",
    confidence: float = 1.0, sources: Optional[List[Dict[str, Any]]] = None,
    supersedes: Optional[str] = None, tier: Optional[str] = None,
    version: int = 1,
) -> None:
    """파일만 쓴다 — git commit은 별도로 commit_changes()를 호출해야 한다."""
    entry = {
        "entry_id": entry_id, "topic": topic, "canonical": canonical,
        "body_md": body_md, "status": status, "provenance": provenance,
        "confidence": confidence, "sources": sources or [],
        "supersedes": supersedes, "tier": tier, "version": version,
    }
    _entry_path(root, entry_id).write_text(json.dumps(entry, ensure_ascii=False, indent=2))


def get_entry(root: Path, entry_id: str) -> Optional[Dict[str, Any]]:
    path = _entry_path(root, entry_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_entries(root: Path, status: Optional[str] = None) -> List[Dict[str, Any]]:
    entries = [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))]
    if status is not None:
        entries = [e for e in entries if e.get("status") == status]
    return entries


def set_status(root: Path, entry_id: str, status: str) -> None:
    """status만 갱신(wiki_store.set_entry_status와 동일 의도) — version은 안 올림."""
    entry = get_entry(root, entry_id)
    if entry is None:
        raise KeyError(entry_id)
    entry["status"] = status
    _entry_path(root, entry_id).write_text(json.dumps(entry, ensure_ascii=False, indent=2))


def commit_changes(root: Path, message: str) -> str:
    """git add -A && git commit. 커밋할 변경이 없으면 빈 문자열 반환(에러 안 냄) —
    여러 스레드가 거의 동시에 호출하면 .git/index.lock 경쟁이 실제로 일어난다
    (demo.py 시나리오 2가 이걸 그대로 드러낸다 — 의도적으로 감추지 않음)."""
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                             check=True, capture_output=True, text=True)
    if not status.stdout.strip():
        return ""
    result = subprocess.run(["git", "commit", "-m", message], cwd=root,
                             check=True, capture_output=True, text=True)
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          check=True, capture_output=True, text=True)
    return rev.stdout.strip()


def history(root: Path, entry_id: str) -> List[Dict[str, str]]:
    """git log가 entry_id의 버전 히스토리를 대신하는지 확인 — wiki_store.py의
    `version` 정수 컬럼 없이도 "몇 번 바뀌었는지/언제/무슨 메시지로"를 git이
    이미 들고 있다."""
    result = subprocess.run(
        ["git", "log", "--follow", "--format=%H|%ad|%s", "--date=iso-strict",
         "--", f"{entry_id}.json"],
        cwd=root, check=True, capture_output=True, text=True,
    )
    commits = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        sha, date, message = line.split("|", 2)
        commits.append({"sha": sha, "date": date, "message": message})
    return commits
