# Rate Limiting Algorithms

## When Should You Use Token Bucket Over Leaky Bucket?

Token bucket allows requests to consume variable tokens and permits bursting up to the bucket size, making it suitable when traffic naturally has uneven demand (e.g., batch API calls, variable-sized uploads). Leaky bucket enforces constant outflow rate regardless of input, better for preventing queue overflow in downstream systems. Choose token bucket if you want to reward efficient clients or handle occasional spikes gracefully; choose leaky bucket if you need predictable, steady consumption or face strict capacity constraints. The tradeoff: token bucket risks more peak load, while leaky bucket may reject legitimate traffic during genuine spikes.

## How Do You Configure Token Bucket Refill Rate?

Set the refill rate (tokens per second) to match your target sustainable throughput, then size the bucket capacity to permit acceptable burst windows. For example, if you want 100 req/s sustained but allow 5-second bursts, configure rate=100 and capacity=500. Refill should align with your downstream system's recovery speed or SLA, not arbitrary limits. Common mistake: setting refill too high to "be generous," which defeats rate limiting; instead, base it on infrastructure throughput and use bucket size for burst allowance. Document both values explicitly since teams often confuse sustained rate with burst capacity.

## What's the Difference Between Fixed Window and Sliding Window Counters?

Fixed window divides time into buckets (e.g., per-second) and counts requests per bucket, resetting at boundaries; it's simple and cheap but allows request spikes at window edges. Sliding window counter tracks counts over the past N seconds continuously, smoothing edge effects and providing more accurate enforcement. Use fixed window for simple, distributed systems where the edge case (two requests at a boundary) is acceptable; use sliding window when strict rate enforcement matters. Pitfall: fixed window's "boundary spike" can let 2× traffic through in milliseconds if requests cluster at the transition; sliding window adds memory per client.

## How Do You Handle Distributed Rate Limiting Without a Central Coordinator?

Distribute the limit across nodes (e.g., 1000 req/s global becomes 100 req/s per 10-node cluster) or use approximate counters (e.g., each node tracks locally and syncs periodically). Local tracking is simple but risks overshooting if requests aren't evenly distributed. Syncing introduces latency and complexity but catches violations faster. For non-critical limits, accept 5–10% overage and rely on local tracking; for strict limits, sync counters every few seconds. Watch the clock skew problem: if nodes have drifting time, window boundaries may not align, leading to unexpected bursts.

## When Should You Use Sliding Window Log Instead of Counter Variants?

Sliding window log records individual request timestamps in a rolling window, providing exact rate limiting without approximation; use it for strict SLA enforcement or when you need audit trails. However, it consumes memory proportional to request volume—storing every timestamp of a high-volume user is expensive. Reserve this for per-tenant limits on smaller audiences or for analytics-heavy systems where you need the detailed log anyway. Pitfall: storing logs in memory across distributed nodes leads to memory bloat and sync complexity; consider this approach only when the request population or window is naturally limited.

## How Do You Size the Token Bucket Capacity Relative to Refill Rate?

Capacity should reflect the maximum acceptable burst duration: capacity ÷ refill_rate = burst duration in seconds. For example, capacity=1000, refill_rate=100 allows a 10-second burst above the steady rate. Set capacity based on downstream tolerance: if your database can handle 3× traffic for 2 seconds, set capacity to 3× the refill rate × 2. Avoid tiny capacities (capacity < refill_rate) because they don't permit any real burst; avoid huge capacities that concentrate all your limit into one large spike. Use empirical testing: apply a burst load and measure downstream system response to calibrate.

## What's the Performance Impact of Per-Tenant Rate Limiting at Scale?

Per-tenant limits require tracking state per customer (separate counters, token buckets, or logs), which scales linearly with tenant count. In-memory tracking works for hundreds of tenants; for thousands, use local caching with lazy cleanup or external stores (Redis). Each lookup and update adds latency, typically sub-millisecond with good indexing but measurable under high concurrency. Pitfall: evicting inactive tenants is easy to forget, leading to unbounded memory growth; implement TTL-based cleanup or LRU. Consider whether you truly need per-tenant isolation or can use coarser-grained grouping (e.g., per region or tier).

## How Do You Choose Between In-Process and Distributed Rate Limiting?

In-process (local memory) is fastest and requires no coordination but only works if a single process handles all traffic for a user or entity. Distributed rate limiting (shared state in Redis or similar) works across load-balanced nodes but adds latency and complexity. Use in-process if traffic for each entity routes to a single service instance; use distributed if requests can land on any node. Hybrid approach: use in-process with periodic sync to handle clock skew and overshoots. Pitfall: mixing in-process and distributed limits often causes confusion; pick one strategy per dimension (e.g., per-user is distributed, per-IP is in-process).

## When Should You Use Leaky Bucket for Queue Depth Management?

Leaky bucket's constant outflow is ideal for controlling queue depth in worker systems: you drain the queue at a fixed rate regardless of inbound spike. This prevents queues from growing unbounded and keeps latency predictable. Use it upstream of expensive operations (database writes, external API calls) where you want to meter consumption. Set the leak rate to match your worker capacity, not your desired throughput. Pitfall: leaky bucket rejects new requests once full, which may be too harsh; consider hybrid approaches where overflow goes to a secondary queue or lower-priority tier.

## How Do You Handle Clock Skew in Fixed Window Rate Limiting?

Fixed window boundaries depend on wall-clock time; if client and server clocks drift, they may disagree on which window is active. In distributed systems, this is common. Mitigation: use synchronized time (NTP) and allow a small grace window (e.g., requests up to 1 second into the next window are accepted). Alternatively, use window identifiers based on logical time rather than wall-clock (though this requires coordination). Pitfall: ignoring clock skew leads to intermittent rate limit violations that are hard to debug. For strict limits, test your system under intentional time drift.

## What's the Memory Overhead of Sliding Window Log Compared to Token Bucket?

Token bucket stores two numbers (tokens and last_refill_time) per entity, constant memory. Sliding window log stores a timestamp per request within the window—if the window is 60 seconds and an entity hits 1000 req/s, that's ~60,000 timestamps in memory. For high-volume users, this becomes prohibitive. Use sliding window log only when the window is short (< 10 seconds) or request rate is naturally low. Hybrid: store only samples (e.g., every 10th request) to reduce memory, accepting slight inaccuracy. For most APIs, token bucket or sliding window counter is more practical.

## How Do You Implement Distributed Rate Limiting with Redis?

Use Redis `INCRBY` to increment a counter and `EXPIRE` to reset it per window, or use Lua scripts for atomic check-and-decrement of token bucket state. Redis gives you sub-millisecond latency and handles concurrency automatically via single-threaded semantics. Configure key naming to isolate tenants (e.g., `rate_limit:user:123:window`) and set TTL to auto-cleanup. Watch Redis memory and latency under load; a single Redis instance becomes a bottleneck at ~10k req/s. Pitfall: not setting TTL on keys leads to unbounded memory; use Redis Cluster or sharding for higher throughput, accepting slightly weaker consistency.

## When Should You Use Fixed Window Counter for Simplicity vs. Accuracy?

Fixed window counter is the simplest to implement and debug: increment a counter each request, reset every second (or hour). Use it when accuracy within ±a few percent is acceptable and you want minimal code. It's common for quota limits (e.g., "1000 API calls per day per user"). The downside is the edge case: requests clustering at a window boundary can double throughput momentarily. If your application can tolerate occasional spikes, or downstream systems have headroom, fixed window is pragmatic. Only upgrade to sliding window if you observe real problems from boundary spikes, not preemptively.

## How Do You Prevent Rate Limit Bypass Through Distributed Requests?

If a client can split requests across multiple identifiers (e.g., using different IP addresses or API keys), they can bypass per-identifier limits. Mitigation: use aggregate limits (e.g., per-account, per-organization) in addition to per-identifier limits, so bypass requires many more identifiers. Monitor for suspicious patterns (many different IPs from one account) and apply secondary limits. Pitfall: over-aggressive aggregate limits may punish legitimate multi-user teams. Use machine learning or anomaly detection for sophisticated bypass attempts, but start with simple rules. Document limit logic so clients understand the intent.

## What's the Right Granularity for Rate Limit Windows (Second vs. Minute vs. Hour)?

Shorter windows (seconds) catch abusive traffic faster but require more frequent resets and state updates. Longer windows (hours, days) are cheaper operationally but are lenient—a spike in the first minute wastes the entire day's quota. Choose based on your threat model: use seconds for abuse detection (bots), minutes for fairness (prevent one user from consuming all capacity), hours or days for quota (business limit). Combine multiple windows (e.g., 100 req/s and 10k req/day) for layered protection. Pitfall: misaligned windows (e.g., per-second and per-minute don't divide evenly) can create confusing edge cases.

## How Do You Configure Burst Allowance Without Over-Provisioning?

Burst capacity should reflect legitimate traffic patterns, not "generous" headroom. Measure your actual traffic: calculate the 99th percentile sustained rate and the 99th percentile peak-to-average ratio. Set refill rate to the sustainable rate and capacity to refill_rate × (peak_ratio − 1). For example, if sustained is 100 req/s and peaks hit 150 req/s, set capacity to 100 × 0.5 = 50 tokens. Test under real load. Pitfall: configuring by guess rather than measurement; burst allowances end up either useless (too small) or dangerous (too large). Use A/B testing if you change settings.

## When Should You Implement Hierarchical Rate Limiting?

Hierarchical limits apply across multiple dimensions: per-endpoint, per-user, per-API-key, per-account, global. Use this when different layers need different protection: prevent one user from monopolizing a high-cost endpoint, prevent one account from overwhelming the platform, etc. Each layer is independent: hitting a per-endpoint limit doesn't affect other endpoints, and hitting a per-user limit doesn't affect other users. Pitfall: confusion about which limit is active; be explicit in error responses ("Rate limit exceeded: per-endpoint quota"). Overhead grows linearly with layers, so limit to 3–4 levels.

## How Do You Handle Rate Limit Carryover or Quota Refunds?

Token bucket naturally carries forward unused tokens (up to capacity), letting efficient clients "save up" for future bursts. Fixed window resets completely, discarding unused quota. Decide based on fairness goals: carryover rewards consistent, efficient users but may seem unfair to bursty users; reset is fair and simple but throws away unused capacity. Some systems allow explicit refunds (e.g., if a request fails partway through, refund tokens), but this adds complexity and audit burden. Document the policy clearly; users should know whether unused quota carries over.

## What Monitoring and Alerting Should You Set Up for Rate Limits?

Track rejection rate (percentage of requests denied), throttled traffic volume, and distribution across tenants or endpoints. Alert if rejection rate spikes (sign of attack or misconfiguration), or if one tenant consistently hits limits (sign of legitimate growth or misbehavior). Log rate limit hits with context (which limit, which entity, reason) to support debugging. Pitfall: alerting on every rejection (high volume) or never looking at logs (missing patterns). Use a tiered approach: dashboard for trends, alerts for anomalies, logs for investigation. Monitor your rate limiter itself—high latency or errors in the limiter bypass the limit entirely.

## How Do You Test Rate Limiting Under Load?

Use load testing tools (wrk, Apache Bench, custom scripts) to generate sustained and burst traffic, then verify that rejection patterns match your configuration. Test at 50%, 100%, and 150% of target rate to see graceful degradation. Inject clock skew (simulate NTP drift) and network partition scenarios (Redis outage) to catch hidden bugs. For distributed setups, test with uneven traffic distribution to confirm load balancing doesn't create local hot spots. Pitfall: testing only happy-path limits; test edge cases like bucket running empty, window boundary transitions, and very short windows. Include latency measurement—rate limiting shouldn't introduce user-facing delays.

## When Should You Use Adaptive or Dynamic Rate Limiting?

Adaptive rate limiting adjusts limits based on system load, time of day, or anomaly detection rather than fixed thresholds. Increase limits during off-peak hours or reduce during incidents. Use this when traffic is highly variable or you want to maximize utilization without SLA violations. Requires sophisticated monitoring and control logic; start with simple rules (e.g., reduce limits to 50% if CPU > 90%) before building machine learning models. Pitfall: unexpected behavior from dynamic changes; always log adjustments and provide manual override. Testing is harder because limits are non-deterministic.
