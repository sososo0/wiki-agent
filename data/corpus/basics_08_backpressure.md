# Backpressure Mechanisms

## What Is Backpressure?

Backpressure is a signal sent upstream in a system that says "slow down, I can't keep up." When one part of a distributed system processes data faster than another part can handle, backpressure is the feedback mechanism that prevents the faster part from overwhelming the slower one. Without backpressure, slow components get buried under requests they can't process, leading to memory exhaustion, timeouts, and cascading failures. Think of a fire hose: if you're filling a bucket but water flows faster than the bucket can drain, backpressure is like pinching the hose to reduce flow until the bucket catches up.

## Why Do We Need Backpressure in Distributed Systems?

In any system with multiple components working at different speeds, there's a risk that fast producers will create more work than slow consumers can handle. Backpressure prevents this imbalance from causing system collapse. Without it, requests queue up indefinitely, memory fills up, and the entire system degrades or crashes. Backpressure helps maintain stability by ensuring no single component becomes a bottleneck that drowns everything upstream. It's the difference between a system that gracefully handles load spikes and one that falls over when demand increases.

## What Are Queues and How Do They Enable Backpressure?

A queue is a buffer that holds requests or data waiting to be processed, sitting between a producer and consumer. Queues enable backpressure by having a maximum size: once a queue fills up, the producer must wait before adding more items. This naturally slows down the producer to match the consumer's pace. For example, if your web server receives 1,000 requests per second but your database can only handle 100 per second, a queue between them holds the excess requests. Once the queue fills, the web server stops accepting new requests until the database processes some queued items.

## How Does Load Shedding Protect Systems Under Extreme Load?

Load shedding is the practice of intentionally rejecting or dropping requests when a system is overloaded rather than letting them queue up indefinitely. Instead of accepting a request you can't process in time, you reject it immediately with a clear error. This prevents cascading failures and keeps the system responsive for requests you *can* handle. For example, if your API is flooded with traffic and your queue is full, rejecting new requests with a 503 error is better than accepting them, consuming memory, and then timing out anyway.

## What Is Reactive Streams Backpressure?

Reactive Streams is a standard for handling asynchronous data flow with explicit backpressure signals. Instead of pushing data to consumers, the consumer explicitly requests a certain number of items from the producer, and the producer only sends that amount. This gives the consumer fine-grained control over how fast it receives data. For example, in a system processing a large file, the consumer might say "send me 100 lines" rather than the producer dumping the entire file at once. This prevents the consumer from being overwhelmed while keeping the pipeline moving.

## How Does Admission Control Decide What Requests to Accept?

Admission control is a policy that determines which incoming requests your system will accept and which it will reject, usually before they consume significant resources. Instead of letting every request into the system to compete for resources, admission control gates entry based on current load, priority, or other criteria. For example, a payment processing system might reject non-critical requests during peak load to ensure critical transactions go through. Admission control acts as a bouncer at the door, preventing the system from becoming overloaded in the first place.

## What's the Difference Between Backpressure and Timeouts?

Backpressure prevents problems by slowing down the producer before resources are exhausted, while timeouts are a response mechanism that triggers after a request has already waited too long. Backpressure is proactive—the system says "wait, I'm getting overwhelmed"—whereas timeouts are reactive—the client gives up after waiting. Using both together is common: backpressure prevents most requests from timing out by controlling flow upstream, and timeouts catch cases where backpressure wasn't enough. A well-tuned system with strong backpressure should rarely hit timeouts.
