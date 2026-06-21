# Idempotency Patterns

## When Should You Use Idempotency Keys Instead of Natural Idempotency?

Idempotency keys—client-provided or generated request identifiers stored server-side—are your go-to when the operation itself has no natural unique identifier. Use them for transfers between accounts, order creation from user input, or any write that combines multiple input fields without a pre-existing ID. The mechanism is simple: hash the key, store it with the result, and return the cached result if you see it again. The main tradeoff is operational: you need a persistent store (database, cache with TTL discipline) and must decide retention windows carefully. Storing keys indefinitely wastes space; too short a window risks duplicate processing if clients retry slowly.

## How Do You Choose Between Database-Backed and Cache-Backed Dedup?

Your dedup storage choice depends on consistency requirements and failure tolerance. Database-backed dedup (typically a table with key, timestamp, and result) provides durability and survives server restarts, making it suitable for high-value operations like payments. Cache-backed (Redis, Memcached) is faster and simpler but loses data on cache failure, requiring fallback logic or acceptance that some requests may be reprocessed after outages. Hybrid approaches—write to both, read from cache first—add complexity. The deciding factor: can you tolerate reprocessing a small percentage of requests, or is the operation expensive/irreversible enough to demand durability?

## What's the Right TTL Strategy for Idempotency Key Storage?

Idempotency keys should persist longer than your longest expected client retry window, but not forever. Set TTL based on your retry policy: if clients give up after 1 hour, keep keys for at least 2 hours. For financial transactions, consider 24–48 hours to handle delayed batch retries or support escalations. Storage costs grow linearly with TTL, so periodic cleanup of expired keys is necessary if your store doesn't auto-expire (databases need scheduled jobs; Redis handles this natively). Watch for the subtle issue: if you aggressively prune old keys, a delayed retry becomes a duplicate execution, not idempotent handling. Document your TTL choice and make it discoverable to clients.

## How Do You Handle Idempotency Key Collisions?

Collisions occur when two different requests accidentally share the same key—rare but possible with weak hash functions or client bugs. Most systems treat a collision as a cache hit: return the stored result regardless of whether the new request is actually identical. This is acceptable if keys are cryptographically strong (UUID v4, HMAC) and collision probability is negligible. If you need stricter semantics, validate that request content matches the stored request before returning the cached result; this adds overhead and complexity. In practice, the simpler approach (return cached result on key match) is standard because deliberate key reuse is the actual idempotency contract, not accidental collision avoidance.

## When Should You Use Natural Idempotency Over Idempotency Keys?

Natural idempotency means the operation's semantics make it safe to repeat without external state tracking—e.g., `PUT /user/123` is naturally idempotent because the second write produces the same state as the first. Use natural idempotency when the business operation has inherent idempotence: setting a value, creating a resource by ID, or applying deterministic transformations. No dedup table needed, no key storage overhead. However, not all operations are naturally idempotent; `POST /transfers` is not because each execution debits an account. The pitfall: assuming an operation is naturally idempotent when it isn't. Verify that repeating the request with identical inputs truly produces the same observable outcome, including side effects.

## How Do You Design Request Content Hashing for Idempotency Key Derivation?

When generating idempotency keys from request content (rather than accepting client-provided keys), hash stable request fields to produce the key. Include request body, user ID, and endpoint; exclude timestamps, request IDs, or other non-deterministic metadata. Use a cryptographic hash (SHA-256) and truncate if needed. The trap: including too many fields makes the key sensitive to minor variations (formatting, field order) that don't represent true duplicates. JSON field order variation is a common culprit. Normalize the request representation before hashing (canonical JSON, sorted fields, consistent types). Document exactly which fields are hashed so clients can predict the key if they generate it themselves.

## What's the Difference Between Exactly-Once and At-Least-Once Semantics in Idempotency?

Exactly-once semantics mean each operation executes precisely one time, no more, no less—achieved by idempotency key tracking plus exactly-once message brokers. At-least-once means operations execute one or more times, requiring the operation itself to be idempotent (natural or via dedup). For most REST APIs, at-least-once with idempotent operations is the practical choice: simpler infrastructure, no single point of failure in dedup logic. Exactly-once requires stronger guarantees from the message broker and stricter operational discipline. Choose exactly-once only if the operation is not naturally idempotent and cannot tolerate any reprocessing—e.g., issuing unique sequential invoice numbers. Otherwise, at-least-once is more resilient.

## How Do You Implement Idempotent Consumers for Event Streams?

Event stream consumers (Kafka, Kinesis) need dedup logic to handle redelivery from broker restarts or network retries. Store the event ID (offset, partition, message ID) in a consumer-side dedup table alongside the last processed event timestamp. Before processing a new event, check if you've seen its ID before; if so, skip processing. Combine this with transactional writes: update both the business state and the dedup record atomically to avoid races. Watch the ordering assumption: dedup prevents duplicate *processing*, but doesn't guarantee messages are processed in order if you're running parallel consumers. For strict ordering, use a single-threaded consumer or partition-locked processing.

## When Should You Persist Idempotency Results vs. Discarding Them?

Persisting results (storing the operation's return value alongside the key) lets you replay it for retries without re-executing. This is essential for expensive operations (API calls to external services, long computations) or non-deterministic ones (data depends on current timestamp or external state). For cheap, deterministic operations, storing just the fact that the key was seen is sufficient; re-execute on retry. The cost-benefit: persistence doubles your storage footprint but eliminates redundant work. Decide based on operation cost and latency sensitivity. For payment processing, always persist results. For simple record updates, re-execution is often acceptable.

## How Do You Handle Clock Skew in Idempotency Key Timestamps?

Idempotency key records typically include a `processed_at` timestamp for cleanup and auditing. Clock skew—servers' clocks drifting out of sync—can cause confusion: a "recent" retry might appear older than the original request, or TTL cleanup might prune unexpectedly. Use monotonic timestamps (NTP-synchronized servers or database-generated timestamps) rather than client-provided times. Store timestamps in UTC, never wall-clock time. In distributed systems, accept small clock drift (±a few seconds) as normal and make TTL windows generous to accommodate it. Never use client-provided timestamps for dedup or expiry decisions; use them only for logging and auditing.

## What's the Right Scope for an Idempotency Key: Global, Per-User, or Per-Account?

Idempotency keys can be scoped to enforce uniqueness constraints. A globally unique key (across all users) prevents one user from reusing another's key by accident. A per-user key allows different users to share keys but isolates their operations. Per-account is a middle ground for multi-tenant systems. Global scoping is simplest but uses more dedup storage; per-user scoping reduces storage and adds a dimension to the lookup (user_id, key). The practical choice: scope to the principal making the request (user or account) unless cross-principal idempotency is meaningful. This also improves cache locality and reduces key collision risk across different request streams.

## How Do You Test Idempotency in Integration Tests?

Test idempotency by deliberately sending the same request twice and verifying the response is identical both times and the side effect occurred exactly once. Use the same idempotency key both times (or generate it deterministically from request content). For async operations, wait for the first request to complete, then send the duplicate and verify no new work is queued. Test with time delays between retries to catch TTL-related bugs. Verify idempotency at the boundary layer (API, event consumer) not in unit tests, because dedup logic is inherently a systems concern. Common pitfall: testing with artificial, tiny request intervals that don't reflect real retry patterns.

## When Should Idempotency Key Validation Be Strict vs. Permissive?

Strict validation—reject any retry if the request body doesn't match the original—prevents accidental cross-contamination but adds latency (you must store the original request or a hash). Permissive validation—accept any retry with the same key regardless of request content—is simpler and faster but allows bugs where clients send wrong data on retry expecting to overwrite. The middle ground, used by Stripe and similar services, is to hash request content and compare hashes: quick to validate, prevents content mismatches, doesn't require storing full request payloads. Choose strict only if your operation's semantics truly require request content matching. For most cases, permissive (matching by key alone) is acceptable and expected by clients.

## How Do You Prevent Idempotency Key Exhaustion Attacks?

An attacker could generate many unique idempotency keys to fill your dedup store and exhaust storage. Mitigate by rate-limiting per client/user (use a rate limiter per API principal), setting aggressive TTLs (hours, not days), and setting a maximum dedup table size with LRU eviction. Monitor dedup table growth and alert on anomalies. Use database storage with configurable retention rather than unbounded in-memory caches. Validate that idempotency keys are reasonably formatted (length, character set) and reject obviously malformed ones. Note: this is not a major threat in practice because legitimate requests also consume dedup slots, but reasonable safeguards are worth the effort.

## What's the Right Pattern for Chaining Multiple Idempotent Operations?

When one operation depends on the output of another, each must be idempotent independently, but the chain's overall idempotency requires careful sequencing. Give each operation its own idempotency key. If operation B fails and is retried, operation A will be retried first (via retry logic at a higher level) and its idempotency key ensures it re-executes at most once. The pitfall: assuming operation B's idempotency key alone guarantees the chain is idempotent. It doesn't—operation B might execute multiple times if operation A nondeterministically produces different outputs. Use saga patterns or workflow orchestrators for long chains; they track which steps have completed and skip already-done work. For short chains (2–3 steps), careful ordering and explicit state tracking is acceptable.

## How Do You Debug Idempotency Key Issues in Production?

Log every idempotency key interaction: key received, lookup result, cache hit/miss, and whether the result was returned or execution occurred. Include the key, principal (user/account), timestamp, and outcome in structured logs. Query logs by key to trace a request's journey. Set up alerting on unexpected patterns: same key from different users, unusually high dedup hit rates, or rapid repeated keys from the same user. Use distributed tracing to correlate idempotency events across services. The operational blind spot: relying on application logs alone without visibility into the dedup store; query the dedup table directly to verify stored records match your expectations.

## When Should You Use Composite Keys vs. Single Idempotency Keys?

Composite keys combine multiple fields (e.g., user_id + operation_type + resource_id) to derive a unique key covering related operations. This is useful for preventing duplicate operations within a bounded scope, like preventing duplicate payment attempts for the same invoice. Single, globally unique keys (UUIDs) are simpler and avoid key derivation logic but don't prevent logically duplicate operations if the client generates a new key. Use composite keys when the business logic defines what "duplicate" means and you want to prevent different keys from causing the same logical operation twice. Use single keys for simple, stateless operations. Composite keys add cognitive load; document the derivation clearly.

## How Do You Handle Idempotency for Streaming or Bulk Operations?

Bulk operations (batch uploads, stream processing) need per-item or per-batch idempotency. For batch operations, assign a batch ID and store it alongside batch metadata; retrying the entire batch is idempotent if you track the batch ID. For streaming, assign unique IDs to each record and track processed IDs (similar to event consumers). The challenge: large batches make per-item dedup expensive. Use range checks or batch checksums to optimize: if you've processed items 1–100 from batch X, reprocessing items 1–100 is a no-op. Partial-batch retries (only items 50–100 failed) require granular tracking; use checksums or Merkle trees to identify divergent items efficiently.

## What Happens to Idempotency Keys When Services Migrate or Databases Change?

Idempotency key stores often outlast the systems they were created for. When migrating databases or decommissioning services, migrate dedup records to the new store, maintaining key and result alongside mappings. Do not discard old keys; even after migration, retrying with an old key should be idempotent. For long migrations, run dual-write phases: write to both old and new stores. Query the new store first, fall back to the old store if not found. Set explicit TTLs during migration so old records naturally expire rather than requiring manual cleanup. Plan for old systems and new systems to coexist briefly; clients don't know about your migration timeline and will keep retrying.

## How Do You Balance Idempotency Overhead Against Operation Latency?

Idempotency dedup adds latency: lookup (10–100ms for network roundtrips), write on miss, and potentially result serialization. For latency-sensitive operations, this overhead is noticeable. Mitigate by using fast dedup stores (in-memory cache, Redis) with database fallback, caching dedup lookups in application memory, and batching dedup writes. Accept that some operations may not be idempotent; not every request needs it. Reserve idempotent handling for operations that clients actually retry (payment APIs) and skip it for operations that complete instantly or are self-healing (cache fills, ephemeral computations). Measure dedup latency in production and adjust retention strategies if overhead is significant.

## When Should You Implement Idempotency at the API Layer vs. Consumer/Worker Layer?

API layer idempotency (REST endpoints) is visible to external clients and part of the API contract; it's expected for operations that clients retry (POST requests). Consumer/worker layer idempotency (event handlers, background jobs) is internal and necessary whenever messages are redelivered. Use both when applicable: API idempotency shields callers, consumer idempotency shields your workers. The gap: if the API is idempotent but the consumer isn't, an API client sees an idempotent operation, but multiple internal jobs execute. Conversely, an idempotent consumer with a non-idempotent API exposes duplicate processing to callers. Implement idempotency at every layer where delivery isn't guaranteed, not just the boundary layer.

## How Do You Document Idempotency Contracts for API Consumers?

Include idempotency expectations in API documentation: which endpoints support it, what field is the idempotency key, TTL for key retention, and the response behavior on duplicate requests (e.g., "returns the same 200 response as the original request"). For client-provided keys, specify format (UUID, string length, character set). For derived keys, document which request fields are used. Provide code examples showing key generation or provision. In error cases, clarify whether the operation was idempotent (did it execute once or multiple times before failing?). Avoid vague language like "this endpoint is idempotent"—be specific about what the client must do to make it idempotent.
