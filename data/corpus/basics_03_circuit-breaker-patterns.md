# Circuit Breaker Patterns

## What Is a Circuit Breaker?

A circuit breaker is a mechanism that stops your application from repeatedly trying to use a service that's currently broken. When a service fails too many times, the circuit breaker "trips" and starts immediately rejecting requests instead of wasting time trying to connect. This is similar to an electrical circuit breaker in your home: when there's too much current flowing, the breaker switches off to prevent damage. In distributed systems, this protects your application from cascading failures where one broken service brings down everything that depends on it.

## What Are the Three States of a Circuit Breaker?

A circuit breaker moves between three distinct states as your application monitors a service's health. The **closed** state means everything is working normally and requests flow through as usual. The **open** state means the service is failing and the circuit breaker is rejecting all requests immediately. The **half-open** state is a testing phase: after the service has been broken for a while, the circuit breaker lets a few requests through to check if the service has recovered. If those test requests succeed, the breaker closes and normal traffic resumes; if they fail, it opens again. This three-state design prevents your application from blindly hammering a broken service forever.

## How Do Failure Thresholds Determine When to Open a Circuit?

A failure threshold is a rule that decides when the circuit breaker should flip from closed to open. For example, you might set a threshold like "open the circuit if 5 consecutive requests fail" or "open the circuit if 50% of requests fail within the last 100 attempts." These thresholds let you tune how sensitive the breaker is: a low threshold responds quickly to problems but might be too aggressive, while a high threshold avoids false alarms but might delay protection. The threshold you choose depends on how critical the service is and how often you expect small, temporary failures.

## Why Keep Circuits Separate With Bulkheads?

A bulkhead is a pattern where you isolate different parts of your system so that one failure doesn't spread to everything else. In practice, this means using separate circuit breakers for different services or different groups of requests—for example, one circuit breaker for the payment service and another for the recommendation service. If the payment service breaks, its circuit breaker opens, but the recommendation service keeps working normally. The name comes from ships, which have watertight compartments (bulkheads) so that if one section floods, the entire ship doesn't sink. Without bulkheads, a single broken dependency could cause your entire application to fail.

## What's the Difference Between Circuit Breakers and Timeouts?

A timeout is a simpler tool that just stops waiting for a response after a fixed amount of time, while a circuit breaker is smarter about recognizing patterns of failure. A timeout protects individual requests—if a request takes too long, you give up and move on. A circuit breaker protects your entire application by recognizing when a service is repeatedly failing and stopping requests before they even start. You typically use both together: timeouts ensure that individual requests don't hang forever, and circuit breakers stop a flood of requests when a service is consistently broken. Think of timeout as a personal patience limit, and circuit breaker as a team decision to avoid a broken resource.

## What Are Fallback Strategies and When Should We Use Them?

A fallback is an alternative action your application takes when a service is unavailable—instead of failing completely, you return cached data, a default response, or a degraded version of the feature. For example, if your recommendation service fails, you could fall back to showing popular items instead of personalized ones. Fallbacks work well with circuit breakers: when a circuit opens, you don't have to return an error to the user; you can smoothly degrade the experience instead. Not every service has a good fallback (like payments), so you need to decide ahead of time what makes sense for each critical dependency.

## How Do Error Budgets Relate to Circuit Breaker Configuration?

An error budget is the amount of failures your service is allowed to have while still meeting its reliability goals—for example, if you promise 99.9% uptime, your error budget is about 43 minutes per month. Understanding your error budget helps you set reasonable failure thresholds in your circuit breakers. If your error budget is tiny because you promised very high reliability, you might use more aggressive thresholds to open circuits quickly and protect your uptime. If you have a larger error budget, you can be more tolerant of occasional failures. Circuit breakers help you spend your error budget wisely by preventing cascading failures that would burn through it all at once.
