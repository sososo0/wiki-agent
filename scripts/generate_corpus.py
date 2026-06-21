"""
wiki-agent / scripts / generate_corpus.py

위키 KB가 답할 수 있는 질문 폭을 넓히기 위해, 기존 5개 시드 주제(retry,
rate limiting, circuit breaker, connection pooling, idempotency)와 같은 결의
"분산 시스템 / 백엔드 신뢰성" 도메인을 카테고리 단위로 확장한 합성 소스
마크다운을 생성한다.

이 스크립트는 ingest_doc.py가 먹는 *입력*(마크다운 파일)만 만든다 — 파이프라인
(parse/chunk/dedupe/curate/gate/promote)은 전혀 건드리지 않는다. 카테고리 1개당
LLM 호출 1회로 하위주제 15~25개를 한 파일에 모아 생성해 호출 수를 최소화한다
(토픽마다 개별 호출하면 호출 수가 카테고리 수 x 하위주제 수로 폭증).

출력: data/corpus/<NN>_<slug>.md, 각 파일은
  # <카테고리>
  ## <하위주제 제목>
  <100~250단어 본문>
  ## <하위주제 제목>
  ...
형태 — core/pipeline/parse.py의 ATX 헤더 분리 규칙과 그대로 맞물려 하위주제
하나가 chunk 하나(= curate/gate 입력 하나)가 된다.

실행: python scripts/generate_corpus.py [--out data/corpus] [--per-category 20]
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

GEN_MODEL = os.environ.get("WIKI_AGENT_GEN_MODEL", "claude-haiku-4-5")

# (slug, 카테고리명, 생성 가이드) — 기존 5개 시드와 같은 "분산 시스템/백엔드
# 신뢰성 패턴" 도메인을 넓힌 카테고리 타뷸레이션. 사람이 직접 작성/리뷰한
# 리스트라 LLM이 도메인을 임의로 정하지 않는다.
CATEGORIES: List[Tuple[str, str, str]] = [
    ("retry-strategies", "Retry and Backoff Strategies",
     "fixed delay, exponential backoff, jitter (full/equal/decorrelated), retry budgets, idempotency keys for retries, retry storms, client-side vs server-side retries"),
    ("rate-limiting-algorithms", "Rate Limiting Algorithms",
     "token bucket, leaky bucket, fixed window counter, sliding window log, sliding window counter, distributed rate limiting, per-tenant limits"),
    ("circuit-breaker-patterns", "Circuit Breaker Patterns",
     "closed/open/half-open states, failure thresholds, error budgets, bulkheads, fallback strategies, circuit breaker vs timeout"),
    ("connection-pooling", "Connection Pooling",
     "pool sizing, min/max connections, idle timeout, connection validation, leak detection, pooling for HTTP/DB/gRPC clients"),
    ("idempotency", "Idempotency Patterns",
     "idempotency keys, natural idempotency, exactly-once vs at-least-once, dedup tables, idempotent consumers"),
    ("caching-strategies", "Caching Strategies",
     "cache-aside, read-through, write-through, write-behind, cache invalidation, TTL strategies, thundering herd, stampede protection"),
    ("cache-consistency", "Cache Consistency",
     "stale reads, cache invalidation patterns, write invalidation vs TTL expiry, distributed cache coherence, negative caching"),
    ("message-queue-patterns", "Message Queue Patterns",
     "point-to-point vs pub/sub, dead letter queues, message ordering, visibility timeout, poison messages, fan-out"),
    ("event-driven-architecture", "Event-Driven Architecture",
     "event sourcing, CQRS, outbox pattern, event versioning, event replay, choreography vs orchestration"),
    ("database-indexing", "Database Indexing",
     "B-tree vs hash index, composite indexes, covering index, index selectivity, write amplification from indexes"),
    ("database-sharding", "Database Sharding",
     "hash sharding, range sharding, directory-based sharding, resharding, hot shard mitigation, shard key selection"),
    ("database-replication", "Database Replication",
     "leader-follower replication, synchronous vs asynchronous replication, replication lag, multi-leader conflicts, read replicas"),
    ("transaction-isolation", "Transaction Isolation Levels",
     "read uncommitted, read committed, repeatable read, serializable, phantom reads, write skew, MVCC"),
    ("database-migrations", "Database Migration Strategies",
     "online schema migration, backward-compatible migrations, expand-contract pattern, migration rollback, zero-downtime migrations"),
    ("distributed-consensus", "Distributed Consensus",
     "Raft, Paxos basics, leader election, quorum reads/writes, split-brain, fencing tokens"),
    ("distributed-coordination", "Distributed Coordination",
     "distributed locks, leases, ZooKeeper/etcd patterns, coordination service failure modes, lock renewal"),
    ("observability-logging", "Observability: Logging",
     "structured logging, log levels, correlation IDs, log sampling, PII redaction in logs, log retention"),
    ("observability-metrics", "Observability: Metrics",
     "counters/gauges/histograms, SLI/SLO/SLA, percentile latency (p50/p95/p99), cardinality explosion, golden signals"),
    ("observability-tracing", "Observability: Distributed Tracing",
     "trace context propagation, spans, sampling strategies, trace-log correlation, root cause via tracing"),
    ("alerting-practices", "Alerting Practices",
     "alert fatigue, actionable alerts, paging thresholds, alert deduplication, runbook links, escalation policies"),
    ("api-versioning", "API Versioning",
     "URI versioning, header versioning, semantic versioning for APIs, deprecation policies, breaking vs non-breaking changes"),
    ("api-pagination", "API Pagination",
     "offset pagination, cursor-based pagination, keyset pagination, page size limits, consistent pagination under writes"),
    ("api-authentication", "API Authentication",
     "API keys, OAuth2 flows, JWT structure and validation, mutual TLS, session vs token auth"),
    ("authorization-patterns", "Authorization Patterns",
     "RBAC, ABAC, ACLs, policy engines, least privilege, permission caching"),
    ("secrets-management", "Secrets Management",
     "secret rotation, vault-based secret storage, env var risks, encryption at rest for secrets, short-lived credentials"),
    ("input-validation", "Input Validation",
     "allowlisting vs denylisting, schema validation, sanitization vs validation, injection prevention, validation at boundaries"),
    ("testing-unit", "Unit Testing Practices",
     "test isolation, mocking vs fakes, test doubles, flaky test causes, coverage pitfalls"),
    ("testing-integration", "Integration Testing",
     "test containers, contract testing, integration test data setup, testing across service boundaries"),
    ("testing-load", "Load and Performance Testing",
     "load testing vs stress testing, soak tests, ramp-up patterns, identifying bottlenecks under load"),
    ("testing-chaos", "Chaos Engineering",
     "fault injection, game days, blast radius limiting, chaos experiment design, steady-state hypothesis"),
    ("deployment-blue-green", "Blue-Green Deployments",
     "traffic switching, rollback speed, database compatibility across versions, blue-green cost tradeoffs"),
    ("deployment-canary", "Canary Deployments",
     "canary traffic percentage, automated canary analysis, metrics-based promotion, canary rollback triggers"),
    ("feature-flags", "Feature Flags",
     "kill switches, percentage rollouts, flag debt, flag targeting rules, flag-driven incident mitigation"),
    ("rollback-strategies", "Rollback Strategies",
     "fast rollback design, database migration rollback, forward-fix vs rollback decision, rollback automation"),
    ("concurrency-patterns", "Concurrency Patterns",
     "producer-consumer, worker pools, backpressure, async/await pitfalls, race conditions, thread safety patterns"),
    ("async-processing", "Asynchronous Processing",
     "background jobs, task queues, at-least-once processing, job retries, job scheduling, long-running task patterns"),
    ("load-balancing", "Load Balancing",
     "round robin, least connections, consistent hashing, layer 4 vs layer 7 LB, health check based routing"),
    ("service-discovery", "Service Discovery",
     "client-side vs server-side discovery, service registry, DNS-based discovery, discovery failure handling"),
    ("configuration-management", "Configuration Management",
     "config vs secrets, dynamic config reload, config validation, environment-specific config, config drift"),
    ("data-consistency-models", "Data Consistency Models",
     "strong consistency, eventual consistency, causal consistency, read-your-writes, consistency vs availability tradeoffs"),
    ("backup-disaster-recovery", "Backup and Disaster Recovery",
     "RPO/RTO, backup verification, point-in-time recovery, cross-region failover, backup retention policy"),
    ("performance-optimization", "Performance Optimization",
     "profiling before optimizing, N+1 query problem, batching, lazy loading pitfalls, premature optimization"),
    ("container-orchestration", "Container Orchestration",
     "liveness vs readiness probes, resource requests/limits, pod disruption budgets, rolling updates, node affinity"),
    ("networking-fundamentals", "Networking Fundamentals",
     "DNS resolution, TLS handshake, HTTP keep-alive, TCP backlog, connection timeouts vs read timeouts"),
    ("capacity-planning", "Capacity Planning",
     "headroom planning, traffic forecasting, autoscaling triggers, scaling lead time, cost-aware capacity planning"),
    ("incident-response", "Incident Response",
     "incident severity levels, incident commander role, communication during incidents, mitigation vs root cause"),
    ("postmortems", "Postmortem Practices",
     "blameless postmortems, timeline reconstruction, action item tracking, postmortem template, contributing factors vs root cause"),
    ("timeout-strategies", "Timeout Strategies",
     "connect vs read vs total timeout, timeout budgets across call chains, hedged requests, timeout tuning pitfalls"),
    ("bulkhead-isolation", "Bulkhead Isolation",
     "thread pool isolation, resource partitioning by tenant, failure containment, isolation cost tradeoffs"),
    ("graceful-degradation", "Graceful Degradation",
     "fallback responses, partial failure handling, feature shedding under load, degraded mode design"),
    ("backpressure", "Backpressure Mechanisms",
     "reactive streams backpressure, queue-based backpressure, load shedding, admission control"),
    ("data-validation-pipelines", "Data Validation in Pipelines",
     "schema evolution, data contracts, validation at ingestion, dead-letter handling for bad data"),
    ("monitoring-dashboards", "Monitoring Dashboard Design",
     "golden signal dashboards, dashboard sprawl, drill-down design, dashboard ownership"),
    ("api-rate-limit-design", "API Rate Limit Design for Consumers",
     "rate limit headers, retry-after semantics, client-side throttling, burst allowances"),
    ("multi-region-architecture", "Multi-Region Architecture",
     "active-active vs active-passive, region failover, data residency, cross-region latency tradeoffs"),
    ("service-mesh", "Service Mesh Patterns",
     "sidecar proxies, mTLS via mesh, traffic shaping, mesh observability, mesh overhead tradeoffs"),
    ("queue-based-load-leveling", "Queue-Based Load Leveling",
     "smoothing traffic spikes via queues, queue depth monitoring, consumer scaling, queue-based decoupling"),
    ("schema-evolution", "Schema Evolution",
     "backward/forward compatible schema changes, protobuf/avro evolution rules, versioned schemas"),
    ("dependency-management", "Dependency Management",
     "vendoring, lockfiles, dependency pinning risks, transitive dependency conflicts, upgrade strategy"),
    ("cost-optimization", "Cloud Cost Optimization",
     "rightsizing, spot/preemptible instances, storage tiering, cost attribution, idle resource cleanup"),
]


def _client():
    import anthropic
    return anthropic.Anthropic()


def build_prompt(category_name: str, guide: str, n: int) -> str:
    return (
        f"You are writing reference material for an internal engineering wiki "
        f"about distributed systems and backend reliability. Write {n} distinct "
        f"subtopics under the category \"{category_name}\".\n\n"
        f"Topics to draw subtopics from (cover a good spread, don't repeat): {guide}\n\n"
        "Format strictly as markdown:\n"
        f"# {category_name}\n\n"
        "## <subtopic title>\n"
        "<100-250 word factual, verifiable explanation written like an engineering "
        "reference doc (definition, when to use it, tradeoffs, a concrete example). "
        "No hallucinated statistics or fake case studies. No marketing language.>\n\n"
        "(repeat ## sections for each subtopic)\n\n"
        "Output ONLY the markdown, no preamble or commentary."
    )


def build_basics_prompt(category_name: str, guide: str, n: int) -> str:
    """심화 레퍼런스체 대신 초급/중급 학습자용 짧은 정의문 생성(build_prompt와
    별도 프롬프트 — 기존 advanced 출력에 영향 없음)."""
    return (
        f"You are writing beginner-to-intermediate reference material for an "
        f"internal engineering wiki about distributed systems and backend "
        f"reliability. The reader is a junior engineer encountering this topic "
        f"for the first time. Write {n} distinct foundational subtopics under "
        f"the category \"{category_name}\".\n\n"
        f"Topics to draw subtopics from (cover a good spread, don't repeat): {guide}\n\n"
        "Format strictly as markdown:\n"
        f"# {category_name}\n\n"
        "## <subtopic title, phrased as a plain question a beginner would ask, "
        "e.g. \"What Is X?\" or \"Why Do We Need X?\">\n"
        "<80-150 word plain-language explanation in this order: (1) a one-sentence "
        "definition with no jargon, (2) why it matters / what problem it solves, "
        "(3) one simple concrete example or analogy. Do NOT include edge cases, "
        "tradeoff tables, or advanced variants — that belongs in a different doc. "
        "No hallucinated statistics or fake case studies. No marketing language.>\n\n"
        "(repeat ## sections for each subtopic)\n\n"
        "Output ONLY the markdown, no preamble or commentary."
    )


def build_intermediate_prompt(category_name: str, guide: str, n: int) -> str:
    """basics(기초 정의)와 advanced(심화 레퍼런스) 사이의 중간 난이도. 독자는 해당
    개념을 한 번쯔음 써본 엔지니어로, 정의는 이미 알고 실무에서 어떻게
    선택/구성하는지를 궁금해한다(build_basics_prompt/build_prompt와 별도 프롬프트
    — 기존 두 출력에 영향 없음)."""
    return (
        f"You are writing intermediate-level reference material for an internal "
        f"engineering wiki about distributed systems and backend reliability. The "
        f"reader already knows the basic definition and has used this kind of "
        f"pattern before, but wants practical guidance on choosing between "
        f"variants and configuring them correctly. Write {n} distinct intermediate "
        f"subtopics under the category \"{category_name}\".\n\n"
        f"Topics to draw subtopics from (cover a good spread, don't repeat): {guide}\n\n"
        "Format strictly as markdown:\n"
        f"# {category_name}\n\n"
        "## <subtopic title, phrased as a practical how-to/when-to question, "
        "e.g. \"When Should You Use X Over Y?\" or \"How Do You Configure X?\">\n"
        "<100-180 word explanation that assumes basic familiarity and focuses on "
        "practical decision-making: a brief reminder of the mechanism (1 sentence), "
        "concrete guidance on when/how to apply it, and one common pitfall or "
        "tradeoff to watch for. No deep internals or exhaustive edge-case analysis "
        "(that belongs in advanced material) and no beginner-level basics "
        "explanations. No hallucinated statistics or fake case studies. No "
        "marketing language.>\n\n"
        "(repeat ## sections for each subtopic)\n\n"
        "Output ONLY the markdown, no preamble or commentary."
    )


_PROMPT_BUILDERS = {
    "basics": build_basics_prompt,
    "intermediate": build_intermediate_prompt,
    "advanced": build_prompt,
}


def generate_category(slug: str, category_name: str, guide: str, n: int, model: str,
                       difficulty: str = "advanced") -> str:
    prompt = _PROMPT_BUILDERS[difficulty](category_name, guide, n)
    resp = _client().messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return text.strip()


def main():
    parser = argparse.ArgumentParser(description="합성 위키 소스 코퍼스 생성")
    parser.add_argument("--out", default=str(ROOT / "data" / "corpus"))
    parser.add_argument("--per-category", type=int, default=20)
    parser.add_argument("--model", default=GEN_MODEL)
    parser.add_argument("--only", nargs="*", default=None,
                         help="지정 시 해당 slug만 생성(재시도/보충용)")
    parser.add_argument("--difficulty", choices=["advanced", "basics", "intermediate"],
                         default="advanced",
                         help="basics: 초급 학습자용 짧은 정의문. intermediate: 기본 정의는 "
                              "안다고 가정하고 실무 선택/구성 가이드. advanced: 심화 레퍼런스. "
                              "basics/intermediate는 파일명에 각각 basics_/intermediate_ "
                              "접두사가 붙어 advanced 출력과 분리됨")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    categories = CATEGORIES
    if args.only:
        only = set(args.only)
        categories = [c for c in CATEGORIES if c[0] in only]

    prefix = f"{args.difficulty}_" if args.difficulty in ("basics", "intermediate") else ""
    for i, (slug, name, guide) in enumerate(categories, start=1):
        out_path = out_dir / f"{prefix}{i:02d}_{slug}.md"
        if out_path.exists():
            print(f"[{i:02d}] skip (exists): {out_path.name}")
            continue
        print(f"[{i:02d}] generating: {name} ...")
        try:
            md = generate_category(slug, name, guide, args.per_category, args.model,
                                    difficulty=args.difficulty)
        except Exception as e:
            print(f"  FAILED: {e}")
            continue
        if not md.startswith("#"):
            md = f"# {name}\n\n{md}"
        out_path.write_text(md + "\n", encoding="utf-8")
        n_sections = len(re.findall(r"^## ", md, flags=re.MULTILINE))
        print(f"  wrote {out_path.name} ({n_sections} subtopics, {len(md)} chars)")
        time.sleep(0.3)

    print("done.")


if __name__ == "__main__":
    main()
