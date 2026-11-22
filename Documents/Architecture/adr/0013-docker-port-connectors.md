# ADR 0013: Docker dedicated port connectors on the web tier, named by a web-tier header

Status: accepted (2026-11-22, owner: pre-approved for the v1.0 build). Proposed 2026-11-22 with T-P6-DOCKERC.

## Context
Nexus serves a docker repository on a whole host:port of its own (a "connector"): `docker pull
nexus.example.com:8082/library/alpine` reaches one repository without a prefix. Waystation routes `/v2/<repo>/...`,
upstream aliases and one default repository (SPECIFICATION §4.4, ADR 0003), so a second unprefixed entry point needs a
client change. T-P6-DOCKERC adds Nexus-style connectors so clients that need a whole host:port keep working.

Three facts constrain the design:

- Only the web container publishes ports (ARCHITECTURE §1), so the listeners belong there.
- The web tier holds no state, no credentials and no business logic, and never talks to MongoDB or Valkey. It cannot
  look up which repository owns a port.
- The server sees every request through the web tier on one port; it must learn which listener a request arrived on.

Options for how the web tier learns its ports:

1. **An environment list on the web container** (`WAYSTATION_DOCKER_CONNECTORS=5001,5002`), rendered into nginx
   `server` blocks at start by `15-waystation.sh`. Static and stateless, like the TLS settings. Publishing the ports
   in compose is operator work anyway, so the list sits next to the `ports:` entries it needs.
2. **An internal server endpoint** the web entrypoint polls, regenerating and reloading nginx. The web tier would
   depend on the server at start, run a reload loop in a read-only container, and the ports still need publishing by
   hand: the automation saves nothing. Rejected.

Options for how the server learns the connector:

1. **Repository name in a header** set by nginx. Needs the port → repository map in the web tier (state). Rejected.
2. **Port number in a header** set by nginx (`X-Waystation-Connector: <port>`); the server maps the port to the
   docker repository whose `format_options.connector_port` it is. The web tier knows only port numbers.
3. **Host header / `$server_port` derived on the server.** The server cannot tell connector ports from the main port
   behind NAT or an external proxy. Rejected.

## Decision
- A docker repository (proxy, hosted or group) may set `format_options.connector_port` (1024-65535, unique across
  repositories, not the base URL's port; a clone starts without it).
- The web container renders one nginx `server` per port in `WAYSTATION_DOCKER_CONNECTORS` (validated at start: port
  numbers, 1024-65535, not the web ports, no duplicates). It answers `/v2` and `/v2/` (proxied with the shared proxy
  settings) and the internal `/_blobs/` location (X-Accel); everything else is 404. With TLS configured it speaks
  HTTPS + HTTP/2 on the same certificate, else plain HTTP. nginx sets `X-Waystation-Connector: <port>` there, which
  replaces any client copy; the main server sets it empty, so nginx never passes a client's copy.
- The server honours the header only when the direct peer is an internal hop (loopback or private address, the same
  rule as `X-Real-IP`, `clientip`). On a connector every `/v2/<name>` resolves to that repository with the whole name
  (no prefix, alias or `ns`); a port no repository has (or a malformed value) resolves nothing (404). `/v2/_catalog`
  lists that repository's images, unprefixed.
- Token flow: the challenge's realm is `<base URL>/auth/token?connector=<port>` (like `?ns=` for containerd mirrors).
  The token service resolves scopes through that repository and renames a granted scope `<repository>/<image>`, so
  the token works on any listener and authentication of `/v2/` needs no connector context. The token service itself
  stays on the main listener.
- The connector only *selects* a repository, exactly as a `<repository>/` prefix does. RBAC, token grants and hidden
  repository rules are unchanged, so neither a forged header (from an internal container) nor a forged `connector`
  query parameter grants anything a prefix would not.
- Nexus import maps a connector's `httpsPort` (else `httpPort`) to `connector_port`; the other port, subdomain
  connectors, ports below 1024 and ports already used are reported as differences.

## Consequences
- Operators list the ports in `WAYSTATION_DOCKER_CONNECTORS`, publish the same numbers on the host, and set
  `connector_port` on the repositories. A port in the list without a repository answers 404 for every name; a
  repository's port missing from the list is not reachable. The UI and "Set me up" say so; the server cannot check
  what the web tier listens on.
- The connector host:port clients use is the base URL's host with the connector port: publish the same number, or
  the "Set me up" snippets name a port clients cannot reach.
- `docker login` is per host:port: a connector needs its own login (the credentials go to the base URL's realm).
- The web tier gains a second kind of `server` block; the proxy settings and the `/_blobs/` location moved to shared
  include files so both kinds stay identical. `make web-config-test` renders and checks every combination in the
  official nginx image.
