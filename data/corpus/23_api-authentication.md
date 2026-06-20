# API Authentication

## API Key Fundamentals

An API key is a unique identifier token issued to a client for authentication purposes. The client includes this key in requests (typically via header, query parameter, or body) to authenticate themselves to the API. API keys are stateless from the server perspective—validation requires checking the key against a stored registry.

API keys work well for server-to-server communication, third-party integrations, and public APIs where client identity matters but fine-grained permission control is secondary. They're simpler to implement than OAuth2 but offer less security if compromised since they don't expire by default and don't support granular scopes.

Tradeoffs: API keys are easy to implement and debug but harder to rotate at scale, cannot be revoked per-session, and expose the same credentials across all requests. A leaked key grants full access until manually revoked.

Example: A payment processor issues a 40-character API key to a merchant. The merchant includes it in the Authorization header for each transaction. The processor validates the key exists and is active before processing.

## API Key Rotation Strategies

Key rotation involves systematically replacing old keys with new ones to limit exposure if a key is compromised. Common patterns include issuing multiple valid keys simultaneously during a transition window, deprecating old keys after a grace period, and automating rotation on a schedule.

Implement rotation by supporting multiple active keys per client during transition. Provide tooling for clients to generate new keys before old ones expire. Log all key usage to detect anomalies during rotation windows. Communicate rotation requirements clearly to clients with adequate notice periods (typically 30-90 days).

Challenges include coordinating across many clients, detecting keys still in use before deprecation, and handling clients who don't rotate proactively. Some systems implement mandatory rotation by incrementally invalidating keys, monitoring for increased error rates from clients using revoked keys.

## API Key Scope and Permission Binding

Scopes define what actions an API key can perform. Rather than a single key with full access, issue keys with restricted capabilities: read-only, specific resources, or particular operations. Store scope information alongside the key in your registry.

Scoped keys reduce blast radius if compromised. A key limited to read-only operations cannot delete data. A key scoped to a specific resource cannot access other customers' data. This follows the principle of least privilege.

Implementation requires maintaining a mapping of key → scopes, then checking requested operation against scopes before processing. The granularity depends on your API design. Fine-grained scopes (per resource and operation) offer better security but complexity in management. Coarse scopes (read/write/admin) are simpler but less secure.

## Stateless vs Stateful API Key Validation

Stateless validation checks cryptographic properties of the key itself (similar to JWT validation) without database lookup. Stateful validation queries a registry to confirm the key exists and is active.

Stateless keys reduce database load and latency but require secure key generation algorithms and cannot be instantly revoked. Stateful keys allow instant revocation and easier permission updates but add lookup overhead to every request.

Most API key systems use stateful validation for operational simplicity—revocation and permission changes take effect immediately. High-throughput systems sometimes cache validation results with TTLs to reduce lookups.

## OAuth2 Authorization Code Flow

The Authorization Code flow is the primary OAuth2 flow for web applications. The user authorizes the application at the authorization server, which issues an authorization code. The application exchanges this code for an access token via a backend channel, keeping the token secret.

Use this flow when you control a web application and can securely store tokens server-side. The user never sees the access token, reducing exposure risk. The authorization server can revoke the authorization code immediately after use.

Flow steps: (1) User clicks "Login with Provider" on your app, (2) App redirects to authorization server, (3) User logs in and approves scopes, (4) Authorization server redirects back with authorization code, (5) App's backend exchanges code for access token using client credentials, (6) App uses access token to call API on behalf of user.

## OAuth2 Implicit Flow Deprecation

The Implicit flow was designed for single-page applications and mobile apps that cannot securely store client secrets. The authorization server returns an access token directly in the URL fragment after user authorization, eliminating the backend exchange step.

This flow is deprecated in OAuth2 security best practices because returning tokens in URL fragments exposes them to browser history, proxy logs, and JavaScript libraries. Modern SPAs should use the Authorization Code flow with PKCE instead.

If maintaining legacy Implicit flow implementations, implement short token lifetimes, restrict scopes, and encourage migration to PKCE-protected flows. Some systems still use Implicit for public clients with minimal security requirements, but this is increasingly discouraged.

## OAuth2 Client Credentials Flow

The Client Credentials flow issues access tokens directly to a client based on client credentials (ID and secret), without user involvement. The client authenticates itself and receives a token scoped to its own capabilities.

Use this for server-to-server communication and background processes. There is no user context—the token represents the service itself. This is appropriate for administrative tasks, cron jobs, and inter-service APIs within your infrastructure.

Client credentials require secure storage of the client secret. Implement secret rotation, audit all token issuances, and monitor for unusual patterns. Tokens should be short-lived and refreshed per request or session. This flow does not support delegation or impersonation—tokens represent the service's own identity.

## OAuth2 Resource Owner Password Credentials Flow

The Resource Owner Password Credentials flow allows the client to collect the user's username and password directly, then exchange them for an access token. This flow has fallen out of favor due to security concerns.

Only use this flow in situations where user trust is high and other flows are impossible—for example, legacy applications or first-party clients where collecting credentials directly is unavoidable. The user's password should never be stored; it's exchanged once for a token.

Modern systems avoid this flow. It trains users to enter credentials into non-official applications (reducing phishing awareness), requires securely handling raw passwords, and provides no way to revoke individual client access without changing the password. Use Authorization Code with PKCE instead.

## OAuth2 Refresh Token Usage

A refresh token is a long-lived credential that can be exchanged for new access tokens without re-authenticating the user. Access tokens are short-lived; when they expire, the client uses the refresh token to obtain a new access token.

Refresh tokens allow balancing security (short access token lifetimes) with usability (users don't re-authenticate frequently). If an access token is compromised, damage is limited to its short lifetime. If a refresh token is compromised, the attacker has longer-term access.

Store refresh tokens securely and implement rotation: issue a new refresh token with each exchange, invalidating the old one. Monitor for refresh token reuse (sign of token theft). Implement refresh token expiration (typically weeks to months) independent of access token expiration. Allow users to revoke refresh tokens explicitly.

## JWT Structure and Composition

A JSON Web Token (JWT) is a compact, URL-safe format for representing claims between parties. It consists of three base64url-encoded parts separated by periods: Header.Payload.Signature. The header specifies the algorithm, the payload contains claims (statements about the subject), and the signature provides integrity verification.

Example JWT (simplified): `eyJhbGc...` (header) `.eyJzdWI...` (payload) `.SflKx...` (signature)

The payload typically contains standard claims (sub for subject, iss for issuer, aud for audience, exp for expiration, iat for issued time) and custom claims relevant to your application. The signature is computed over the header and payload using the specified algorithm and a secret key or public key pair.

JWTs are stateless—the server doesn't store them, only validates the signature. This enables horizontal scaling since any server can validate any token. However, JWTs cannot be instantly revoked; revocation requires token blacklisting or waiting for expiration.

## JWT Signing Algorithms

JWTs can be signed with symmetric algorithms (HS256, HS384, HS512 using HMAC) or asymmetric algorithms (RS256, RS384, RS512 using RSA; ES256, ES384, ES512 using ECDSA). The algorithm is specified in the JWT header.

Symmetric algorithms use a shared secret key for both signing and verification. They're faster but require the secret to be shared with all verifying parties. Use symmetric signing for internal tokens where all servers share the secret.

Asymmetric algorithms use a private key for signing and a public key for verification. The public key can be shared widely (in JWKS endpoints, for example). Use asymmetric signing when tokens must be verified by external services or when different services need different trust domains. The public key distribution overhead is worthwhile for security boundaries.

## JWT Expiration and Validation

JWTs include an `exp` (expiration time) claim indicating when the token is no longer valid. Validation requires checking the current time against this claim. Setting appropriate expiration times is critical to limiting token lifetime and attack surface.

Short-lived access tokens (5-15 minutes) minimize damage if compromised but require frequent refresh. Longer-lived tokens (hours to days) reduce refresh overhead but increase exposure. Balance security and usability for your use case.

Validation must also check the `nbf` (not before) claim, verify the signature, confirm the issuer and audience match expectations, and check token blacklists if revocation is required. Never trust the payload without validating the signature—the payload is visible but unverified without signature validation.

## JWT Claims Design

Claims are key-value pairs in the JWT payload. Standard claims (registered claims) include `sub`, `iss`, `aud`, `exp`, `iat`, `nbf`. Custom claims (private claims) represent application-specific data like user ID, permissions, or organization membership.

Design claims to be minimal and immutable within the token lifetime. Include only data necessary for immediate authorization decisions—large payloads increase token size and don't change during the token's life. For mutable data (like user permissions), store the actual state server-side and reference it with a stable identifier in the token.

Example claims for an access token: `{"sub": "user123", "iss": "auth.example.com", "aud": "api.example.com", "scope": "read write", "org": "acme", "exp": 1234567890}`. The API validates these claims and makes authorization decisions accordingly.

## JWT Bearer Token Usage

JWT bearer tokens are transmitted in the Authorization header using the Bearer scheme: `Authorization: Bearer <token>`. This is the standard HTTP mechanism for token-based authentication.

The server extracts the token from the header, decodes it, validates the signature and claims, and processes the request. The header is the preferred location because it's standard HTTP and not logged as aggressively as query parameters.

Use HTTPS exclusively when transmitting bearer tokens—HTTP exposes them in plain text. Implement token rotation in clients to limit exposure of long-lived tokens. Log all token validation failures to detect tampering or misuse, but avoid logging full tokens in error messages.

## Mutual TLS (mTLS) Fundamentals

Mutual TLS (mTLS) extends standard TLS by requiring both client and server to authenticate via X.509 certificates. The client verifies the server's certificate (standard TLS), and the server verifies the client's certificate (mutual authentication).

mTLS provides strong cryptographic authentication for both parties, binding requests to specific client certificates. Unlike tokens, certificates are revocable via certificate revocation lists (CRL) or the Online Certificate Status Protocol (OCSP), enabling instant revocation.

mTLS is appropriate for high-security inter-service communication, regulated industries, and scenarios where strong mutual authentication is required. Implementation complexity is higher than token-based auth—certificate management, distribution, and rotation infrastructure is required. Every request incurs TLS handshake overhead (though connection reuse mitigates this).

## mTLS Certificate Management

Managing client certificates at scale requires infrastructure: certificate issuance, storage, rotation, and revocation. Organizations typically use an internal PKI (Public Key Infrastructure) or a service like Kubernetes-native cert-manager.

Generate unique certificates per client and rotate them regularly (typically yearly). Before expiration, issue new certificates and update clients. During rotation windows, servers accept both old and new certificates. Monitor certificate expiration to prevent service outages from expired certs.

Revocation requires checking certificate status via CRL or OCSP. CRL has scaling challenges with large numbers of certificates; OCSP is more scalable but adds per-request latency. Many systems cache revocation status with short TTLs as a compromise.

## mTLS vs Token-Based Authentication

Token-based authentication (API keys, OAuth2, JWT) is stateless, lightweight, and suitable for user-facing APIs. mTLS is stateful (requires certificate infrastructure), heavyweight, and suited for service-to-service authentication.

Tokens are transmitted in application-layer headers and are flexible—different scopes, permissions, and expirations can be expressed. mTLS authenticates at the transport layer, providing only binary client identity (certificate is valid or not).

Hybrid approaches are common: use TLS for transport security (HTTPS), tokens for application-layer authentication (Bearer tokens), and mTLS for critical service boundaries. The choice depends on threat model, scalability requirements, and operational overhead tolerance.

## Session-Based Authentication

Session-based authentication stores authentication state server-side. The client authenticates (usually with username/password), receives a session identifier (typically a cookie), and includes this identifier with subsequent requests. The server looks up the session to verify the client.

Sessions are stateful—each server must have access to session storage (shared cache, database, or sticky load balancing). They're natural for user-facing web applications with browsers (which handle cookies automatically) but less suitable for distributed systems.

Sessions can be instantly revoked by deleting the session record. They support per-session isolation (each user session is independent). Tradeoffs include scalability challenges, session fixation vulnerabilities, and difficulty implementing across multiple domains.

## Token-Based Authentication Advantages

Token-based authentication (including API keys and JWTs) is stateless from the server perspective. The client includes a token with requests; the server validates it without database lookup. This enables horizontal scaling—any server can validate any token without shared state.

Tokens can be cached or validated with cryptographic signatures (JWTs), reducing latency. They're suitable for APIs serving many clients, microservices, and mobile applications. Token revocation is more difficult (requires blacklists or waiting for expiration), but instant revocation is less critical for many use cases.

Token-based auth is the standard for modern APIs. It scales better than sessions, supports fine-grained permissions (scopes), and works across multiple domains and protocols.

## Authentication Failure Handling

When authentication fails, return a 401 Unauthorized response indicating credentials are invalid, expired, or malformed. Distinguish between 401 (authentication failed) and 403 (authorization failed—credentials are valid but insufficient for the requested resource).

Log all authentication failures to detect attacks and troubleshoot legitimate failures. Include enough context (client identifier, timestamp, endpoint) without exposing sensitive data. Rate-limit failed authentication attempts per client to slow brute-force attacks.

Avoid leaking information in error messages. Generic "Invalid credentials" is better than "Username exists but password is wrong." This prevents user enumeration attacks. Return insufficient detail to guide attackers but enough for legitimate clients to troubleshoot integration issues.
