# Bulkhead Isolation

## What Is Bulkhead Isolation?

Bulkhead isolation is a design pattern where you partition a system's resources so that a failure in one part doesn't spread to the rest. The name comes from ship design: compartments (bulkheads) are sealed so a leak in one section doesn't sink the entire vessel. In backend systems, this means separating compute, memory, or network capacity between different workloads or customers. When one component fails or gets overloaded, the isolation prevents it from exhausting shared resources and crashing other components that depend on the same infrastructure.

## Why Should We Separate Thread Pools?

Thread pool isolation means giving different types of work their own dedicated threads rather than sharing a single pool. If all requests—whether fast or slow—compete for the same threads, a slow batch job can starve fast user-facing requests by holding all available threads. By separating them, you ensure that even if one pool is blocked waiting for a slow database query, another pool can still process incoming requests. For example, a web server might use one thread pool for HTTP requests and a separate pool for background cleanup tasks, so a backlogged cleanup job won't delay user responses.

## How Do We Partition Resources by Tenant?

Resource partitioning by tenant means allocating fixed portions of your system's capacity to each customer independently. Instead of all tenants sharing one database connection pool or one cache, you give each a quota: tenant A gets 10 connections, tenant B gets 10 connections. This prevents a single tenant from consuming all available resources and degrading service for others. In a multi-tenant SaaS product, one customer running an expensive analytics query can't drain the connection pool and timeout requests for other customers.

## What Does Failure Containment Mean?

Failure containment is the core benefit of isolation: ensuring that when one component fails, it affects only its own isolated section, not the whole system. When a bulkhead is in place, errors, crashes, or performance degradation in one workload or tenant are trapped within that boundary instead of cascading outward. For instance, if a payment processing service crashes, an isolated payment bulkhead means it doesn't take down the user profile service, product catalog, or search functionality—those keep running normally using their own separate resources.

## What Are the Performance Costs of Isolation?

Isolation requires dedicating resources that might otherwise be shared, which can mean less efficient overall utilization. If you give thread pool A 50 threads and pool B 50 threads, but pool A only needs 30, those 20 threads sit idle instead of helping pool B during its peak load. Memory overhead also increases because isolated components may duplicate caches, buffers, or connection pools rather than sharing a single one. The tradeoff is intentional: you accept slightly lower average efficiency in exchange for reliability and predictable worst-case performance under failure.

## How Do Circuit Breakers Work With Bulkheads?

A circuit breaker is a monitoring mechanism that stops sending requests to a failing component once it detects problems, and bulkheads prevent that failing component from dragging down other parts of the system. Think of them as complementary tools: bulkheads contain the blast radius of a failure, and circuit breakers detect the failure and prevent cascades by quickly failing over. For example, a bulkhead isolates calls to a slow third-party API to their own thread pool, while a circuit breaker monitors those calls and opens after seeing too many timeouts, fast-failing new requests instead of waiting for the API to recover.

## When Should We Add Bulkhead Isolation?

You should add isolation when you have workloads or customers with different reliability requirements or failure modes sharing the same resources. Common signals include: one type of request consistently slower than another, multi-tenant systems where one customer's traffic could affect others, or critical services alongside non-critical background work. Start by identifying which failures would be most damaging—those components are good candidates for their own isolated resource pools. Don't isolate everything upfront; add bulkheads based on actual failures you've observed or realistic scenarios you want to guard against.
