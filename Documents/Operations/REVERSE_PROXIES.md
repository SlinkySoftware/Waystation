# Reverse proxies in front of Waystation

The Waystation `web` container listens on plain HTTP (`WAYSTATION_HTTP_PORT`, default 8080). In production you put
a TLS-terminating reverse proxy in front of it. This page lists what that proxy must do and gives working
configurations for nginx, Traefik, Caddy and HAProxy, plus notes for Synology DSM. Deployment itself is in
[DEPLOYMENT.md](DEPLOYMENT.md).

The example files live in `Build/Containers/examples/reverse-proxies/` (and the Traefik label override in
`Build/Containers/examples/traefik.override.example.yaml`); the blocks below are identical to them.
`make proxies-check` validates each one in containers (`nginx -t`, `caddy validate`, `haproxy -c`, Traefik starting
with the file provider and with Docker labels, `docker compose config` on the override) and then runs it against a
stand-in for the web tier over TLS: HTTP to HTTPS redirect, the live event stream arriving unbuffered, a 1 GiB
download and a 1 GiB upload passing whole, `X-Forwarded-For` and `X-Forwarded-Proto` arriving, and with `SLOW=1` a
response after 75 s of silence and an 80 s upload (which catch the common 60 s defaults). Checked with nginx 1.30,
Traefik 3, Caddy 2 and HAProxy 3.2.

## 1. Requirements for any proxy

| Requirement | Why |
|---|---|
| **Route the whole host name to Waystation, at the root** | Docker clients always call `https://<host>/v2/` and get tokens from `<WAYSTATION_BASE_URL>/auth/token`; the UI and API use absolute paths (`/ui/`, `/api/v1/`, `/repository/`). Serving Waystation under a path prefix (`https://example.com/waystation/`) does not work. Give it its own host name, e.g. `registry.example.com` |
| **No request body size limit** | Uploads and `docker push` layers can be many GiB (Waystation's own limit is `WAYSTATION_MAX_UPLOAD_GB`) |
| **No request buffering** | Bodies must stream to Waystation, which hashes them as they arrive; a proxy that spools a 10 GiB upload to disk first doubles the time and needs the disk |
| **No response buffering** | Cached files are sent by nginx `sendfile` at line rate; a proxy that spools them to temp files (nginx's default 1 GiB temp file limit) slows and stalls large downloads |
| **The live event stream is not buffered**: `GET /api/v1/events/stream` (`text/event-stream`) | The dashboard updates live from it. A buffering proxy delivers events in bursts or not at all. The web tier sends `X-Accel-Buffering: no` and a heartbeat every 15 s |
| **Long idle timeouts** (15 minutes is a good default; 1 hour on the event stream) | A client waiting on another client's fill of the same large file can see no bytes until that fill completes; uploads of large layers take longer than the 60 s many proxies default to. The web tier allows 600 s itself |
| **HTTP/1.1 keep-alive to the web tier** | Avoids a TCP handshake per request for clients that make thousands of small requests (npm, apt) |
| **Pass `X-Forwarded-For`** (and `X-Forwarded-Proto` / `X-Forwarded-Host`) | Client addresses in logs, sign-in rate limits and the `/metrics` allow list come from it, once trusted (section 2) |
| Do not rewrite `Location`, `WWW-Authenticate`, `Link` or `Docker-*` headers, and do not compress `application/octet-stream` or `application/vnd.*` responses | Registry clients check digests and follow the URLs Waystation generates from `WAYSTATION_BASE_URL` |

Set `WAYSTATION_BASE_URL` to exactly the URL clients use (`https://registry.example.com`, no trailing slash). It is
used for every generated URL (Docker token realm, npm tarball URLs, "Set me up" snippets), so the proxy does not
need to forward `Host` correctly for Waystation to work, but forwarding it does no harm.

## 2. Trusting `X-Forwarded-For`

By default Waystation ignores `X-Forwarded-*` from outside and logs the address that connected to the web
container, which behind a proxy is the proxy itself. To log and rate-limit by the real client:

```bash
WAYSTATION_TRUST_FORWARDED=true
WAYSTATION_TRUSTED_PROXIES=192.0.2.10/32        # the proxy's address as the web container sees it
```

How the address is resolved: the web tier passes its peer address to the server as `X-Real-IP` and appends it to
`X-Forwarded-For`. If that peer is in `WAYSTATION_TRUSTED_PROXIES`, the server walks `X-Forwarded-For` from right to
left and takes the first address that is not a trusted proxy. With several proxies in a chain, list them all.
`X-Forwarded-Proto` and `X-Forwarded-Host` are not used for URLs (`WAYSTATION_BASE_URL` is).

Which address to list:

| Proxy runs | Address the web container sees |
|---|---|
| On another machine | That machine's address (`192.0.2.10/32`) |
| On the Docker host, connecting to the published port | Usually the gateway of Waystation's `frontend` network (Docker's port forwarding), so set an explicit `WAYSTATION_SUBNET` and trust its gateway, e.g. `WAYSTATION_SUBNET=198.51.100.0/24` and `WAYSTATION_TRUSTED_PROXIES=198.51.100.1/32` |
| In a container on a shared Docker network | The proxy container's address on that network: give the network a fixed subnet and trust that subnet |

To confirm, sign in through the proxy and look at the `client_ip` field of the server's access log
(`docker logs waystation-server`): it should be your workstation's address, not the proxy's. Only list proxies that
overwrite or append `X-Forwarded-For` themselves; trusting anything else lets clients forge their address.

## 3. nginx

<!-- example: Build/Containers/examples/reverse-proxies/nginx/waystation.conf -->
```nginx
# Waystation behind nginx (Documents/Operations/REVERSE_PROXIES.md). nginx terminates TLS and forwards plain HTTP to
# the Waystation web container. Checked by Build/Containers/examples/reverse-proxies/check/check.sh.
#
# Upstream: the web container's alias on a shared Docker network (waystation-web:8080), or 127.0.0.1:8080 when web
# publishes its port on the same host (WAYSTATION_HTTP_BIND=127.0.0.1).
upstream waystation {
    server waystation-web:8080;
    keepalive 32;
}

server {
    listen 80;
    listen [::]:80;
    server_name registry.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name registry.example.com;

    ssl_certificate     /etc/nginx/tls/fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # Large uploads and downloads pass through untouched: no body size limit, no buffering to temp files.
    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_max_temp_file_size 0;

    # Long timeouts: they are idle times between two reads/writes, and a client waiting on another client's cache
    # fill can see no bytes until the fill commits. The web tier itself allows 600 s.
    client_body_timeout 900s;
    send_timeout 900s;
    proxy_connect_timeout 10s;
    proxy_send_timeout 900s;
    proxy_read_timeout 900s;

    # Keep-alive to the web tier.
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    # Waystation trusts X-Forwarded-For from this proxy only when WAYSTATION_TRUST_FORWARDED=true and this proxy's
    # address (as the web container sees it) is in WAYSTATION_TRUSTED_PROXIES.
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;

    # Everything (UI, API, /v2/, /repository/, /auth/, health) goes to the web tier at the root of the host.
    location / {
        proxy_pass http://waystation;
    }

    # Live event stream (SSE): never buffered or cached; idle up to an hour (heartbeats arrive every 15 s). The web
    # tier also sends "X-Accel-Buffering: no", which nginx honours even where buffering is on.
    location = /api/v1/events/stream {
        proxy_pass http://waystation;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
    }
}
```

`http2 on` needs nginx 1.25.1 or later (older versions: `listen 443 ssl http2;`). If nginx and Waystation share a
Docker network, set `WAYSTATION_HTTP_BIND=127.0.0.1` or drop the published port so clients cannot bypass the proxy.

## 4. Traefik v3

Traefik streams bodies, never buffers unless you add the `buffering` middleware (do not), and flushes
`text/event-stream` responses immediately. The one setting that matters is the entry point's `readTimeout`: Traefik
v3 defaults it to **60 s**, which cuts any upload that takes longer. Set it to `0s` on the entry point Waystation
uses.

Static configuration:

<!-- example: Build/Containers/examples/reverse-proxies/traefik/traefik.yaml -->
```yaml
# Traefik v3 static configuration for Waystation (Documents/Operations/REVERSE_PROXIES.md). Routes come either from
# the file provider (dynamic/waystation.yaml) or from Docker labels (../../traefik.override.example.yaml); keep the
# provider you use. Checked by Build/Containers/examples/reverse-proxies/check/check.sh.
entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
  websecure:
    address: ":443"
    transport:
      respondingTimeouts:
        # Traefik v3 defaults readTimeout to 60 s, which cuts any upload (docker push of a large layer) that takes
        # longer. 0 = no limit; idle connections are still closed after idleTimeout.
        readTimeout: 0s
        writeTimeout: 0s
        idleTimeout: 180s
    # Only list proxies that sit in front of Traefik itself (a load balancer, a CDN); clients' own X-Forwarded-*
    # headers are replaced otherwise.
    # forwardedHeaders:
    #   trustedIPs: ["192.0.2.0/24"]

providers:
  file:
    directory: /etc/traefik/dynamic
    watch: true
  docker:
    exposedByDefault: false
    network: proxy

# Certificates: either static files (dynamic/waystation.yaml, "tls.certificates") or ACME:
# certificatesResolvers:
#   letsencrypt:
#     acme:
#       email: admin@example.com
#       storage: /etc/traefik/acme/acme.json
#       tlsChallenge: {}

log:
  level: INFO
```

### File provider

<!-- example: Build/Containers/examples/reverse-proxies/traefik/dynamic/waystation.yaml -->
```yaml
# Traefik v3 dynamic configuration (file provider) for Waystation (Documents/Operations/REVERSE_PROXIES.md).
# Traefik streams request and response bodies (no buffering middleware here, ever) and flushes text/event-stream
# responses immediately, so the live event stream and large blobs need no special route.
http:
  routers:
    waystation:
      rule: Host(`registry.example.com`)
      entryPoints: [websecure]
      service: waystation
      tls: {}
      # tls:
      #   certResolver: letsencrypt

  services:
    waystation:
      loadBalancer:
        servers:
          - url: http://waystation-web:8080
        passHostHeader: true
        serversTransport: waystation
        healthCheck:
          path: /healthz
          interval: 15s
          timeout: 3s

  serversTransports:
    waystation:
      forwardingTimeouts:
        dialTimeout: 10s
        # 0 = wait as long as needed for response headers (a cache fill of a large blob can take a while to start).
        responseHeaderTimeout: 0s
        idleConnTimeout: 90s

tls:
  certificates:
    - certFile: /etc/traefik/tls/fullchain.pem
      keyFile: /etc/traefik/tls/privkey.pem
```

### Docker labels

Keep the site-specific labels in an override file next to `compose.yaml` on the host (never in the repository's
`compose.yaml`). It removes the published port and attaches `web` to Traefik's network:

<!-- example: Build/Containers/examples/traefik.override.example.yaml -->
```yaml
# Site override: Waystation behind Traefik with Docker labels (Documents/Operations/REVERSE_PROXIES.md).
# Copy next to compose.yaml on the host as compose.override.yaml (or pass it with -f) and adapt the names.
# Assumes Traefik runs on this host with the Docker provider (exposedByDefault: false), an entry point "websecure"
# and an external network "proxy" shared with it (Build/Containers/examples/reverse-proxies/traefik/traefik.yaml).
# Requires Docker Compose 2.24+ for !reset.
services:
  web:
    # No published port: Traefik reaches web over the shared network.
    ports: !reset []
    networks:
      proxy:
        aliases: [waystation-web]
    labels:
      traefik.enable: "true"
      traefik.docker.network: proxy
      traefik.http.routers.waystation.rule: Host(`registry.example.com`)
      traefik.http.routers.waystation.entrypoints: websecure
      traefik.http.routers.waystation.tls: "true"
      # With ACME instead of a static certificate:
      # traefik.http.routers.waystation.tls.certresolver: letsencrypt
      traefik.http.services.waystation.loadbalancer.server.port: "8080"
      traefik.http.services.waystation.loadbalancer.healthcheck.path: /healthz
      traefik.http.services.waystation.loadbalancer.healthcheck.interval: 15s

networks:
  proxy:
    external: true
```

```bash
docker compose --env-file waystation.env -f compose.yaml -f traefik.override.yaml up -d --wait
```

The web container then reaches Traefik from the `proxy` network: give that network a fixed subnet and put it in
`WAYSTATION_TRUSTED_PROXIES`.

## 5. Caddy 2

Caddy's defaults already fit: no body size limit, bodies streamed, no proxy read timeouts, `X-Forwarded-For`,
`-Proto` and `-Host` set, and `text/event-stream` flushed immediately. `flush_interval -1` makes every response
stream without delay.

<!-- example: Build/Containers/examples/reverse-proxies/caddy/Caddyfile -->
```caddyfile
# Waystation behind Caddy 2 (Documents/Operations/REVERSE_PROXIES.md). Caddy obtains a certificate automatically
# for the site name. Checked by Build/Containers/examples/reverse-proxies/check/check.sh.
#
# Caddy streams request and response bodies, sets no body size limit and no proxy timeouts by default, and sends
# X-Forwarded-For/-Proto/-Host itself, so very little is needed here.
registry.example.com {
	# Own certificate instead of ACME:
	# tls /etc/caddy/tls/fullchain.pem /etc/caddy/tls/privkey.pem

	reverse_proxy waystation-web:8080 {
		# Flush every write immediately: the live event stream and blob downloads are never held back.
		flush_interval -1

		health_uri /healthz
		health_interval 15s

		transport http {
			dial_timeout 10s
		}
	}
}
```

If another proxy sits in front of Caddy, add its range to Caddy's global `servers { trusted_proxies static ... }`
so Caddy keeps the original `X-Forwarded-For`.

## 6. HAProxy

HAProxy streams request and response bodies and has no body size limit unless `option http-buffer-request` is on.
Its `timeout client` / `timeout server` are idle timeouts; `timeout tunnel` covers long-lived responses such as the
event stream.

<!-- example: Build/Containers/examples/reverse-proxies/haproxy/haproxy.cfg -->
```haproxy
# Waystation behind HAProxy (Documents/Operations/REVERSE_PROXIES.md). HAProxy terminates TLS and forwards plain
# HTTP to the Waystation web container. Checked by Build/Containers/examples/reverse-proxies/check/check.sh.
global
    log stdout format raw local0

# Docker's embedded DNS, so waystation-web is re-resolved when the container is recreated. Drop this section (and
# "resolvers docker" below) when HAProxy runs on the host and the server line points at 127.0.0.1:8080.
resolvers docker
    nameserver dns 127.0.0.11:53
    hold valid 10s

defaults
    mode http
    log global
    option httplog
    # Adds X-Forwarded-For with the client address.
    option forwardfor
    timeout connect 10s
    # Idle timeouts (no bytes in either direction). A client waiting on another client's cache fill can see no
    # bytes until the fill commits; the web tier itself allows 600 s.
    timeout client 900s
    timeout server 900s
    # Live event stream and other long-lived responses: heartbeats every 15 s keep them well inside this.
    timeout tunnel 1h
    timeout http-request 30s
    timeout http-keep-alive 30s
    # No "option http-buffer-request": request bodies (uploads) are streamed, and HAProxy sets no body size limit.

frontend waystation_https
    bind :80
    bind :443 ssl crt /usr/local/etc/haproxy/tls/registry.example.com.pem alpn h2,http/1.1
    http-request redirect scheme https code 301 unless { ssl_fc }
    http-request set-header X-Forwarded-Proto https
    http-request set-header X-Forwarded-Host %[req.hdr(host)]
    default_backend waystation

backend waystation
    option httpchk GET /healthz
    http-check expect status 200
    server web waystation-web:8080 check inter 15s resolvers docker init-addr last,libc,none
```

The certificate file is the certificate chain followed by the private key in one PEM file.

## 7. Synology DSM (Login Portal reverse proxy)

Not covered by `make proxies-check` (DSM is not available as a container); these are the settings to use, from DSM
7.2 (Control Panel → Login Portal → Advanced → Reverse Proxy):

- **Source**: protocol HTTPS, host name `registry.example.com`, port 443, HSTS as you prefer. Choose the
  certificate for this host name in Control Panel → Security → Certificate → Settings.
- **Destination**: protocol HTTP, the Docker host's address and `WAYSTATION_HTTP_PORT` (e.g. `192.0.2.20:8080`).
- **Advanced Settings**: set the proxy connection timeout to 60 s, and the proxy send and read timeouts to the
  maximum DSM accepts (at least 900 s). HTTP version: HTTP 1.1.
- **Custom Header**: the "WebSocket" preset is not needed. DSM adds `X-Forwarded-For` itself; set
  `WAYSTATION_TRUSTED_PROXIES` to the NAS's address.
- DSM's reverse proxy is nginx with DSM's own defaults, which are not exposed in the UI (body size limit and
  buffering vary between DSM versions). The event stream works because the web tier sends `X-Accel-Buffering: no`.
  If large uploads fail with `413` or large pulls stall, DSM's limits are the cause: run one of the proxies above in
  a container on the NAS (Container Manager) instead, or point clients at the Docker host directly.

## 8. Checklist after setup

```bash
curl -fsS https://registry.example.com/healthz                     # {"status":"ok"}
curl -fsS https://registry.example.com/readyz                      # {"status":"ok"}
curl -sI  https://registry.example.com/v2/ | grep -i www-authenticate   # realm="https://registry.example.com/auth/token"
curl -sN  --max-time 5 https://registry.example.com/api/v1/events/stream?topics=stats -o /dev/null -w '%{http_code}\n'
```

The last call answers `401` without a session; signed in, the dashboard's live indicator shows the stream is
connected. Pull a large image through the proxy (`docker pull registry.example.com/library/ubuntu:26.04`) and
watch it on the dashboard: progress should move smoothly, not jump at the end.
