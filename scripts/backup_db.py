"""
wiki-agent / scripts / backup_db.py

WIKI_AGENT_DB가 가리키는 SQLite 파일을 통째로 백업하는 신뢰된 오프라인 스크립트.
단순 파일 복사(cp)는 WAL 모드에서 최근 커밋이 -wal 파일에만 있을 수 있어 일관성이
깨질 위험이 있다 — sqlite3 Online Backup API를 쓰는 core/wiki_store.backup_db가
실행 중에도 일관된 스냅샷을 보장한다.

run_update_cycle.py의 cron에 자동으로 묶지 않았다 — purge_old_logs.py와 같은 이유로,
백업 빈도/보관 위치는 운영자가 직접 정해 별도 cron job으로 추가하길 권장한다.

실행: WIKI_AGENT_DB=<절대경로> python scripts/backup_db.py [--dest <백업파일경로>]
   (--dest 생략 시 wiki_agent_backup_<unix ts>.db를 현재 디렉터리에 생성)
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def main():
    parser = argparse.ArgumentParser(description="wiki_agent DB를 백업 파일로 복제")
    parser.add_argument("--dest", default=None,
                         help="백업 파일 경로(기본: ./wiki_agent_backup_<unix ts>.db)")
    args = parser.parse_args()

    dest = args.dest or f"wiki_agent_backup_{int(time.time())}.db"
    wiki_store.init_db(seed=True)
    wiki_store.backup_db(dest)

    print(f"backed up {wiki_store.DB_PATH} -> {dest}")


if __name__ == "__main__":
    main()
