# Backpressure Mechanisms

## When Should You Use Reactive Streams Backpressure Over Queue-Based Systems?

Reactive streams backpressure (where consumers signal demand upstream) shines when you have low-latency requirements and direct producer-consumer relationships. Use it when your system can afford to wait for explicit pull signals and when you want to avoid intermediate buffering. The main trade-off is implementation complexity: reactive streams require careful handling of cancellation and demand coordination. Queue-based systems are simpler to reason about but can accumulate unbounded buffers if not sized carefully.

## How Do You Configure Queue Depth for Backpressure?

Queue depth is the maximum number of pending items before a producer stops accepting work. Set it based on your memory budget, latency tolerance, and burst patterns. A queue too small rejects legitimate traffic; too large masks problems and increases latency under load. Start with 1–2× your expected per-second throughput and adjust downward if latency becomes unacceptable under sustained load. Monitor queue utilization; consistently full queues signal that downstream processing can't keep pace and you need to shed load elsewhere.

## Should You Implement Backpressure at the Transport Layer or Application Layer?

Transport-layer backpressure (TCP flow control, gRPC window sizing) is automatic but coarse-grained and can hide application-level bottlenecks. Application-layer backpressure (explicit signaling, queue management) gives you visibility and control over business logic. Most production systems use both: rely on transport-layer backpressure as a safety net, but implement application-level checks at service boundaries to fail fast and avoid cascading delays. The pitfall is treating transport backpressure as sufficient; it doesn't know about your application's actual capacity.

## How Do You Choose Between Bounded and Unbounded Queues?

Bounded queues (fixed maximum size) fail fast and prevent memory exhaustion but risk rejecting valid requests. Unbounded queues are more forgiving under bursts but can consume all available memory during sustained overload. Use bounded queues at system boundaries (API ingestion, load balancer) and unbounded queues for internal stages where you have good observability and can scale horizontally. Always pair unbounded queues with alerting; they mask capacity problems until they cause outages.

## What's the Difference Between Hard and Soft Backpressure Limits?

Hard limits (queuing stops immediately when capacity is reached) provide strict guarantees but cause sharp rejections. Soft limits (gradual degradation, probabilistic rejection) smooth out traffic spikes but are harder to reason about. Use hard limits for resource protection (memory, connections) and soft limits (token bucket, p-value based admission) for fairness between tenants or priority levels. The risk with soft limits is that clients don't get clear feedback and may retry aggressively, amplifying the problem.

## How Do You Implement Backpressure in Asynchronous, Fire-and-Forget Systems?

True backpressure requires a return path for feedback, which conflicts with fire-and-forget semantics. Options include: async acknowledgment channels (producer waits for explicit ack before sending next batch), metrics-driven throttling (producer observes downstream latency and self-regulates), or explicit polling (producer periodically checks queue depth). Each has latency or consistency trade-offs. Fire-and-forget systems often need external load shedding (circuit breakers, admission control) because the producer can't self-regulate. Document your choice explicitly; ambiguity here causes silent data loss.

## When Should You Combine Backpressure with Load Shedding?

Backpressure alone may not be sufficient if you can't reduce the incoming load (e.g., external API clients don't respect slow responses). Load shedding (rejecting low-priority requests) handles this by making room for high-priority work. Use backpressure first to let fast consumers keep up without queuing; add load shedding if backpressure causes unacceptable latency for critical paths. Common pitfall: shedding load too aggressively and wasting capacity, or shedding too late after the queue already grew.

## How Do You Handle Backpressure with Fanout (One Producer, Many Consumers)?

Fanout creates a coordination problem: the producer must wait for all consumers to be ready. Options include: blocking until all consumers have capacity (slowest consumer throttles all), independent queues per consumer (memory overhead, no fairness guarantees), or hybrid (per-consumer soft limits with periodic load shedding). Use independent queues if consumers have different processing rates; use coordinated backpressure only if all consumers matter equally and you can afford latency spikes. Watch for deadlock if a consumer queue fills up and that consumer is also needed by another producer.

## What's the Right Way to Expose Backpressure Metrics?

Expose queue depth (current and percentiles), queue fill rate, rejection rate, and time spent waiting for backpressure to clear. Avoid exposing raw latency as your primary backpressure signal; instead alert on "queue depth approaching limit" or "backpressure events per minute." This lets you distinguish between normal load and saturation. The trap is logging backpressure events without context; always pair rejection counts with queue depth, consumer throughput, and upstream request rate so you can diagnose whether the consumer is slow or the producer is overloaded.

## How Do You Prevent Thundering Herd When Backpressure Clears?

When a blocked producer suddenly gets capacity (e.g., a consumer recovers), all waiting producers may resume simultaneously, causing a spike that re-triggers backpressure. Solutions include: staggered resumption (wake producers gradually), token bucket filling at a controlled rate, or exponential backoff on retry. For reactive streams, use proper subscription demand signaling rather than resuming everything at once. The risk is that your "fix" for overload becomes a new source of oscillation and instability.

## Should Backpressure Be Synchronous or Asynchronous?

Synchronous backpressure (blocking until space is available) is simpler to reason about but can cause thread starvation. Asynchronous backpressure (producer gets a future/promise for when to retry) avoids blocking but adds complexity and latency. Use synchronous backpressure in small, bounded thread pools where you want strict capacity control; use asynchronous in high-concurrency systems (async runtimes, event loops). Mixing both (e.g., blocking inside an async executor) causes deadlocks and is a common source of subtle bugs.

## How Do You Tune Backpressure for Bursty Traffic Patterns?

Bursty traffic needs larger queue buffers to absorb spikes without rejecting valid requests, but large buffers hide saturation. Use queues sized for 2–3× your peak burst duration, pair with fast metrics (1-5 second windows) to detect sustained overload, and add load shedding rules that trigger if backpressure persists. Avoid relying on backpressure alone; add a circuit breaker or admission control that kicks in if queue depth stays high for more than a few seconds. Test your burst handling before deploying; many outages happen at the first unexpected traffic spike.

## When Is Backpressure Insufficient and You Need Admission Control?

Backpressure only works if the upstream client respects slow responses (sees increased latency and backs off). External clients, batch jobs, or retry storms often don't; they keep hammering your service regardless. Use admission control (token bucket, quota-based limits, priority queues) when backpressure alone doesn't prevent queueing. Admission control rejects or delays requests before they enter your queue, protecting downstream systems. The trade-off is that rejections are visible to clients; be prepared to handle errors gracefully and document your limits.

## How Do You Coordinate Backpressure Across Multiple Service Boundaries?

Each service has its own queue and backpressure logic, but saturation at one service propagates upstream as increased latency. Strategies include: end-to-end timeout enforcement (requests die if they exceed a deadline), cascading backpressure (service B's slow response triggers service A to apply backpressure), or global rate limiting (a central authority allocates capacity across services). Cascading is implicit but can cause latency amplification; global rate limiting is explicit but adds coordination overhead. Use observability (trace latency paths) to detect cross-boundary bottlenecks early.

## What Happens When You Apply Backpressure to a System with Retries?

Retries can amplify backpressure: a slow consumer causes the producer to queue, the client times out and retries, which increases load further. Use exponential backoff on the client side, set client timeouts shorter than your backpressure queue TTL, and implement idempotency so retries don't duplicate work. Better still, use circuit breakers to fail fast rather than queuing during sustained outages. The pitfall is naively combining backpressure with aggressive retries; you'll create a death spiral where backpressure causes more retries, which causes more backpressure.

## How Do You Implement Backpressure-Aware Graceful Shutdown?

During shutdown, you want in-flight requests to finish but new ones to be rejected immediately. Use a shutdown flag that triggers load shedding first (reject new requests), then allow backpressure to clear existing queues (give in-flight requests time to complete). Set a hard timeout (e.g., 30 seconds) to force termination if queues don't drain. Monitor queue depth during shutdown; if it doesn't drop, you have a slow consumer or a deadlock. Common mistake: shutting down the consumer before draining the queue, losing requests.

## Should You Implement Per-Client or Global Backpressure?

Global backpressure applies a single limit across all clients, treating them fairly. Per-client backpressure lets you set different limits by client, priority, or SLA. Use global backpressure when all clients are equal and you want simplicity. Use per-client when you have multi-tenant systems, high-value customers, or critical services that must not be starved by noisy neighbors. Per-client adds observability burden; track queue depth and rejections per client separately and use dashboards to spot fairness issues.

## How Do You Test Backpressure Behavior Under Load?

Use chaos engineering or load testing to deliberately trigger backpressure: slow down the consumer and watch the queue grow, kill a consumer and verify the queue stabilizes at capacity, restart the consumer and watch for thundering herd. Measure latency percentiles (p50, p99) and rejection rates under sustained load. Verify that backpressure doesn't cause timeout cascades or hidden data loss. The trap is testing only happy-path load; realistic tests include network jitter, partial failures, and uneven load distribution across consumers.

## How Do You Balance Backpressure Latency vs. Throughput?

Tight backpressure (small queue, early rejection) minimizes latency but reduces throughput under bursts. Loose backpressure (larger queue, late rejection) improves throughput but increases tail latency. Your choice depends on SLA: strict latency SLAs require tight backpressure; throughput-focused systems can tolerate larger queues. Measure both: track p99 latency and requests-per-second under your expected load profile. Adjust queue depth to hit your SLA while maximizing throughput. Expect trade-offs; you rarely get both.

## What's the Relationship Between Backpressure and Observability?

Backpressure is invisible without good metrics: queue depth, rejection rate, time waiting for backpressure to clear, and consumer processing latency. Instrument your queue to emit these metrics at regular intervals (not just on state changes). Use distributed tracing to see how backpressure propagates across services; look for requests that spent unexpectedly long time in a queue. The risk is treating backpressure as an implementation detail; make it part of your observability story so operators can diagnose "why is latency high?" quickly.

## How Do You Recover from a Backpressure Incident?

After backpressure clears (consumer recovers, load drops), don't assume your system is healthy. Verify that: the queue drained completely (no stuck requests), clients didn't timeout or give up, and performance returned to baseline. Run a quick post-incident check: did load shedding reject legitimate work? Did retries amplify the problem? Did backpressure propagate upstream? Use the incident to tune queue size, add alerts, or improve consumer throughput. Document what triggered the incident (traffic spike, slow query, resource exhaustion) so you can prevent it.

## When Should You Use Load Shedding Instead of Backpressure?

Load shedding (rejecting requests) and backpressure (queuing and signaling upstream) are complementary. Use backpressure when you want to preserve all valid work and can afford some latency. Use load shedding when you need to protect the system and can afford to lose or delay low-priority work. Combine both: apply backpressure to absorb short bursts, then shed load if backpressure persists. The anti-pattern is using only backpressure and hoping the queue never grows too large; that's how you get OOM kills instead of graceful degradation.
