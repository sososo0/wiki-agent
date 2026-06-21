# Timeout Strategies

## When Should You Separate Connect Timeouts From Read Timeouts?

Connect timeout controls how long to wait for a TCP handshake; read timeout controls how long to wait for a response after connection succeeds. Separate them when your network characteristics and processing latencies differ—typically connect should be tight (100–500ms) to fail fast on unreachable hosts, while read can be longer if backend processing is genuinely variable. Set them together only when you cannot distinguish network health from service health. A common pitfall: setting read timeout too short to accommodate legitimate P99 latencies, which causes cascading failures under load when responses slow but are not actually hung.

## How Do You Set Total Timeout vs. Individual Phase Timeouts?

Total timeout is a ceiling across all phases (connect + request send + read); individual phase timeouts cap specific stages. Use total timeout as a safety net to prevent a single slow phase from consuming all your deadline budget, especially in chains where you call multiple downstreams. Set individual timeouts slightly lower than total so that intermediate phases fail cleanly. Watch for: if total timeout is poorly chosen relative to individual timeouts, you may hit the total deadline while still holding resources, making debugging harder than hitting a specific phase timeout.

## When Should You Implement Timeout Budgets Across Microservice Call Chains?

A timeout budget allocates your total end-to-end deadline across multiple hops. When service A calls B calls C, subtract expected latency plus overhead for each hop so that C has enough time to complete and return. Start by measuring P95 latencies for each service and add 10–20% headroom; decrement the budget as you traverse the chain. Pitfall: not accounting for retries or queuing delays—if each hop has a 500ms timeout and you retry twice, you may exceed your overall SLA even though individual timeouts are reasonable.

## How Do You Choose Between Hard Deadlines and Soft Timeouts?

Hard deadlines abort work immediately when exceeded; soft timeouts allow graceful degradation (e.g., returning cached data). Hard deadlines are appropriate for user-facing requests where latency SLAs are strict and partial results are worse than timeouts. Soft timeouts suit background jobs and internal APIs where best-effort results with additional latency are acceptable. The tradeoff: hard deadlines are simpler to reason about but can trigger unnecessary failures if you are slightly pessimistic; soft timeouts require careful state management to avoid wasted work. Choose based on whether missing the deadline is a contract violation or a preference.

## Should You Use Different Timeouts for Different Request Paths?

Yes—different operations have different latency profiles. Write operations often need longer timeouts than reads due to durability writes or distributed consensus; batch operations need longer than single-record fetches. Configure timeouts per endpoint or operation type rather than globally. A pitfall: copy-pasting a global timeout across heterogeneous operations, which causes fast operations to time out under load or slow operations to fail unnecessarily. Measure your actual P99 latencies per operation and size timeouts accordingly.

## When Is Hedged Requests the Right Strategy?

Hedged requests send a duplicate request to an alternative replica or instance if the primary request hasn't completed by a threshold (typically P50–P75 latency). Use hedging when you have spare capacity, multiple replicas, and tail latencies are driven by occasional slow instances rather than systematic overload. Hedging can reduce P99 latency significantly. Pitfall: hedging amplifies load—if hedging threshold is too aggressive or capacity is tight, you double request volume and worsen the problem. Also, ensure downstream operations are idempotent; hedging on non-idempotent operations causes duplicate writes.

## How Do You Set Hedging Thresholds Correctly?

The hedging threshold should be set somewhere between P50 and P75 of normal latency for the operation—typically around P50–P66. If you hedge too early (e.g., at P25), you send many redundant requests under normal conditions and waste capacity. If you hedge too late (e.g., at P95), you only catch extreme outliers and gain little tail-latency improvement. Monitor the hedge success rate (requests where the secondary completes faster than primary) and adjust threshold upward if success rate is low, downward if you are hedging too often.

## Should Hedging Requests Use the Same Timeout as Primary Requests?

Yes, hedging requests should use the same timeout as the primary request. The hedge is an alternative path to the same data; it should respect the same deadline. However, the hedge may hit the timeout sooner if issued partway through the primary's wait window. For example, if primary times out at 500ms and you hedge at 300ms, the hedge has 200ms remaining. Pitfall: setting a longer timeout on hedges hoping to improve success rates—this defeats the purpose of hedging (getting a fast answer) and still consumes resources.

## How Do You Tune Timeouts When Response Latency is Multimodal?

Multimodal latency (e.g., cache hits at 10ms, cache misses at 500ms) makes a single timeout feel wrong for one of the modes. Rather than averaging, set the timeout to accommodate the slower mode and use hedging or caching to mitigate the fast mode. Alternatively, measure what fraction of requests fall into each mode and accept that the slower mode may rarely timeout. Pitfall: setting timeout to the mean or median, which times out too many slow-but-legitimate requests. Understand whether modes are acceptable behavior (different code paths) or signs of problems (connection pooling exhaustion).

## When Should You Retry After a Timeout?

Retry after timeout only if the operation is idempotent and you still have budget (time and retry count). For non-idempotent operations (writes, transfers), retrying risks duplication; prefer failing fast and letting the client decide. For idempotent operations, retrying can mask transient latency spikes. Set a tight retry budget (e.g., one retry) and decrement your timeout budget for subsequent attempts. Pitfall: unlimited retries on every timeout, which converts a single slow request into many slow requests and amplifies load during outages.

## How Do You Handle Timeout Interactions With Connection Pooling?

Connection pooling lets you reuse TCP connections, which reduces connect-timeout overhead but introduces queue wait. If a connection is slow (e.g., handling a previous slow request), new requests wait in the pool queue, consuming time before they hit the read timeout. Set pool timeouts (queue wait) separately from request timeouts and ensure queue wait is accounted for in your overall deadline. Pitfall: ignoring queue delays and setting request timeouts as though requests execute immediately—under load, queue wait can exceed your timeout budget.

## Should Timeout Values Be Percentile-Based or Fixed?

Use percentile-based timeouts (e.g., set timeout to P99 of observed latency + 20% headroom) as a starting point, then adjust periodically. Fixed timeouts are simpler operationally but require discipline to re-tune as SLIs change. Percentile-based timeouts naturally adapt if your service gets faster or slower. Pitfall: setting timeout to a published SLI percentile without extra headroom—if SLI is P99 and you timeout at P99, you will timeout about 1% of requests due to natural variance. Build in 10–30% headroom above the percentile.

## When Should You Implement Adaptive Timeouts?

Adaptive timeouts adjust based on recent latency observations—if the service is slow, increase timeout temporarily to avoid cascading failures; if it is fast, decrease to reduce resource holding. Use adaptive timeouts for internal services under your control where you can measure latency continuously and tolerate complexity. Avoid adaptive timeouts for external APIs where you cannot observe latency reliably. Pitfall: adaptive timeout logic that is too reactive, oscillating between too-short and too-long values. Use moving averages or percentiles rather than instantaneous latency.

## How Do You Avoid Timeout Cascades in Synchronous Call Chains?

A timeout cascade occurs when timeouts at one layer cause timeouts at the layer above. Prevent this by ensuring upstream timeout is longer than downstream timeout plus expected processing time. If service A calls B calls C, set timeout(A) > timeout(B) + timeout(C) + processing overhead. Also ensure that when a downstream timeout occurs, you fail fast upstream rather than waiting for the full upstream timeout. Use structured deadline propagation (e.g., gRPC deadlines, context timeouts) to enforce this automatically.

## Should You Use Timeouts or Deadlines for Deadline-Driven Systems?

Deadlines specify an absolute time when a result is no longer useful; timeouts specify a duration. Deadlines are more flexible because they propagate through call chains without recalculation (deadline − now = remaining time). Timeouts are simpler if you do not control all layers. Use deadlines if your system uses a deadline-propagation framework (gRPC context, OpenTelemetry baggage); use timeouts otherwise. Pitfall: mixing absolute deadlines and relative timeouts in the same chain—conversion between them is error-prone, especially across time zones or clock skew.

## When Should You Set Different Timeouts for Reads vs. Writes?

Writes often need longer timeouts than reads because they involve durability guarantees (replication, WAL syncs) or distributed consensus. A typical pattern: reads timeout at 500ms, writes at 2–5 seconds, depending on your infrastructure. Reads are also often retryable; writes less so. Measure your P99 latencies separately for each operation type and size accordingly. Pitfall: applying a single timeout to a service that does both reads and writes, causing writes to timeout unnecessarily or reads to hold resources too long.

## How Do You Timeout Batch Operations or Bulk Requests?

Batch operations have higher latency than single-record operations due to volume and sequential processing. Rather than multiplying single-record timeout by batch size (which is rarely accurate), measure batch latency directly at your expected batch sizes. Offer configurable batch sizes with corresponding SLAs. A pitfall: setting batch timeout naively (e.g., single_record_timeout × count) causes either frequent timeouts or excessive resource holding for small batches. Also consider partial batch returns if timeout approaches mid-execution.

## Should Timeout Errors Be Retried Differently Than Other Errors?

Yes. Timeout errors often indicate transient load or latency spikes and are good candidates for immediate retry (with backoff). Other errors (4xx, logic errors) should not be retried or should be retried differently. However, distinguish between client-side timeout (network or endpoint unreachable) and server-side timeout (processing took too long)—only the latter is safe to retry unconditionally. Pitfall: treating all timeouts the same and retrying indiscriminately, which amplifies load during actual outages instead of recovering from transient hiccups.

## How Do You Measure Whether Your Timeouts Are Correctly Calibrated?

Monitor four metrics: timeout error rate (should be < 1–2% for user-facing services), P99 latency vs. timeout (should have headroom), timeout vs. actual failure rate (timeouts should correlate with real problems, not random noise), and resource utilization at timeout (if timeout is hit, are resources being held long?). If timeout error rate is high, your timeout is too aggressive or your service is unhealthy. If timeout is rarely approached, it may be too generous. Adjust periodically as traffic patterns and infrastructure change.

## When Should You Use Timeout With Circuit Breakers?

Timeouts are fast-fail mechanisms for individual requests; circuit breakers stop sending requests entirely when a service is clearly unhealthy. Use both: timeouts to detect slow or hung requests, circuit breakers to detect patterns of failures and protect the upstream service from wasted retries. A timeout indicates one request is slow; a circuit breaker indicates the service is down or severely degraded. Configure circuit breaker to trip after a threshold of timeout errors (e.g., 5 timeouts in 10 seconds), then enter a fallback mode. Pitfall: circuit breaker threshold too sensitive, flipping on transient timeouts, or too insensitive, not protecting upstream until damage is done.

## How Do You Handle Timeout Configuration Across Multiple Environments?

Timeouts should differ between environments: dev/test can be lenient (e.g., 10 seconds) to avoid false positives when running slow tests, staging should match production closely, and production should be tuned for typical latency. Use environment-based configuration (environment variables, feature flags) rather than hardcoding. Regularly sync production timeout values with staging for pre-deployment validation. Pitfall: different timeouts in staging and production that mask latency issues—a service that barely meets staging timeout may consistently timeout in production. Keep them in sync unless there is a deliberate reason for divergence.

## Should You Implement Timeout Telemetry and Alerting?

Yes. At minimum, alert on timeout error rate exceeding your threshold (e.g., > 2% for 5 minutes), and log timeout events for post-incident analysis. Include context: was timeout a network error, slow processing, or a cascade from downstream? Also track time-to-timeout vs. total latency percentiles—if P99 latency is 80% of timeout, you have little headroom. Pitfall: not alerting on timeouts until they impact customers, or alerting on individual timeout events (too noisy). Focus on trends and rates.
