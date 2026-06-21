# Idempotency Patterns

## What Is Idempotency and Why Does It Matter?

Idempotency means an operation produces the same result no matter how many times you run it. In distributed systems, network failures and retries are inevitable—a request might arrive twice, or a response might get lost before the client sees it. If your system isn't idempotent, retrying a failed request could create duplicate charges, duplicate records, or inconsistent state. Idempotency lets you safely retry without fear of unintended side effects. Think of it like a light switch: flipping it on ten times has the same result as flipping it once—the light is on.

## What Are Idempotency Keys?

An idempotency key is a unique identifier that a client includes with a request to mark "this is the same logical operation." Your system stores this key along with the result, so if the same key arrives again, you return the cached result instead of re-executing. This is the most practical way to make non-idempotent operations safe to retry. For example, a payment API might require you to send a request ID; if your network hiccups and you retry, the API recognizes the ID and doesn't charge twice—it just returns the same receipt.

## What Is Natural Idempotency?

Natural idempotency means an operation is already safe to repeat without any special logic. Read operations are naturally idempotent—querying a database ten times gives you the same data. Some writes are too, like "set this field to value X"—repeating it doesn't change the outcome. The advantage is simplicity: you don't need to add deduplication logic or store request history. The tradeoff is that not all operations can be naturally idempotent; operations like "add 1 to a counter" or "transfer money" inherently change state and need explicit idempotency mechanisms.

## How Do Deduplication Tables Help?

A deduplication table (or request cache) is a database table that stores recent requests and their outcomes, keyed by idempotency key. When a request arrives, you check this table first: if the key exists, you return the stored result; if not, you execute the operation, store the result, and mark it complete. This is the workhorse implementation behind idempotency keys. A simple example: a table with columns `(idempotency_key, status, response)` lets you handle retries by looking up the key rather than re-processing. The main cost is storage and cleanup—old entries must eventually be deleted.

## What's the Difference Between Exactly-Once and At-Least-Once Delivery?

At-least-once means a message or request is guaranteed to arrive, but might show up multiple times—retries happen automatically. Exactly-once means it arrives one and only one time, with no duplicates. Exactly-once is much harder to build and comes with latency costs. In practice, most systems use at-least-once delivery paired with idempotent consumers: the system doesn't promise no duplicates, but your code handles them gracefully. This is the pragmatic middle ground—it's easier to build, faster, and safe as long as your operations are idempotent.

## What Is an Idempotent Consumer?

An idempotent consumer is a piece of code that processes messages or events safely even if they arrive more than once. Instead of relying on the messaging system to prevent duplicates, the consumer itself detects and skips them. A common pattern: read a message ID, check if you've already processed that ID, and if so, skip it or return the cached result. Idempotent consumers shift responsibility from infrastructure to application code, which is often easier and cheaper than building exactly-once guarantees into a message broker. For instance, a worker processing orders might track processed order IDs in a set and ignore any it has seen before.

## How Do You Choose the Right Idempotency Pattern for Your System?

Different operations need different patterns based on their nature and risk. Read-only operations need nothing—they're naturally idempotent. Simple state mutations (like setting a field) can often be naturally idempotent too. For operations with side effects (payments, sending emails, creating records), use idempotency keys plus a dedup table if you control the request path, or use an idempotent consumer if you're processing from a queue. The guiding principle: make failures safe to retry without creating duplicates or inconsistency. Start simple—add idempotency keys where they're needed most (money operations, external API calls)—and expand as your system grows.
