# Rate Limiting Algorithms

## What Is a Token Bucket?

A token bucket is a rate limiting method where you refill a container with a fixed number of tokens at regular intervals, and each request costs one token to process. If the bucket runs out of tokens, new requests are rejected until more tokens arrive. This matters because it lets you allow sudden traffic spikes (by accumulating unused tokens) while still enforcing an average limit over time. Think of it like a coffee shop that gives out 10 drink vouchers every hour—if you didn't use vouchers yesterday, you can use extra today, but you can never exceed the bucket's maximum capacity.

## What Is a Leaky Bucket?

A leaky bucket is a rate limiting method where requests go into a queue, and they're processed at a fixed, constant rate regardless of how fast they arrive. If the queue gets too full, new requests are dropped. This matters because it smooths out bursty traffic into a steady stream, preventing downstream systems from being overwhelmed by sudden demand spikes. Imagine a bathtub where water pours in at unpredictable speeds, but it drains out at a constant rate—the tub itself limits how much water can pile up before it overflows.

## What Is a Fixed Window Counter?

A fixed window counter is a rate limiting method where you divide time into fixed intervals (like each minute or hour), reset a counter at the start of each interval, and allow a maximum number of requests per interval. This matters because it's simple to implement and understand, making it a good starting point for basic rate limiting needs. The downside is that requests can bunch up at interval boundaries, but that's a problem for a different document. Imagine a library that lets 20 people enter each hour—the counter resets exactly at 1:00, 2:00, 3:00, regardless of when those 20 people actually arrived.

## What Is a Sliding Window Log?

A sliding window log is a rate limiting method where you keep a timestamp record of every request, and when a new request arrives, you check how many timestamps fall within the past time window and reject if it exceeds the limit. This matters because it's very accurate and handles edge cases that fixed windows miss, so it's useful when you need precise rate limit enforcement. The tradeoff is memory usage, but that's beyond the scope of this introduction. Imagine a ticket gate that remembers the exact time of every person who entered in the last 60 minutes—to let the next person through, you just count the tickets from the past hour.

## What Is a Sliding Window Counter?

A sliding window counter is a rate limiting method that estimates request counts by combining data from two adjacent fixed windows, weighted by how much of the current window has elapsed. This matters because it's more accurate than fixed window counters while using much less memory than sliding window logs, making it a practical middle ground for many systems. Think of it as a hybrid: you track requests per hour, but when checking limits mid-hour, you blend the current hour's count with a fraction of the previous hour's count to smooth out boundary effects.

## Why Do We Need Distributed Rate Limiting?

Distributed rate limiting is the practice of enforcing rate limits across multiple backend servers instead of on each server independently. This matters because modern systems often run many server instances behind a load balancer, and if each server enforces limits separately, a user can bypass limits by spreading requests across servers. For example, if each of your five web servers allows 100 requests per minute, a determined user could actually send 500 requests per minute by hitting all five servers—distributed rate limiting prevents this by coordinating limits across all servers, typically using a shared data store like Redis.

## What Are Per-Tenant Rate Limits?

Per-tenant rate limits are separate rate limit quotas applied to individual customers or users rather than global limits for everyone. This matters because different customers may have different needs and willingness to pay, and fair resource sharing requires preventing one customer from monopolizing your system at others' expense. If you're a SaaS API, you might allow your free tier users 100 requests per minute, paid users 10,000 per minute, and enterprise customers unlimited—each customer's requests count against only their own limit, not a shared pool.
