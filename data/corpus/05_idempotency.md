# Idempotency Patterns

## Idempotency Keys

An idempotency key is a client-generated unique identifier attached to a request that allows the server to detect and handle duplicate submissions. When a request arrives with a key the server has already processed, the server returns the cached result instead of re-executing the operation. This is commonly implemented by storing (key, result) pairs in a lookup table. Idempotency keys are essential for handling network retries, browser refreshes, and client-side retry logic without causing side effects. They shift responsibility for deduplication to the service layer. Keys should be immutable for a given logical operation and unique across all requests. The main tradeoff is the storage overhead of maintaining a dedup table and the complexity of coordinating keys across distributed clients. Example: A payment service accepts transfer requests with an idempotency key; if the client retries due to timeout, the service recognizes the duplicate key and returns the original transfer ID without charging twice.

## Natural Idempotency

Natural idempotency refers to operations that are inherently safe to repeat multiple times without unintended side effects. These operations produce the same result regardless of how many times they execute. Examples include reads, updates that set values (rather than increment), and deletions. Natural idempotency requires no additional deduplication infrastructure—the operation's semantics provide safety. However, not all business operations are naturally idempotent; transfers, payments, and provisioning operations typically require explicit idempotency mechanisms. Natural idempotency should be leveraged wherever possible to reduce complexity. The challenge is designing systems to maximize naturally idempotent operations and clearly documenting which operations are unsafe to repeat.

## Exactly-Once Delivery Semantics

Exactly-once delivery guarantees that each message or operation is processed precisely one time, even in the presence of failures or retries. This is the strongest delivery guarantee but also the most difficult and expensive to implement reliably. Achieving exactly-once typically requires combining idempotency detection with transactional processing. In practice, exactly-once requires coordinating across multiple layers: message brokers, service logic, and storage systems. Most distributed systems cannot guarantee true exactly-once without significant architectural complexity and performance costs. Instead, they often implement exactly-once semantics through a combination of deduplication and careful transaction handling.

## At-Least-Once Delivery Semantics

At-least-once delivery guarantees that a message or operation will be delivered and processed at least one time, though it may be processed multiple times. This is easier to implement than exactly-once and is the default behavior of many message brokers and retry mechanisms. Applications must be designed to handle duplicate deliveries gracefully, typically by implementing idempotency on the consumer side. At-least-once is suitable when the cost of occasional duplicates is lower than the cost of building exactly-once infrastructure. For example, log aggregation systems often use at-least-once semantics because reprocessing a log entry is harmless. The application layer must detect and suppress duplicates.

## Deduplication Tables

A deduplication table (or idempotency store) is a database table that tracks which operations have already been processed, keyed by an idempotency key. When a request arrives, the system checks the dedup table; if an entry exists, it returns the cached result. If not, it processes the request and stores the result in the table. Dedup tables are the primary mechanism for implementing idempotency keys at scale. Key design decisions include: retention policy (how long to keep entries), what to store in the result column (full response vs. pointer to result), and whether to use a dedicated database or co-locate with the main datastore. Dedup tables must handle concurrent writes carefully to avoid race conditions. They add latency (extra database lookup) but prevent costly duplicate processing.

## Request-Response Caching for Idempotency

Request-response caching uses an idempotency key to cache and return the full response for previous requests. When a duplicate request arrives, the system retrieves the cached response from memory or cache layer rather than re-executing the operation. This pattern is efficient and provides low-latency deduplication. The tradeoff is that responses must be serializable and safe to re-transmit. Cache invalidation and retention policies must be carefully managed—responses typically cannot be cached indefinitely due to storage constraints. Example: An API service uses Redis to cache responses keyed by idempotency key for 24 hours, allowing rapid duplicate detection without database overhead.

## Distributed Transaction Coordination

Distributed transaction coordination ensures that operations spanning multiple services either all succeed or all fail, supporting idempotency across system boundaries. Patterns include two-phase commit (2PC) and saga patterns. Two-phase commit uses a coordinator to manage prepare and commit phases across multiple services, but it's difficult to implement reliably and scales poorly. Saga patterns decompose operations into a series of local transactions with compensating transactions for rollback. Both patterns are complex and may impact performance. Distributed transactions are necessary when an operation must atomically affect multiple services, but they should be avoided when possible by redesigning data ownership.

## Saga Pattern for Distributed Idempotency

The saga pattern breaks a distributed operation into a sequence of local transactions, each with a compensating transaction for rollback. Idempotency is achieved by making each step idempotent and tracking which steps have completed. If a saga fails partway through, it executes compensating transactions in reverse order to undo prior steps. Sagas can be choreography-based (services call each other) or orchestration-based (a coordinator drives the flow). Orchestration is typically easier to reason about and debug. The challenge is ensuring compensating transactions actually undo the original transaction's effects and handling the case where a compensating transaction itself fails. Sagas are suitable for long-running, multi-step business processes.

## Idempotent Message Processing

Idempotent message processing ensures that consuming and processing a message multiple times produces the same result as processing it once. This is critical for message queue systems where messages may be delivered multiple times due to failures or retries. Consumers must be designed to detect duplicate messages (typically via message ID or idempotency key) and skip reprocessing. This often requires maintaining state about which messages have been processed. Idempotent processing supports at-least-once delivery semantics from message brokers. The main challenge is state management and coordination when multiple consumer instances run in parallel.

## Idempotent Consumers

An idempotent consumer is an application that consumes messages or events and can safely process the same message multiple times without causing duplicate effects. Idempotent consumers rely on idempotency keys or natural idempotency in their operations. They must track which messages have been processed (via dedup tables, caches, or message IDs) and skip duplicates. Idempotent consumers decouple from the reliability guarantees of the upstream system, allowing the message broker to use simpler at-least-once semantics. This architectural pattern is fundamental to reliable systems: push idempotency responsibility to the consumer layer rather than trying to guarantee exactly-once in the broker. Examples include consuming Kafka topics or SQS queues.

## Versioned State for Idempotency

Versioned state uses version numbers or timestamps to detect and handle duplicate updates idempotently. When an operation arrives, the system checks the current version; if the version hasn't changed, it applies the operation and increments the version. Duplicate requests with an earlier version are detected and either rejected or returned with the current state. This pattern is useful for handling concurrent updates and ensuring idempotency without explicit dedup tables. It's commonly used in optimistic concurrency control. The tradeoff is that all updates must be version-aware, and applications must handle version conflicts. Example: A document service tracks document version; an update request specifies the version it's updating from, allowing detection of stale or duplicate updates.

## Idempotency in Payment Processing

Payment processing requires strict idempotency because duplicate charges have severe consequences. Payment systems typically implement idempotency keys as a first-class requirement—clients must provide an idempotency key (often a UUID) with each payment request. The payment processor records the key and result; if the same key arrives again, it returns the original transaction ID without charging again. Idempotency keys in payments must be stored durably and tracked indefinitely (or for a very long period). Payment systems often also use additional safeguards like duplicate detection based on amount, account, and timestamp. This is one of the most critical applications of idempotency patterns.

## Conditional Updates for Idempotency

Conditional updates use predicates (conditions) on the current state to make updates idempotent. An operation specifies a condition that must be true for the update to proceed; if the condition fails, the update is skipped. This allows updates to be safely repeated—the second update will find the condition already met and do nothing. Example: "If account balance is exactly $100, deduct $50" is idempotent; repeating it will find the balance is $50 and skip. Conditional updates avoid the need for explicit dedup tables by leveraging data state. They require careful condition design and are most suitable for operations that have a clear target state. Database systems often support conditional updates via optimistic locking or compare-and-swap semantics.

## Idempotency Keys in REST APIs

REST API idempotency is commonly implemented via the Idempotency-Key header, which clients include on requests. The server stores this key and its associated response; on duplicate requests with the same key, it returns the cached response. This pattern is standardized in proposals like RFC 7231 and used widely in payment and financial APIs. Idempotency keys are typically UUIDs and should be scoped to a user or tenant. Retention policies vary but typically range from 24 hours to 30 days depending on the use case. The main advantage is simplicity and standardization; the overhead is the dedup table lookup and storage. APIs must document which endpoints are idempotent and how long keys are retained.

## Event Sourcing with Idempotency

Event sourcing records all changes to state as a sequence of immutable events. Idempotency can be achieved by deduplicating events before appending them to the event stream. When the same event (identified by idempotency key) is submitted again, it's recognized as a duplicate and not appended. The event stream becomes the source of truth; replaying events produces the current state. This architecture naturally supports exactly-once semantics because each event is appended exactly once. Deduplication requires tracking which events have been seen, typically in a separate dedup table. Event sourcing adds complexity (event versioning, replay logic) but provides auditability and supports temporal queries.

## Outbox Pattern for Reliable Messaging

The outbox pattern ensures that operations and their corresponding messages are processed atomically. When an operation completes, it writes an outbox record (containing the message to publish) in the same transaction. A separate process polls the outbox and publishes messages, deleting outbox entries once published. This guarantees that if an operation succeeds, the corresponding message will eventually be published, supporting exactly-once semantics. If the message publishing fails, the outbox record remains and will be retried. Idempotency must be implemented in the message consumer because outbox publishing may produce duplicates. The outbox pattern is essential for maintaining consistency between local state and external events.

## Inbox Pattern for Idempotent Consumption

The inbox pattern ensures that incoming messages are processed idempotently and exactly-once. When a message arrives, the service writes it to an inbox table and then processes it. Duplicate messages are detected via inbox records and skipped. Once processed, the inbox entry is marked complete. This pattern decouples message processing from delivery guarantees; the message broker can deliver at-least-once, and the inbox pattern upgrades it to exactly-once. The inbox requires transactional writes (message stored and processed in same transaction) and cleanup policies for old records. Combined with the outbox pattern, inbox/outbox provides end-to-end exactly-once semantics.

## Lease-Based Idempotency for Distributed Locks

Lease-based idempotency uses distributed locks with time-limited leases to ensure operations execute only once. A client acquires a lease before executing an operation; if the lease is held, the operation is skipped. The lease automatically expires after a timeout, allowing recovery if the lease holder crashes. This pattern is useful for ensuring idempotency in systems without a dedup table, but it adds complexity and potential failure modes (lease expiration while operation is in progress). Lease-based approaches work well for long-running operations where explicit dedup is impractical. The main tradeoff is balancing lease timeout (too short causes duplicate processing, too long delays recovery).

## Deterministic Processing for Idempotency

Deterministic processing makes operations idempotent by ensuring they always produce the same output given the same input, with no randomness or external state dependencies. Operations must not call external services, use timestamps, or rely on system state. Instead, they compute results purely from inputs. This enables safe replay of operations—reprocessing with the same input always produces the same output. Deterministic processing is powerful for event sourcing and distributed systems where replay is necessary. The limitation is that many real operations require external calls (payment processors, third-party APIs). Deterministic processing must be carefully designed and documented.

## Partial Failure Handling in Idempotent Systems

Partial failure occurs when some steps of an operation succeed while others fail, leaving the system in an inconsistent state. Idempotent systems must handle partial failures gracefully. When a retry occurs, the idempotency detection layer must recognize which steps have already completed and skip them, restarting only failed steps. This requires detailed state tracking about operation progress. Saga patterns and distributed transactions address partial failure through compensating transactions. Alternatively, idempotent operations can be designed to be restartable—the retry picks up where the previous attempt left off. Careful design is needed to avoid duplicate work while ensuring all steps eventually complete.

## Idempotency Key Lifecycle and Cleanup

Idempotency keys must be stored durably but cannot be retained indefinitely due to storage costs. The lifecycle of an idempotency key involves storage, retention, and cleanup phases. Retention policies typically range from 24 hours to 30 days, depending on business requirements (longer for payments, shorter for ephemeral operations). Cleanup can be batch-based (periodic deletion of expired entries) or incremental (lazy deletion on access). The tradeoff is between storage cost and the risk of handling an old request as new if the key is cleaned up prematurely. Critical operations like payments should have longer retention and careful cleanup policies. Monitoring and alerting should track dedup table growth and cleanup effectiveness.

## Cross-Service Idempotency Coordination

Cross-service idempotency coordination ensures that operations spanning multiple microservices maintain idempotency guarantees. When a client request triggers operations across services, each service must track the idempotency key and coordinate results. This requires: agreement on key format, shared or distributed dedup tracking, and consistent response handling. Services may coordinate via a central dedup service, distributed tracing, or explicit message passing. The challenge is consistency—all services must agree on whether an operation succeeded or failed. Saga patterns and distributed transactions address this but at significant complexity and performance cost. Alternative approaches include designing operations to be naturally idempotent across service boundaries.
