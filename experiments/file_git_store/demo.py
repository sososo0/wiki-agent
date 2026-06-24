"""
wiki-agent / experiments / file_git_store / demo.py

"파일+git이 SQLite의 단일 writer 락(이슈 1)과 복제 불가(이슈 3)를 실제로
푸는가"를 실측으로 보여주는 실행 스크립트. pytest가 아니다 — 기존 `pytest`
실행 시 "153 passed"라는 신뢰된 신호에 이 실험 코드가 섞이지 않게 의도적으로
분리했다(experiments/file_git_store/FINDINGS.md 참고).

실행: python experiments/file_git_store/demo.py
"""

import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import store
import search as search_mod


def scenario_1_concurrent_writes_no_commit(root: Path) -> None:
    print("\n=== 시나리오 1: 다른 entry_id 동시 파일쓰기(커밋 없음) ===")
    store.init_repo(root)

    def write_one(i: int):
        store.add_entry(root, f"e{i}", f"topic {i}", f"canonical {i}", f"body {i}")
        return i

    t0 = time.time()
    errors = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(write_one, i) for i in range(10)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                errors.append(e)
    elapsed = time.time() - t0

    written = len(store.list_entries(root))
    print(f"10개 동시 쓰기 완료: {elapsed:.3f}s, 에러={len(errors)}건, 파일={written}개")
    assert errors == [], f"파일쓰기 단계에서 에러가 나면 안 됨: {errors}"
    assert written == 10
    print("[PASS] 다른 엔트리끼리 파일쓰기는 락 경합 없이 전부 성공함"
          "(SQLite였다면 전부 같은 DB 락을 다퉜을 상황).")


def scenario_2_concurrent_commits_hit_git_lock(root: Path) -> None:
    print("\n=== 시나리오 2: 동시 git commit — 직렬화 지점이 어디로 옮겨가는지 ===")

    def write_and_commit(i: int):
        store.add_entry(root, f"f{i}", f"topic {i}", f"canonical {i}", f"body {i}")
        return store.commit_changes(root, f"add f{i}")

    errors = []
    successes = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(write_and_commit, i) for i in range(10)]
        for f in as_completed(futures):
            try:
                f.result()
                successes += 1
            except Exception as e:
                errors.append(str(e))

    print(f"동시 write+commit 10건: 성공={successes}, 실패={len(errors)}")
    if errors:
        print(f"  예시 에러: {errors[0][:200]}")
        print("[FINDING] git commit은 .git/index.lock으로 직렬화된다 — 파일쓰기는"
              " 락프리지만 '커밋'은 SQLite의 단일 writer 락과 같은 종류의 직렬화"
              " 지점이다(그냥 위치만 옮겨간 것). 동시 요청이 많으면 commit을"
              " 즉시 하지 말고 큐에 모아 배치로 1번만 커밋해야 한다(시나리오 2b).")
    else:
        print("[FINDING] 이번 실행에서는 우연히 충돌 없이 끝남(git이 내부적으로"
              " 재시도/대기하거나 타이밍이 안 겹쳤을 수 있음) — 그래도 결론은"
              " 같다: commit은 여러 프로세스가 동시에 같은 repo에 쓸 때 안전하다고"
              " 보장된 연산이 아니라, 배치/큐로 직렬화를 명시적으로 설계해야 한다.")


def scenario_2b_batched_commit_avoids_lock_contention(root: Path) -> None:
    print("\n=== 시나리오 2b: 파일쓰기는 병렬로, 커밋은 1번만(배치) ===")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(store.add_entry, root, f"g{i}", f"topic {i}",
                        f"canonical {i}", f"body {i}")
            for i in range(10)
        ]
        for f in as_completed(futures):
            f.result()
    write_elapsed = time.time() - t0

    t0 = time.time()
    sha = store.commit_changes(root, "batch add g0..g9")
    commit_elapsed = time.time() - t0

    print(f"파일쓰기 10건(병렬): {write_elapsed:.3f}s, 커밋 1회(배치): {commit_elapsed:.3f}s,"
          f" commit={sha[:8]}")
    print("[PASS] 커밋을 배치로 모으면 직렬화 지점(commit)을 사이클당 1번으로"
          " 줄일 수 있다 — run_update_cycle.py 같은 배치 작업 패턴과 자연스럽게 맞음.")


def scenario_3_git_log_and_clone_as_replication(root: Path, clone_dir: Path) -> None:
    print("\n=== 시나리오 3: git log(버전 히스토리)·git clone(복제) 확인 ===")
    store.set_status(root, "e0", "active")
    store.commit_changes(root, "promote e0 to active")
    store.set_status(root, "e0", "deprecated")
    store.commit_changes(root, "deprecate e0")

    hist = store.history(root, "e0")
    print(f"e0.json 히스토리: {len(hist)}개 커밋")
    for h in hist[:3]:
        print(f"  {h['sha'][:8]} {h['date']} {h['message']}")
    assert len(hist) >= 2, "최소 2번(create+1번 이상 status 변경) 커밋이 쌓여야 함"
    print("[PASS] wiki_store.py의 정수 `version` 컬럼 없이도 git 히스토리가"
          " '몇 번 바뀌었는지/언제/왜'를 그대로 들고 있음.")

    subprocess.run(["git", "clone", str(root), str(clone_dir)],
                    check=True, capture_output=True)
    cloned_e0 = (clone_dir / "e0.json").exists()
    print(f"git clone으로 복제 성공: {cloned_e0}")
    assert cloned_e0
    print("[PASS] git clone이 복제(이슈 3)를 그대로 대신함 — 별도 복제 메커니즘 불필요.")


def scenario_4_search_without_any_db(root: Path) -> None:
    print("\n=== 시나리오 4: DB 없이 core.retrieval.hybrid_search 그대로 동작 확인 ===")
    entries = store.list_entries(root)

    # _dense_rank가 entries 텍스트 전체와 query를 "따로" embed_fn에 넘기므로
    # (core/retrieval.py:107-108), vocab을 매 호출마다 새로 만들면 두 호출의
    # 벡터 차원이 달라져 매칭이 깨진다 — entries 전체 기준으로 한 번만 고정.
    from core import retrieval as retrieval_mod
    import numpy as np
    all_texts = [retrieval_mod._entry_text(e) for e in entries]
    vocab = sorted(set(w for t in all_texts for w in search_mod._tokenize(t)))

    def stub_embed_fn(texts):
        # 토큰 겹침을 흉내내는 더미 임베딩(실제 모델 로딩 없이 동작 확인만 목적).
        vecs = []
        for t in texts:
            toks = set(search_mod._tokenize(t))
            vecs.append([1.0 if w in toks else 0.0 for w in vocab])
        arr = np.array(vecs) if vocab else np.zeros((len(texts), 1))
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms > 0, norms, 1.0)

    def stub_rerank_fn(query, texts):
        q = set(search_mod._tokenize(query))
        return [float(len(q & set(search_mod._tokenize(t)))) for t in texts]

    results = search_mod.search(
        "topic 3", entries, k=3, embed_fn=stub_embed_fn, rerank_fn=stub_rerank_fn,
    )
    print(f"쿼리 'topic 3' 결과: {[r['entry_id'] for r in results]}")
    assert results, "검색 결과가 비어 있으면 안 됨"
    print("[PASS] FTS5/SQLite 없이도 core/retrieval.py의 hybrid_search가 파일 기반"
          " entries에 그대로 동작함(검색 로직 자체는 storage-agnostic이었다는 뜻).")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="file_git_store_demo_"))
    clone_dir = Path(tempfile.mkdtemp(prefix="file_git_store_clone_"))
    print(f"실험 디렉터리: {tmp}")
    try:
        scenario_1_concurrent_writes_no_commit(tmp)
        scenario_2_concurrent_commits_hit_git_lock(tmp)
        scenario_2b_batched_commit_avoids_lock_contention(tmp)
        scenario_3_git_log_and_clone_as_replication(tmp, clone_dir)
        scenario_4_search_without_any_db(tmp)
        print("\n모든 시나리오 완료. 자세한 결론은 FINDINGS.md 참고.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(clone_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
