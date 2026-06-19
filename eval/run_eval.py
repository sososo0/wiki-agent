"""
wiki-agent / eval / run_eval.py

평가 하니스. core.wiki_store.search_wiki 를 retriever로 받아
recall@k, mrr, correctness(LLM-as-judge)를 계산한다.
이 함수가 모든 사이클·shadow/active 비교의 단일 기준점이다.

실행: python eval/run_eval.py
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from core import wiki_store

GEN_MODEL = os.environ.get("EVAL_GEN_MODEL", "claude-haiku-4-5")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-haiku-4-5")

GOLD_PATH = Path(__file__).resolve().parent / "gold_set.jsonl"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def load_gold(path=GOLD_PATH):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def generate(query, hits, model=GEN_MODEL):
    """검색된 entry만 근거로 답변 생성 (서빙 에이전트의 grounded 답변을 흉내)."""
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


def judge_answer(answer, ex, model=JUDGE_MODEL):
    """LLM-as-judge: 답변이 gold_answer/must_contain 기준을 충족하면 1, 아니면 0."""
    prompt = (
        "You are grading a candidate answer against a reference answer for "
        "a knowledge-base Q&A system. Judge only factual correctness and "
        "semantic coverage of the required points -- ignore style or length.\n\n"
        f"Question: {ex['q']}\n"
        f"Reference answer: {ex['gold_answer']}\n"
        f"Required points (judge semantically, not verbatim): {ex['must_contain']}\n"
        f"Candidate answer: {answer}\n\n"
        "Does the candidate answer correctly convey the reference answer and "
        "cover the required points? Reply with exactly one word: \"yes\" or \"no\"."
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return 1 if text.strip().lower().startswith("yes") else 0


def evaluate(retriever, gold, k=5, gen_fn=generate, judge_fn=judge_answer):
    """recall@k / mrr / correctness. 모든 사이클·shadow/active 비교의 단일 기준점."""
    recall, mrr, correct = 0, 0, 0
    for ex in gold:
        hits = retriever(ex["q"], k)
        ids = [h["entry_id"] for h in hits]
        if set(ex["gold_entry_ids"]) & set(ids):
            recall += 1
        for rank, eid in enumerate(ids, 1):
            if eid in ex["gold_entry_ids"]:
                mrr += 1 / rank
                break
        correct += judge_fn(gen_fn(ex["q"], hits), ex)
    n = len(gold)
    return {"recall@k": recall / n, "mrr": mrr / n, "correctness": correct / n}


def main():
    parser = argparse.ArgumentParser(description="wiki-agent 평가 하니스")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--gold", default=str(GOLD_PATH))
    parser.add_argument("--out", default=str(BASELINE_PATH))
    parser.add_argument(
        "--save-baseline", action="store_true",
        help="기존 baseline 파일이 있어도 이번 결과로 덮어쓴다(기본은 보존).")
    args = parser.parse_args()

    before = None
    out_path = Path(args.out)
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            before = json.load(f)

    wiki_store.init_db(seed=True)
    gold = load_gold(args.gold)
    scores = evaluate(wiki_store.search_wiki, gold, k=args.k)

    print(f"gold set: {len(gold)} questions, k={args.k}")
    if before:
        print(f"  {'metric':12s} {'before':>8s} {'after':>8s} {'delta':>8s}")
        for name, val in scores.items():
            b = before.get(name)
            delta = val - b if isinstance(b, (int, float)) else None
            b_str = f"{b:.3f}" if isinstance(b, (int, float)) else "n/a"
            d_str = f"{delta:+.3f}" if delta is not None else "n/a"
            print(f"  {name:12s} {b_str:>8s} {val:>8.3f} {d_str:>8s}")
    else:
        for name, val in scores.items():
            print(f"  {name:12s} {val:.3f}")

    if before is None or args.save_baseline:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"k": args.k, "n": len(gold), **scores}, f, indent=2, ensure_ascii=False)
        print(f"\nbaseline saved -> {out_path}")
    else:
        print(f"\nbaseline preserved (use --save-baseline to overwrite) -> {out_path}")


if __name__ == "__main__":
    main()
