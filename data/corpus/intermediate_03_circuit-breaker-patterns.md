# Circuit Breaker Patterns

## When Should You Use a Circuit Breaker vs. a Simple Timeout?

A circuit breaker tracks repeated failures across requests, while a timeout only limits individual request duration. Use a circuit breaker when you need to stop hammering a degraded downstream service and give it time to recover; use a timeout when you're primarily concerned about unbounded latency in your own request path. Circuit breakers are more efficient under cascading failure because they fail fast after a threshold is reached rather than waiting for each timeout to elapse. The pitfall: relying on a circuit breaker without a timeout can leave you hanging if the breaker gets stuck in half-open and the service never responds.

## How Do You Choose Between Count-Based and Time-Based Failure Thresholds?

Count-based thresholds (e.g., "open after 5 failures") are simpler to reason about and work well when failure rates are consistent. Time-based thresholds (e.g., "open if error rate exceeds 50% in the last 10 seconds") adapt better to traffic fluctuations and avoid false positives during sparse request periods. Count-based can trip prematurely when traffic is low; time-based requires careful window sizing to avoid being too slow to react. Choose count-based for internal services with steady traffic, and time-based for public-facing or highly variable endpoints.

## What's the Right Strategy for Configuring Half-Open State Behavior?

The half-open state tests whether the downstream service has recovered by allowing a limited number of requests through. Set the half-open request limit low (typically 1–3) to minimize damage if the service is still unhealthy. Use a shorter timeout during half-open testing than normal operation, because a slow response during half-open usually signals the service isn't ready. A common mistake is using a large half-open limit or no special timeout handling, which defeats the purpose of controlled recovery and can overwhelm a fragile service. Pair it with a longer delay before entering half-open to give the downstream service meaningful recovery time.

## How Do You Decide Between Fail-Fast and Degraded-Response Fallback Strategies?

Fail-fast (returning an error immediately when the circuit opens) is appropriate for critical operations where stale or incorrect data causes more harm than unavailability. Degraded-response strategies (returning cached data, defaults, or reduced-scope results) maintain some user experience when the full service is down. Fail-fast is simpler to implement and debug; degraded responses require careful design of what "good enough" means and how long cached data is valid. Choose based on your users' tolerance: financial transactions often need fail-fast, while recommendation engines can degrade gracefully.

## When Should You Implement Multiple Nested Circuit Breakers in a Call Chain?

Use nested circuit breakers when your service makes calls to multiple downstream dependencies, each with independent failure modes. Each dependency gets its own breaker with thresholds tuned to its reliability and criticality. Nesting allows fine-grained control: you can degrade gracefully by losing one dependency while maintaining others. The pitfall is cascading trips—if your breaker at layer N opens, it can trigger timeouts at layer N-1, causing its breaker to open too. Mitigate this by ensuring timeouts + half-open delays at each layer are staggered or by using bulkheads to isolate blast radius.

## How Do You Balance Error Budget Consumption Between Circuit Breaker Thresholds and Fallbacks?

An error budget is your allowance for failures before you violate availability SLOs. Circuit breaker thresholds determine how many failures you absorb before stopping requests; fallbacks determine whether those stopped requests become user-facing errors. If your error budget is tight, set stricter circuit breaker thresholds to open earlier and reduce error count, then invest in fallback strategies that don't consume budget (like serving stale data). If your budget is loose, you can tolerate more failures before opening, giving the breaker time to distinguish transient blips from real outages. Review both thresholds and fallback effectiveness monthly against actual SLO tracking.

## What's the Difference Between a Circuit Breaker and a Bulkhead, and When Do You Use Both?

A circuit breaker stops requests to a failing service; a bulkhead isolates resources (threads, connections, memory) so one service's failure doesn't starve others. They solve different problems: a breaker prevents cascading failures across services; a bulkhead prevents a single misbehaving dependency from consuming all your capacity. Use a circuit breaker on every downstream call; use bulkheads on your most critical resources or when you have low-priority work that can be deprioritized. They work together: a bulkhead prevents a breaker from being overwhelmed during testing, and a breaker prevents a bulkhead from filling up with requests to a dead service.

## How Do You Configure the Delay Before Transitioning from Open to Half-Open?

The delay (also called "sleep window") should reflect your best estimate of how long the downstream service needs to recover. Too short, and you'll re-enter half-open repeatedly while the service is still starting up; too long, and you stay unavailable longer than necessary. Start with 30 seconds for in-datacenter services and 60+ seconds for external APIs, then adjust based on observed recovery times. Use exponential backoff if repeated half-open probes fail again—double the delay and retry. A common pitfall: fixed delays that don't account for the type of failure (e.g., a restart needs longer than a temporary spike).

## When Should You Use Success-Based vs. Failure-Based Half-Open Exit Criteria?

Success-based means half-open closes after a few successful requests; failure-based means it reopens immediately on the first failure. Success-based (standard approach) is more lenient and confirms the service is stable before fully closing. Failure-based is stricter and protects against services that are partially functional. In practice, use success-based as your default—it's easier to tune and causes fewer unnecessary reopenings. Use failure-based only for critical services with all-or-nothing semantics (you can't tolerate any bad responses leaking through). The pitfall with failure-based: a single slow request during half-open can reopen the breaker, creating a ping-pong effect.

## How Do You Avoid False Positives from Transient Network Blips?

Transient failures (a single dropped packet, brief GC pause) shouldn't trip your breaker. Distinguish them by requiring failures to be consecutive or by using a small time window—if the next request succeeds, don't count the prior failure against your threshold. Alternatively, use a fast retry-with-backoff before incrementing the failure counter. The trade-off: stricter logic avoids false opens but delays real failure detection. For count-based breakers, require at least 3–5 consecutive failures before opening; for time-based, ensure your window is at least 10 seconds. Log and monitor half-open transitions separately to catch patterns of transient blips that might indicate infrastructure issues.

## What Metrics Should You Export from Your Circuit Breaker for Monitoring?

Export: state transitions (closed → open, open → half-open, half-open → closed), count of failures that triggered opening, duration spent in each state, and success/failure rates during half-open probes. These reveal whether breakers are working as intended and catch configuration drift. Additionally log which specific error types triggered the breaker—network timeouts, 503s, and parsing errors have different implications for recovery. A common oversight: only monitoring state without failure counts, which can hide a breaker that's flapping rapidly between open and half-open. Set alerts on state transitions to a service that's breaking frequently (indicates a deeper problem) and on breakers stuck in half-open (indicates recovery is stalled).

## How Do You Handle Slow Requests During Half-Open Testing?

A slow request during half-open doesn't necessarily indicate failure—it might be a transient spike. Set a shorter timeout for half-open probes than normal operation (e.g., 2 seconds vs. 10 seconds), so you reject slow responses quickly without waiting for the full timeout. Treat slow responses during half-open as soft failures: count them, but require a few slow responses before reopening (don't reopen on the first slow request). This prevents thrashing when the service is recovering but not yet fully healthy. Document your half-open timeout separately from your normal timeout to avoid confusion during troubleshooting.

## When Should You Disable a Circuit Breaker Temporarily for Debugging?

Disabling should be a last resort and only for isolated debugging. If you disable a breaker in production, you lose protection against cascading failures and your service becomes vulnerable to timeouts and resource exhaustion. If you must debug, disable for a single service instance in a canary environment, collect data, then re-enable immediately. Never disable for more than a few minutes, and always have an automated timeout to re-enable if you forget. A better approach: use a "forced open" mode for testing (explicitly holding the breaker open to simulate downstream failure) rather than disabling the breaker entirely.

## How Do You Coordinate Circuit Breaker Configuration Across Multiple Instances of Your Service?

Breaker state is typically local to each instance—one instance's breaker can be closed while another's is open. This is fine for detection (each instance independently detects failure and opens), but it can lead to thundering herd during half-open testing (all instances probe simultaneously). Use a shared cache or broadcast mechanism to coordinate state across instances: when one instance opens its breaker, inform others to also open immediately (or at least increase their failure thresholds). Alternatively, use a centralized breaker service that all instances query. The trade-off: coordination adds latency and introduces a single point of failure (the coordinator), but it reduces unnecessary probing during recovery.

## What's the Right Approach to Breaker Configuration for Batch or Periodic Jobs?

Batch jobs often have looser latency tolerances but stricter failure tolerances (one failure might invalidate an entire batch result). Use a higher failure threshold for batch jobs—allow more failures before opening—but a shorter half-open delay (since the next batch attempt won't happen for hours anyway). For jobs that retry internally, place the breaker around the entire job rather than around each internal retry, to prevent the job from hammering the downstream service repeatedly. Set the breaker to open very conservatively for batch jobs because you can't use degraded responses—you either complete or fail the entire job.

## How Do You Test Your Circuit Breaker Configuration Without Breaking Production?

Use chaos engineering practices: inject failures in a staging environment that mirrors production traffic patterns. Verify that your breaker opens at the expected failure count, that half-open probes recover correctly, and that your fallback strategy actually works. Simulate not just failures but also the timing and distribution (bursty vs. steady). Test breaker interactions with timeouts and retries—make sure they're not fighting each other. A pitfall: testing only the happy path (verifying the breaker opens and closes) without verifying user experience during open state. Also test recovery under load: does half-open succeed when traffic is high, or does it fail and reopen immediately?

## When Should You Use a Breaker on Internal vs. External Service Dependencies?

Use circuit breakers on all dependencies (internal or external), but tune thresholds differently. External services are less reliable and you have no control over their recovery, so use lower failure thresholds and longer half-open delays. Internal service failures are usually faster to repair (you can fix them immediately), so you can use slightly higher thresholds and shorter delays. For internal services, breakers serve mainly to prevent cascading failures; for external services, they're essential to give the provider time to recover and to protect your users from persistent outages. In both cases, invest in monitoring the downstream service's health separately, not just your breach detections.

## How Do You Prevent a Circuit Breaker from Getting Stuck in Half-Open?

Half-open gets stuck when requests consistently succeed at a low level but never reach the success threshold needed to close. This happens with flaky services that work intermittently. Set a maximum half-open duration (e.g., 5 minutes) after which the breaker reopens automatically if it hasn't closed. Alternatively, use a higher success threshold for closing (e.g., 10 successful requests) rather than a low threshold (e.g., 1 success). Tune your half-open timeout and retry logic—if the half-open probe always times out, it'll never succeed. Log and alert on breakers stuck in half-open for more than a few minutes, as this usually indicates a service that's partially functional and needs manual investigation.

## How Do You Configure Breakers for Services with Multiple Error Types?

Not all errors warrant opening the breaker equally. A 503 (service unavailable) usually means the service is down and you should open; a 400 (bad request) means your client is misconfigured and opening the breaker won't help. Make your breaker sensitive only to server errors and network timeouts, and count client errors separately or ignore them. Some implementations support error categorization: define a set of "retryable" errors that count toward the threshold, and let non-retryable errors fail immediately. This requires knowing your downstream service's error semantics—document which errors are transient vs. permanent. A common pitfall: treating all 5xx errors equally when some (like 501 not implemented) indicate a permanent mismatch and won't recover.

## What's the Interaction Between Circuit Breaker Thresholds and Retry Logic?

Retries and breakers can conflict: retries give the service more chances to succeed, but too many retries can overwhelm a struggling service and trigger the breaker. Disable retries once the breaker opens (you're already controlling the rate of requests to the failing service), or use very aggressive retries (immediate, no backoff) only when the breaker is closed. If you use exponential backoff in retries, it can mask breaker thresholds—failures will appear more spread out and slow to aggregate. Structure your retry logic as: fast retries while the breaker is closed (if it's a transient blip, retries help), then fail fast once the breaker is open. Test the interaction by simulating failures and verifying that retries don't prevent the breaker from opening.

## How Do You Balance Aggressive vs. Conservative Breaker Configuration?

Aggressive configuration (low failure thresholds, short timeouts) opens quickly, protecting your service from overload but risking false positives that deny users access to a flaky but functional service. Conservative configuration (high thresholds, long timeouts) tolerates more failures but can lead to user-facing timeouts and cascading failures. Start conservative and tighten based on observed failure patterns. Monitor the ratio of true failures (where the service was actually down) to false positives (where a transient blip triggered the breaker). If your false-positive rate is high, increase thresholds or require consecutive failures; if you're missing real outages, decrease thresholds. Different services warrant different settings—standardize a template but allow override for critical vs. best-effort paths.
