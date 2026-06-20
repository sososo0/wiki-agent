"""
wiki-agent / eval / agentic_eval.py

멀티홉 에이전틱 태스크 평가. run_eval.py의 골드셋은 "질문 1개 -> 검색 1회 ->
정답 엔트리 1개"만 다루지만, 실제 서빙 에이전트(Hermes 등)는 search_wiki를
도구로 여러 번 호출해 정보를 조합해야 하는 태스크도 풀어야 한다. 이 스크립트는
그 능력을 ReAct 스타일 루프(decide -> search|answer)로 별도 측정한다.

진단/리포트 전용 — core/pipeline/promote.py의 shadow->active 승격 게이트에는
연결하지 않는다(HARD CONSTRAINT: 그 경로는 evaluate_fn의 "recall@k"/"correctness"
키만 보며, 이 스크립트는 그 계약을 건드리지 않는다). 쓰기 도구도 노출하지 않는다
— 에이전트가 가진 유일한 도구는 search_wiki(읽기 전용)다.

실행: python eval/agentic_eval.py [--max-turns 4]
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

DECIDE_MODEL = os.environ.get("EVAL_GEN_MODEL", "claude-haiku-4-5")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-haiku-4-5")

GOLD_PATH = Path(__file__).resolve().parent / "agentic_gold_set.jsonl"

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _extract_json_object(text: str) -> str:
    """모델이 코드펜스/설명을 덧붙여도 첫 '{'~마지막 '}' 사이만 추출."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]


def load_agentic_gold(path=GOLD_PATH):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _format_transcript(transcript):
    if not transcript:
        return "(no searches yet)"
    lines = []
    for i, step in enumerate(transcript, 1):
        topics = ", ".join(h["topic"] for h in step["hits"]) or "(no results)"
        lines.append(f"{i}. searched \"{step['query']}\" -> [{topics}]")
    return "\n".join(lines)


def decide_next_action(task, transcript, model=DECIDE_MODEL):
    """LLM 1회 호출로 다음 행동 결정: 검색을 더 할지, 지금까지 모은 정보로
    답할지. {"action": "search", "query": str} | {"action": "answer", "answer": str}.

    모델이 지시를 무시하고 JSON 대신 긴 산문 답변을 바로 써버리는 경우가 haiku에서
    관찰됨(특히 정보가 충분히 모인 턴) — 그 경우 JSON 파싱이 실패해도 크래시하지
    않고, 응답 전체를 answer로 취급한다(모델이 사실상 답을 한 것이므로 의미상 맞음)."""
    prompt = (
        "You are an agent answering a task by searching a wiki knowledge base. "
        "You can call search multiple times to gather information from "
        "different entries before answering. Decide the next step.\n\n"
        f"Task: {task}\n\n"
        f"Search history so far:\n{_format_transcript(transcript)}\n\n"
        "If you need more information from a different angle than what you've "
        "already searched, reply with JSON: "
        '{"action": "search", "query": "..."}\n'
        "If you have enough information to answer the task fully, reply with "
        'JSON: {"action": "answer", "answer": "..."} where the answer cites the '
        "entry topics it relied on. Keep the answer to 2-3 plain-prose sentences, "
        "no markdown headings or bullet lists.\n"
        "Reply with JSON only, no markdown, no code fences, no other text."
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(_extract_json_object(text))
    except json.JSONDecodeError:
        return {"action": "answer", "answer": text}


def force_final_answer(task, transcript, model=DECIDE_MODEL):
    """max_turns 도달 시, 그때까지 모은 정보만으로 답을 강제로 1회 요청."""
    prompt = (
        "You are an agent answering a task by searching a wiki knowledge base. "
        "You have run out of search budget -- answer now using only the search "
        "results gathered so far, citing the entry topics you relied on. If "
        "they are insufficient, say what's missing. Plain prose, no markdown "
        "headings or bullet lists, 2-4 sentences.\n\n"
        f"Task: {task}\n\nSearch history:\n{_format_transcript(transcript)}"
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")


def run_agent_task(
    task, *, search_fn=None, decide_fn=None, force_answer_fn=None,
    max_turns=4, k=5,
):
    """매 턴 decide_fn(task, transcript) 호출 -> "search"면 search_fn(query, k)
    실행 후 transcript에 추가, "answer"면 종료. max_turns 도달 시 force_answer_fn
    으로 강제 종료(그때까지 모은 정보로 최종 답 1회 더 요청)."""
    search_fn = search_fn or wiki_store.search_wiki
    decide_fn = decide_fn or decide_next_action
    force_answer_fn = force_answer_fn or force_final_answer

    transcript = []
    retrieved_ids = set()
    tool_calls = 0

    for _ in range(max_turns):
        decision = decide_fn(task, transcript)
        if decision.get("action") == "answer":
            return {
                "answer": decision.get("answer", ""),
                "tool_calls": tool_calls,
                "retrieved_ids": retrieved_ids,
            }
        query = decision.get("query", task)
        hits = search_fn(query, k)
        tool_calls += 1
        retrieved_ids.update(h["entry_id"] for h in hits)
        transcript.append({"query": query, "hits": hits})

    answer = force_answer_fn(task, transcript)
    return {"answer": answer, "tool_calls": tool_calls, "retrieved_ids": retrieved_ids}


def judge_task_success(answer, task_ex, model=JUDGE_MODEL):
    """LLM-as-judge: 답변이 task_ex["must_contain"]의 포인트를 (여러 gold_entry_ids
    출처를 합쳐) 전부 커버하면 1, 아니면 0. judge_answer와 동일한 1회 호출 패턴."""
    prompt = (
        "You are grading a candidate answer for a multi-hop knowledge-base "
        "task that requires combining information from more than one source "
        "entry. Judge only factual correctness and semantic coverage of the "
        "required points below -- ignore style or length.\n\n"
        f"Task: {task_ex['task']}\n"
        f"Required points (judge semantically, not verbatim): {task_ex['must_contain']}\n"
        f"Candidate answer: {answer}\n\n"
        "Does the candidate answer correctly cover ALL of the required points? "
        "Reply with exactly one word: \"yes\" or \"no\"."
    )
    resp = _anthropic_client().messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return 1 if text.strip().lower().startswith("yes") else 0


def run_agentic_eval(
    tasks, *, search_fn=None, decide_fn=None, force_answer_fn=None,
    judge_fn=judge_task_success, max_turns=4, k=5,
):
    """tasks 전체에 대해 run_agent_task + judge_fn을 돌려 집계 지표를 낸다.

    multihop_recall: 모든 태스크에서 실제로 찾아낸 gold_entry_ids 비율
    (분모는 전체 태스크의 gold_entry_ids 총합, 분자는 그중 retrieved_ids에
    실제로 들어간 것)."""
    per_task = []
    success_count = 0
    total_tool_calls = 0
    gold_total = 0
    gold_hit = 0

    for ex in tasks:
        result = run_agent_task(
            ex["task"], search_fn=search_fn, decide_fn=decide_fn,
            force_answer_fn=force_answer_fn, max_turns=max_turns, k=k,
        )
        success = judge_fn(result["answer"], ex)
        success_count += success
        total_tool_calls += result["tool_calls"]

        gold_ids = set(ex["gold_entry_ids"])
        hit_ids = gold_ids & result["retrieved_ids"]
        gold_total += len(gold_ids)
        gold_hit += len(hit_ids)

        per_task.append({
            "task": ex["task"],
            "answer": result["answer"],
            "tool_calls": result["tool_calls"],
            "retrieved_ids": sorted(result["retrieved_ids"]),
            "gold_entry_ids": sorted(gold_ids),
            "success": success,
        })

    n = len(tasks)
    return {
        "task_success_rate": success_count / n if n else 0.0,
        "avg_tool_calls": total_tool_calls / n if n else 0.0,
        "multihop_recall": gold_hit / gold_total if gold_total else 0.0,
        "per_task": per_task,
    }


def main():
    parser = argparse.ArgumentParser(description="wiki-agent 에이전틱 태스크 평가")
    parser.add_argument("--max-turns", type=int, default=4)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--gold", default=str(GOLD_PATH))
    args = parser.parse_args()

    wiki_store.init_db(seed=True)
    tasks = load_agentic_gold(args.gold)
    result = run_agentic_eval(tasks, max_turns=args.max_turns, k=args.k)

    print(f"agentic gold set: {len(tasks)} multi-hop tasks, max_turns={args.max_turns}")
    print(f"  task_success_rate: {result['task_success_rate']:.3f}")
    print(f"  avg_tool_calls:    {result['avg_tool_calls']:.3f}")
    print(f"  multihop_recall:   {result['multihop_recall']:.3f}")
    print()
    for t in result["per_task"]:
        mark = "OK" if t["success"] else "FAIL"
        print(f"  [{mark}] ({t['tool_calls']} calls) {t['task']}")
        print(f"        gold={t['gold_entry_ids']} retrieved={t['retrieved_ids']}")


if __name__ == "__main__":
    main()
