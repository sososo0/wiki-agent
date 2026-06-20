"""
wiki-agent / eval / run_eval.py

평가 하니스. core.wiki_store.search_wiki 를 retriever로 받아
recall@k, mrr, correctness(LLM-as-judge)를 계산한다.
이 함수가 모든 사이클·shadow/active 비교의 단일 기준점이다.

골드셋에 "unanswerable": true로 표시된 문항(KB가 답을 모르는 질문)이 있으면
escalation_correctness도 함께 계산한다 — "모를 때 모른다고 하는가"를 별도
차원으로 측정(answerable 문항의 recall@k/mrr/correctness 계산에는 영향 없음).

evaluate(qualitative=True)는 binary correctness 대신/추가로 groundedness(근거 충실도)/
completeness(필수 포인트 커버리지)/relevance(질문 적합도) 1-5 rubric을 LLM-judge 1회
호출로 함께 받아 qualitative_report(질문별 점수+rationale)를 만든다. core/pipeline/
promote.py의 promote_if_better가 정확히 "recall@k"/"correctness" 키로 회귀를 판정하므로
(HARD CONSTRAINT 경로), 이 옵트인 확장은 기존 키/의미를 절대 바꾸지 않고 새 키만
추가한다 — qualitative=False(기본값)에서는 호출부 입장에서 동작이 100% 동일하다.

실행: python eval/run_eval.py [--qualitative] [--qualitative-report PATH]
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


def judge_escalation(answer, ex, model=JUDGE_MODEL):
    """LLM-as-judge: KB가 답을 모르는 질문에 모델이 모른다고 적절히 인정했는가,
    아니면 구체적이지만 근거 없는 답을 지어냈는가(yes=올바르게 모른다고 함)."""
    prompt = (
        "You are grading whether an AI correctly declined to answer because "
        "its knowledge base had no relevant information, instead of "
        "fabricating a specific-sounding answer.\n\n"
        f"Question: {ex['q']}\n"
        f"Candidate answer: {answer}\n\n"
        "Does the candidate answer correctly admit it doesn't know / doesn't "
        "have the information, rather than inventing a confident specific "
        "answer? Reply with exactly one word: \"yes\" or \"no\"."
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return 1 if text.strip().lower().startswith("yes") else 0


def _extract_json_object(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '{'~마지막 '}' 사이만 추출."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def judge_quality(answer, ex, model=JUDGE_MODEL):
    """LLM-as-judge: judge_answer의 binary yes/no 대신 groundedness(근거 충실도)/
    completeness(필수 포인트 커버리지)/relevance(질문 적합도)를 1-5로 채점하고
    rationale을 받는다. judge_answer와 동일하게 LLM 호출 1회."""
    prompt = (
        "You are grading a candidate answer against a reference answer for "
        "a knowledge-base Q&A system, on three 1-5 dimensions:\n"
        "- groundedness: is the answer supported by real facts (no fabrication), "
        "1=fabricated, 5=fully grounded\n"
        "- completeness: does it cover the required points below, "
        "1=missing all, 5=covers all\n"
        "- relevance: does it directly address the question, "
        "1=off-topic, 5=fully on-topic\n\n"
        f"Question: {ex['q']}\n"
        f"Reference answer: {ex['gold_answer']}\n"
        f"Required points: {ex['must_contain']}\n"
        f"Candidate answer: {answer}\n\n"
        "Reply with JSON only, no other text: "
        '{"groundedness": <1-5>, "completeness": <1-5>, "relevance": <1-5>, '
        '"rationale": "one short sentence"}'
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(_extract_json_object(text))


def evaluate(
    retriever, gold, k=5, gen_fn=generate, judge_fn=judge_answer,
    escalation_judge_fn=judge_escalation,
    qualitative=False, quality_judge_fn=judge_quality,
):
    """recall@k / mrr / correctness(answerable 문항만) + escalation_correctness
    (unanswerable 문항만, 있을 때만). 모든 사이클·shadow/active 비교의 단일 기준점
    — "recall@k"/"correctness" 키와 의미는 core/pipeline/promote.py의 회귀 판정이
    의존하므로 절대 바뀌지 않는다.

    qualitative=True면 같은 gen_fn 출력(답변 재생성 없이 재사용)에 quality_judge_fn을
    추가로 1회 호출해 groundedness/completeness/relevance(1-5 평균) +
    qualitative_report(질문별 점수+rationale)를 옵트인으로 덧붙인다."""
    answerable = [ex for ex in gold if not ex.get("unanswerable")]
    unanswerable = [ex for ex in gold if ex.get("unanswerable")]

    recall, mrr, correct = 0, 0, 0
    quality_totals = {"groundedness": 0, "completeness": 0, "relevance": 0}
    qualitative_report = []
    for ex in answerable:
        hits = retriever(ex["q"], k)
        ids = [h["entry_id"] for h in hits]
        if set(ex["gold_entry_ids"]) & set(ids):
            recall += 1
        for rank, eid in enumerate(ids, 1):
            if eid in ex["gold_entry_ids"]:
                mrr += 1 / rank
                break
        answer = gen_fn(ex["q"], hits)
        correct += judge_fn(answer, ex)
        if qualitative:
            quality = quality_judge_fn(answer, ex)
            for dim in quality_totals:
                quality_totals[dim] += quality[dim]
            qualitative_report.append({"q": ex["q"], "answer": answer, **quality})
    n = len(answerable)
    result = {"recall@k": recall / n, "mrr": mrr / n, "correctness": correct / n}

    if qualitative:
        for dim, total in quality_totals.items():
            result[dim] = total / n
        result["qualitative_report"] = qualitative_report

    if unanswerable:
        escalation_correct = sum(
            escalation_judge_fn(gen_fn(ex["q"], retriever(ex["q"], k)), ex)
            for ex in unanswerable
        )
        result["escalation_correctness"] = escalation_correct / len(unanswerable)

    return result


def main():
    parser = argparse.ArgumentParser(description="wiki-agent 평가 하니스")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--gold", default=str(GOLD_PATH))
    parser.add_argument("--out", default=str(BASELINE_PATH))
    parser.add_argument(
        "--save-baseline", action="store_true",
        help="기존 baseline 파일이 있어도 이번 결과로 덮어쓴다(기본은 보존).")
    parser.add_argument(
        "--qualitative", action="store_true",
        help="groundedness/completeness/relevance 1-5 rubric을 추가로 채점한다.")
    parser.add_argument(
        "--qualitative-report", default=None,
        help="--qualitative와 함께 지정 시 질문별 점수+rationale을 이 경로에 JSON으로 저장.")
    args = parser.parse_args()

    before = None
    out_path = Path(args.out)
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            before = json.load(f)

    wiki_store.init_db(seed=True)
    gold = load_gold(args.gold)
    scores = evaluate(wiki_store.search_wiki, gold, k=args.k, qualitative=args.qualitative)
    qualitative_report = scores.pop("qualitative_report", None)

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
        n_unanswerable = sum(1 for ex in gold if ex.get("unanswerable"))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "k": args.k, "n": len(gold),
                "n_answerable": len(gold) - n_unanswerable,
                "n_unanswerable": n_unanswerable,
                **scores,
            }, f, indent=2, ensure_ascii=False)
        print(f"\nbaseline saved -> {out_path}")
    else:
        print(f"\nbaseline preserved (use --save-baseline to overwrite) -> {out_path}")


if __name__ == "__main__":
    main()
