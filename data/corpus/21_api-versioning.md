# API Versioning

## URI Path Versioning

URI path versioning embeds the API version directly in the URL path, such as `/v1/users` or `/v2/products`. This is the most visible and explicit versioning strategy, making the version immediately apparent to API consumers.

The primary advantage is clarity—clients cannot accidentally use the wrong version. Version information is also available in server logs and analytics without additional parsing. However, URI versioning can clutter URL structures and make routing more complex at the application level.

When implementing URI versioning, establish clear patterns early. For example, use `/v{major}/resource` consistently across all endpoints. Ensure that version numbers in the path correspond to semantic versioning releases. A common tradeoff is that different versions may require separate code paths or middleware branching logic, increasing maintenance overhead.

Example: A payment service might expose `/v1/charges` for legacy clients and `/v2/charges` for new integrations, allowing both to coexist during transition periods.

## Header-Based Versioning

Header-based versioning places version information in HTTP request headers, typically using a custom header like `X-API-Version: 2` or the `Accept` header with a vendor-specific media type such as `application/vnd.company.v2+json`.

This approach keeps URLs clean and consistent, which is valuable for caching and CDN strategies since different versions share the same URL. However, the version is invisible in the URL itself, making it harder to discover or debug. Clients may inadvertently use incorrect versions if they forget to set headers correctly.

Header versioning works well in scenarios where you control most clients (internal APIs) or when versions are closely related and backward-compatible. It requires robust documentation and client library support to prevent version confusion.

Example: A mobile app might send `Accept: application/vnd.api.v3+json` to receive response schema version 3, while the same URL serves different response structures based on the header.

## Semantic Versioning for APIs

Semantic versioning (SemVer) applies the MAJOR.MINOR.PATCH versioning scheme to APIs, where MAJOR indicates breaking changes, MINOR indicates backward-compatible new features, and PATCH indicates backward-compatible bug fixes.

Applying SemVer to APIs provides a standardized way for clients to understand compatibility. Tools and package managers rely on semantic versioning to manage dependencies automatically. However, APIs have ambiguities that SemVer doesn't address—removing a field might not technically break backward-compatible clients that ignore extra fields, but adding required request fields breaks forward compatibility.

When adopting semantic versioning, define clearly what constitutes a breaking change for your specific API. Document whether removed fields, changed response structures, or modified behavior require major version increments. This clarity is essential because different teams may have different interpretations.

Example: Version 2.1.0 adds an optional `metadata` field (minor bump), while version 3.0.0 removes the `legacy_id` field (major bump).

## Breaking Changes Definition

A breaking change is any modification to an API contract that forces clients to update their code to maintain functionality. Common breaking changes include removing endpoints, changing response field types, removing required response fields, adding required request parameters, or altering the behavior of existing functionality.

Defining breaking changes clearly is foundational to versioning strategy. Without consensus on what constitutes a breaking change, teams cannot coordinate deprecation timelines or version rollout strategies effectively. Some changes appear breaking but aren't—adding optional fields or optional request parameters generally don't break existing clients.

Establish written criteria for your API. Document whether field reordering counts as breaking (it shouldn't for JSON), whether status code changes are breaking (typically yes), and whether performance degradation counts (usually no, but consistency matters). Share this definition across teams so all stakeholders understand the implications of proposed changes.

Example: Changing `GET /api/v1/user/{id}` response type from `{"age": 25}` to `{"age": "25"}` (integer to string) breaks typed clients expecting an integer.

## Non-Breaking Changes

Non-breaking changes are modifications that don't require clients to update code. These include adding new optional request parameters, adding new response fields, adding new endpoints, and modifying internal behavior that doesn't affect external contracts.

Non-breaking changes enable continuous API improvement without forcing widespread client updates. They allow you to extend functionality while maintaining compatibility with existing consumers. However, clients sometimes assume responses are complete and unchanging—adding new fields in responses can occasionally cause issues with strict schema validators.

When making non-breaking changes, communicate them clearly even though they don't require immediate client action. Clients may want to adopt new fields or parameters to take advantage of improvements. Use minor version increments to signal availability of new optional features.

Example: Adding `POST /api/v2/users/{id}/preferences` as a new endpoint doesn't break existing clients using other endpoints, and adding an optional `?include_metadata=true` query parameter doesn't affect clients omitting it.

## Deprecation Policies

A deprecation policy establishes the timeline and process for retiring old API versions or endpoints. It communicates to clients when support ends and enforces orderly migration to newer versions.

Clear deprecation policies reduce surprise breaking changes and allow organizations to plan resource allocation for maintaining multiple versions. Without explicit policies, clients continue using old versions indefinitely, forcing API maintainers to support many versions simultaneously.

A typical deprecation policy specifies minimum notice periods (commonly 6-24 months), communication channels (documentation updates, email notifications, dashboard warnings), and hard cutoff dates. Include which versions are actively supported, which are in deprecation windows, and which are retired. Enforce policies consistently—if you announce a retirement date, honor it or your credibility suffers.

Example: A policy might state "endpoints in deprecation status are supported for 12 months from the announcement date, with warnings sent to active consumers at month 6, 9, and 11."

## Version Sunset Headers

Version sunset headers communicate deprecation status using standard HTTP headers, typically the `Sunset` header (RFC 8594) or custom headers like `Deprecation` and `Sunset-Expires`. These headers indicate when an API version will no longer be available.

Using standard headers enables clients to automatically detect deprecation. Well-behaved client libraries can warn developers or raise alarms when they're using endpoints near retirement. Sunset headers also document the deprecation timeline in the same communication channel (HTTP) as the actual API.

The tradeoff is that many clients don't inspect these headers, so they serve as supplementary communication rather than primary notification. Implement sunset headers as part of a multi-channel deprecation strategy including documentation, emails, and dashboard warnings.

Example: A response includes `Sunset: Wed, 31 Dec 2025 23:59:59 GMT` and `Deprecation: true` to indicate this endpoint version will be removed on that date.

## Multiple Concurrent Versions

Supporting multiple concurrent API versions means maintaining separate code paths, databases schemas, or response formatters for different versions simultaneously. This allows clients to migrate at their own pace rather than forcing simultaneous upgrades.

Concurrent version support improves stability and adoption—clients aren't forced to make breaking migrations and risk breaking their own systems. However, maintaining multiple versions multiplies testing, documentation, and bug-fix efforts. Each version must receive security patches, creating operational complexity.

Establish clear limits on concurrent versions. Most APIs support 2-3 versions simultaneously. Define when older versions reach end-of-life. Use feature flags, middleware routing, or separate deployment pipelines depending on how different your versions are.

Example: Supporting v1, v2, and v3 simultaneously means three sets of endpoint implementations, tests, and documentation. When a security vulnerability is discovered, all three versions require patches.

## Backward Compatibility Strategies

Backward compatibility ensures that new API versions don't break existing clients. Strategies include adding features additively (new optional fields), maintaining old endpoints alongside new ones, and using response envelope patterns that allow extensions.

Backward compatibility reduces friction when deploying new versions. Clients continue functioning even if they don't upgrade immediately. However, achieving backward compatibility constrains API design—you cannot remove fields, change types, or restructure responses without planning migration paths.

Use these compatibility patterns: optional request fields, additive response fields (clients ignore unknown fields), new separate endpoints alongside old ones, and versioned response envelopes. Document which fields are guaranteed stable across versions.

Example: Version 2.0 adds an optional `timezone` parameter to `POST /users`, which v1 clients can omit, and adds a new `created_at` response field that v1 clients can safely ignore.

## Additive Field Extensions

Additive field extensions allow APIs to add new response fields without breaking existing clients. When an API returns new fields that were previously absent, well-designed clients simply ignore them.

This pattern works because JSON and similar formats allow clients to receive objects with additional properties than they expect. Clients parsing responses typically extract needed fields and ignore others. This enables servers to evolve responses incrementally.

The constraint is that you cannot remove fields (breaking), change field types, or restructure nested objects using only additive extensions. For more substantial changes, new major versions are required. Document which fields clients should consider stable and which may appear or disappear.

Example: A user response changes from `{"id": 1, "name": "Alice"}` to `{"id": 1, "name": "Alice", "email_verified": true}`. Existing clients continue functioning because they extract only `id` and `name`.

## Envelope Versioning Patterns

Envelope versioning wraps API responses in a container structure that includes version metadata, allowing multiple response formats to coexist. A typical envelope includes `{version, data, metadata}` where the `data` payload changes per version.

This pattern provides explicit version information in responses and allows serving different data structures in the same response envelope. Clients can inspect the version field and parse accordingly. However, envelopes add response payload overhead and increase complexity.

Envelopes work well for APIs with dramatic differences between versions or when you need explicit version declaration in responses. For simpler versioning needs, omit envelopes and rely on URI or header versioning.

Example: A response might be `{"version": "2", "data": {...}, "meta": {"timestamp": ...}}` where different versions contain different data structures inside the envelope.

## Transparent Versioning

Transparent versioning hides version complexity from clients by automatically routing requests to appropriate implementation versions without explicit version specification in URIs, headers, or other mechanisms.

This approach uses server-side logic to determine which version to serve based on client characteristics, user account settings, or gradual rollout percentages. Clients never specify versions and remain unaware of multiple implementations running in parallel.

Transparent versioning requires sophisticated backend infrastructure to manage routing rules and feature flags. It complicates debugging and visibility—clients don't know which version they're using. Use this approach only when client-side version awareness adds no value and you can manage rollout entirely server-side.

Example: Based on a user's account creation date or a feature flag, requests automatically receive responses from either the v1 or v2 implementation, with clients unaware of the distinction.

## API Gateway Versioning

API gateways act as single entry points that route requests to versioned backend services. Gateways can implement versioning logic, request/response translation, and rate limiting per version.

This architecture decouples clients from knowing backend service details. Gateways translate between client requests and backend service APIs, allowing backend changes without affecting clients. However, gateways introduce a potential bottleneck and single point of failure.

Use API gateways when you have heterogeneous backend services with different versioning schemes, or when you need centralized versioning logic, request transformation, or version-specific rate limits. Document the gateway's versioning behavior clearly since it mediates all client-server communication.

Example: A gateway receives `/v2/orders` from a client and routes it to the `orders-service:v2` backend, translating the response format before returning to the client.

## Content Negotiation for Versioning

Content negotiation uses the HTTP `Accept` header to specify desired response formats or versions. Servers parse the `Accept` header and return content in the requested format, potentially implementing multiple versions through content negotiation.

Content negotiation is standards-compliant and allows serving multiple formats from the same URL. However, it's invisible in URLs and requires clients to set headers correctly. Content negotiation can also complicate caching since the same URL returns different content based on headers.

Implement content negotiation carefully—ensure defaults are sensible and documentation is clear. Combine with other versioning signals when appropriate. This works well for API versioning when you control clients or have sophisticated client libraries handling negotiation.

Example: A request with `Accept: application/vnd.api+json;version=3` might receive a version 3 response, while `Accept: application/vnd.api+json;version=2` receives version 2.

## Versioning Query Parameters

Query parameter versioning places version information in the query string, such as `/api/users?version=2`. This keeps the base path clean while making version visible in the full URL.

Query parameter versioning is more flexible than URI path versioning—you can implement defaults or change versions on the fly. However, query parameters are often considered less significant than path structure, and some frameworks treat them as filters rather than routing signals.

Use query parameter versioning when versions are minor variations or when you want to support version negotiation alongside path routing. Ensure caching behavior is correctly configured for different versions accessed via the same path with different query parameters.

Example: `GET /api/products?api_version=2&include=pricing` specifies both the API version and additional request parameters in the query string.

## Client Library Version Management

Client libraries encapsulate API versioning logic, handling URL construction, header management, and response parsing for a specific API version. Libraries can target single versions or support multiple versions with branching logic.

Well-designed client libraries abstract versioning complexity from application developers. However, maintaining client libraries across multiple versions creates additional work. Users must ensure they're using libraries compatible with their target API versions.

Provide client libraries for frequently-used languages and clearly document version support. Include upgrade guides when libraries support multiple versions. Consider generated client libraries using OpenAPI specifications to reduce manual maintenance.

Example: A JavaScript client library `api-client@2.0.0` automatically constructs `/v2/` URLs and includes version-specific request/response handling, while developers simply call `client.getUser(id)`.

## Versioning Documentation Strategies

API documentation must clearly communicate available versions, their features, differences, and deprecation status. Documentation should maintain separate sections per version or clearly mark version-specific information.

Poor documentation about versions causes client confusion and support burden. Clients need to know which version to start with, how versions differ, when versions are deprecated, and how to migrate between versions.

Implement documentation as follows: list all supported versions prominently, provide separate endpoint documentation per version (or clearly mark version-specific behavior), include migration guides for major versions, and automate version deprecation notices. Use tools like OpenAPI/Swagger to generate version-specific documentation from specifications.

Example: Documentation includes a "Supported Versions" section listing v1 (deprecated, sunset Dec 2025), v2 (stable), and v3 (latest), with separate endpoint reference pages for each.

## Monitoring and Analytics Per Version

Monitoring API usage by version reveals adoption patterns, helps prioritize support resources, and provides early warning when deprecated versions still have active users.

Collect metrics separately per API version: request counts, error rates, response times, and client information. This data informs deprecation decisions—if a version still has significant traffic, sunset dates need adjustment. Alert when deprecated versions receive unexpected traffic spikes.

Implement version tracking in logging, metrics, and tracing systems. Extract version from URIs, headers, or routing logic and include it as a dimension in all metrics. Dashboard alerts should notify when deprecated versions exceed usage thresholds unexpectedly.

Example: Dashboards show v1 receiving 5% of traffic (mostly legacy clients), v2 receiving 30%, and v3 receiving 65%, informing decisions about v1 retirement timeline.

## Compatibility Testing Across Versions

Testing must verify that backward-compatible changes don't break clients expecting older behavior, and that new versions correctly handle old client requests. Test suites should include tests per supported version.

Backward-compatibility testing is easy to neglect but critical. Without it, you might ship changes claiming to be non-breaking that actually break specific client patterns. Test both that old clients work with new servers and that new clients work with old servers (if applicable).

Implement test matrices covering supported version combinations. Include contract tests verifying response schemas per version. Use property-based testing to verify that changes don't affect existing client parsing patterns.

Example: Test matrix verifies that v1 clients continue working when servers deploy v2, and that v2 clients continue working when connecting to v2 servers, catching unexpected compatibility issues.

## Versioning Rollout Strategies

Versioning rollout strategies manage deployment of new versions, including canary releases (new version to small user percentage), feature flags (new version behind toggles), and blue-green deployments (entire version swapped atomically).

Careful rollout reduces risk—deploying a new version to 100% of traffic immediately risks widespread outages. Gradual rollouts let you detect and fix issues affecting small populations before widespread impact.

Combine rollout strategies with monitoring. Start new versions at 1-5% traffic, watch for error rates and latency changes, and gradually increase percentage. Maintain ability to quickly rollback to previous versions. Use feature flags to control version behavior independent of deployment, allowing quick toggles without redeployment.

Example: Deploy v3 to 5% of traffic for 1 hour (monitoring closely), increase to 25% for 4 hours, then 100%. If errors exceed thresholds at any stage, immediately rollback to v2.
