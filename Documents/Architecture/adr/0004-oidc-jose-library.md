# ADR 0004: OIDC relying party on the shared upstream client, JOSE with `joserfc`

Status: accepted (2026-11-09, approved by the owner).

## Context
ARCHITECTURE §2 names `authlib` for OIDC. T-A02 needs two things from a library: verifying ID tokens (JWS, JWK sets)
and the OAuth 2.0 authorization code flow. Two facts changed the picture:

- authlib's JOSE module (`authlib.jose`) is deprecated by its maintainers in favour of `joserfc`, a separate package
  from the same project (authlib org, same author, BSD-3-Clause, depends only on `cryptography`).
- authlib's HTTP clients are its own `httpx.AsyncClient` subclasses. Waystation's rule is that every outbound call
  to a third party goes through the server's pooled upstream clients and the SSRF guard (ARCH §6.1, CLAUDE.md
  "never follow redirects to private address space"); authlib's client would bypass both.

## Decision
- ID token signature checks and JWK parsing use `joserfc` (`jwt.decode` with an explicit single-algorithm
  allowlist, `JWKRegistry.import_key`). Algorithm policy, key selection (kid, key type, curve, the key's own `alg`),
  claim checks and JWKS caching/rotation are Waystation code in `waystation_server.security.oidc`.
- The code flow (discovery, authorization request with state + nonce + PKCE S256, token request, userinfo,
  RP-initiated logout URL) is implemented in `security.oidc` / `oidc_auth` on `UpstreamClients` through a small
  `Http` seam (also the test seam). No authlib dependency.
- ARCHITECTURE §2 would read: "`joserfc` (OIDC ID tokens; the OAuth flow is in-house on the upstream client)"
  instead of "`authlib` (OIDC)".

## Consequences
- One dependency (`joserfc`), no deprecated module, every IdP request SSRF-checked, size-bounded and time-limited.
- The flow code is ours to maintain (about 300 lines); it is covered by unit tests with an in-process provider and
  by integration tests against Keycloak and authentik.
- If not approved: swap `joserfc` for `authlib.jose` (same primitives) behind the same functions; the flow code
  stays on the upstream client either way.
