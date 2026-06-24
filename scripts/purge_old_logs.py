"""
wiki-agent / scripts / purge_old_logs.py

retrieval_log/feedback의 retention 정책을 집행하는 신뢰된 오프라인 스크립트.
--window-days(scripts/run_update_cycle.py)는 mine_gaps가 보는 범위를 제한할 뿐
삭제는 하지 않으므로, 이 스크립트가 없으면 두 테이블은 영원히 자란다.

conversation_log는 의도적으로 건드리지 않는다 — `/conversations` UI가 보여주는
사용자 대화 기록이라 마이닝 입력 로그와는 별개의 보존 정책이 필요하다고 판단했다
(core/wiki_store.purge_old_logs 주석 참고).

run_update_cycle.py의 cron에 자동으로 묶지 않았다 — 데이터 삭제는 되돌릴 수 없는
작업이라, 기존에 매일 자동 실행되는 사이클에 조용히 추가하기보다 운영자가 명시적으로
이 스크립트를 별도 cron job으로 추가하기를 권장한다.

실행: WIKI_AGENT_DB=<절대경로> python scripts/purge_old_logs.py [--retention-days 30]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wiki_store


def main():
    parser = argparse.ArgumentParser(
        description="retrieval_log/feedback의 오래된 행을 삭제(retention 정책 집행)")
    parser.add_argument("--retention-days", type=float, default=30,
                         help="이보다 오래된 행을 삭제(기본 30일 — mine_gaps의 마이닝 "
                              "윈도우 14일보다 넉넉히 길게 잡아 안전 마진을 둠)")
    args = parser.parse_args()

    wiki_store.init_db(seed=True)
    result = wiki_store.purge_old_logs(retention_days=args.retention_days)

    print(f"retention_days: {args.retention_days}")
    print(f"retrieval_log deleted: {result['retrieval_log_deleted']}")
    print(f"feedback deleted: {result['feedback_deleted']}")


if __name__ == "__main__":
    main()
