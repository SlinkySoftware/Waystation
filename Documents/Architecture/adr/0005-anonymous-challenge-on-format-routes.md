# ADR 0005: Basic challenge instead of 404 for anonymous requests to hidden repositories (format routes)

Status: Proposed — needs the owner's approval (raised by T-P5-MAVEN, 2026-11-10). Nothing is implemented; the code
follows ARCHITECTURE §7 as written.

## Context
ARCHITECTURE §7 / CLAUDE.md: a repository the principal may not see answers `404` (hidden objects never leak). The
default anonymous role reads proxy repositories only, so an anonymous request to a hosted or group repository gets
`404 Not Found`.

Several format clients send credentials only after a `401` with `WWW-Authenticate: Basic`: Maven and Gradle by
default (Maven sends basic auth preemptively only for `PUT`), sbt/Coursier, Helm classic, pip for index URLs without
inline credentials. Against Waystation they never see the challenge: a `GET` of a hosted or group repository with
correctly configured `settings.xml` credentials fails with `404` (Maven: "No versions available", "Could not find
artifact"). Today the workaround is preemptive authentication on the client (Maven:
`-Daether.connector.http.preemptiveAuth=true`, e.g. in `.mvn/maven.config`; Gradle: `authentication {
create<BasicAuthentication>("basic") }`), documented in `Documents/Operations/clients/maven.md` and used by the Maven
client harness.

Nexus answers `401` (Basic challenge) to anonymous requests for repositories anonymous cannot read, so existing
Nexus users' client configurations rely on the challenge.

## Decision (proposed)
On `/repository/<repo>/...` only, an **anonymous** request (no credentials at all) to a repository the anonymous
principal cannot see answers `401` with `WWW-Authenticate: Basic realm="Waystation"` instead of `404`. Authenticated
principals keep the `404` for hidden repositories, and everything outside the format routes (REST API, `/v2/`,
search, browse) is unchanged. Implemented in `security.access.require_repo` (one decision point) behind the router's
format-route path, with a test in the route-enumeration suite.

## Consequences
- Maven, Gradle, sbt, Helm and pip work with plain credentials, like with Nexus; no client-side preemptive switches.
- An anonymous caller can tell "exists but needs credentials" (`401`) from "does not exist" (`404`) for repository
  names on the format routes, i.e. repository names can be enumerated anonymously. Nexus has the same property;
  repository names are not secret in most deployments, but this weakens §7 for anonymous callers.
- Alternative: keep §7 and require preemptive authentication on clients (status quo), documented per format.
