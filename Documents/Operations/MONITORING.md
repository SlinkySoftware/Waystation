# Monitoring Waystation

Waystation exposes health endpoints and Prometheus metrics from the server. The repository ships a Grafana
dashboard and a Zabbix 7 template for them in `Build/monitoring/`. This page lists exactly what is exported today
and what is not yet. Deployment: [DEPLOYMENT.md](DEPLOYMENT.md).

| File | What |
|---|---|
| `Build/monitoring/grafana/waystation-dashboard.json` | Grafana dashboard (Grafana 11 or later, Prometheus data source chosen by a variable) |
| `Build/monitoring/zabbix/waystation_by_http.yaml` | Zabbix 7.0 template "Waystation by HTTP" (HTTP agent items, repository discovery, triggers) |

## 1. Endpoints

All of them are reached through the web container at the base URL (`https://registry.example.com/...`).

| Endpoint | Access | Answer |
|---|---|---|
| `GET /healthz` | anyone | `200 {"status":"ok"}` while the server process runs |
| `GET /readyz` | anyone | `200 {"status":"ok"}` when MongoDB and Valkey both answered within 2 s, else `503 {"status":"unavailable"}` |
| `GET /metrics` | client address in `WAYSTATION_METRICS_ALLOW_CIDRS` (default loopback only), or `Authorization: Bearer <API token>` with the `metrics` scope whose owner holds `stats-read` | Prometheus text format; everyone else gets `404` |

Two ways to let a scraper in:

- **By address**: add your Prometheus server's or Zabbix server's (or proxy's) address, as the web container sees
  it, to `WAYSTATION_METRICS_ALLOW_CIDRS`, e.g. `WAYSTATION_METRICS_ALLOW_CIDRS=127.0.0.0/8,::1/128,192.0.2.30/32`.
  Behind a reverse proxy the address is resolved through `X-Forwarded-For` only when the proxy is trusted
  (REVERSE_PROXIES.md section 2).
- **By token**: create an API token with only the `metrics` scope for a user (or service account) holding
  `stats-read` (the built-in `viewer` role has it), and send it as a bearer token. The token works from any address.

The worker has no HTTP listener: its liveness is the container healthcheck (a heartbeat file the job loop renews;
`docker inspect --format '{{.State.Health.Status}}' waystation-worker`). Monitor it with your container monitoring
(for Zabbix, the "Docker by Zabbix agent 2" template reports container health).

## 2. Metrics exported today

All from the server, per server process (no process, Python runtime or Go-style default collectors are exported).
Counters also have the usual `_created` series.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `waystation_requests_total` | counter | `repo`, `format`, `status`, `cache` | Format requests answered by the proxy engine (`/repository/...`, `/v2/...`). `cache` is `HIT` (from cache), `REVALIDATED` (metadata checked upstream, unchanged), `STALE` (from cache because the upstream failed), `FETCHING` (miss, this request fetched it), `COALESCED` (miss, joined another request's fetch) or `ERROR` (the upstream failed; status 502). Negative-cache answers count as `HIT` with status 404 |
| `waystation_bytes_total` | counter | `repo`, `direction` | `out_cache`: sent to clients from the blob store (upstream bandwidth saved). `out_upstream`: sent to clients while streaming from upstream. `in_upstream`: downloaded from upstreams |
| `waystation_upstream_latency_seconds` | histogram | `repo` | Upstream time to response headers. Buckets 5 ms to 60 s |
| `waystation_inflight_transfers` | gauge | none | Live transfers in this process right now |
| `waystation_coalesce_total` | counter | `repo`, `event` | Request coalescing: `owner` (fetched for others), `solo` (fetched without Valkey coordination), `tail` (followed a fetch by streaming its file), `wait` (waited for its commit), `takeover` (took over from an owner that went away) |
| `waystation_upstream_state` | gauge | `repo` | Declared (0 ok, 1 degraded, 2 blocked, 3 offline) but **not set yet**, so it has no samples |

Not counted in these metrics: REST API and UI requests, the event stream, the Docker token service and the
`/v2/` version check; only format traffic through the proxy engine.

Derived figures used by the dashboard and the template:

- **Cache hit ratio** = requests with `cache` in `HIT|REVALIDATED|STALE` / all requests.
- **Upstream error ratio** = requests with `cache="ERROR"` / all requests. Per repository, this is the "upstream
  degraded / down" signal until `waystation_upstream_state` is wired.
- **Bandwidth saved** = `waystation_bytes_total{direction="out_cache"}`.

With `WAYSTATION_SERVER_WORKERS` above 1, each scrape reaches one of the processes (multi-process aggregation is not
done yet), so counters jump between scrapes. Keep the default of 1 when you rely on these metrics; the stats API and
the UI dashboard aggregate across processes and replicas.

## 3. Prometheus and Grafana

Scrape job (Prometheus):

```yaml
scrape_configs:
  - job_name: waystation
    scheme: https
    metrics_path: /metrics
    scrape_interval: 30s
    # Only when scraping by token instead of by address:
    # authorization:
    #   credentials_file: /etc/prometheus/waystation-metrics.token
    static_configs:
      - targets: ['registry.example.com']
```

Import `Build/monitoring/grafana/waystation-dashboard.json` (Dashboards → New → Import, or file provisioning). It
picks the Prometheus data source with the `datasource` variable and filters by `job`, `instance` and `repo`.
Panels: requests per second, cache hit ratio, bytes served from cache and fetched from upstream, upstream error
ratio and transfers in flight (stat tiles); requests by cache state; throughput by direction; hit ratio and
upstream errors per repository; upstream latency p95; coalescing events; a per-repository table; and a short note of
what is not exported yet. Ratios and totals in the tiles and the table cover the selected time range.

Checked by loading it into Grafana 12.2 through file provisioning, with Prometheus 3.7 scraping sample output of
the real `waystation_server.metrics.Metrics` class: every panel query returned data.

## 4. Zabbix

Import `Build/monitoring/zabbix/waystation_by_http.yaml` (Data collection → Templates → Import; Zabbix 7.0 or
later), link "Waystation by HTTP" to a host (no agent or interface needed) and set the macros:

| Macro | Default | Meaning |
|---|---|---|
| `{$WAYSTATION.URL}` | `https://registry.example.com` | Base URL, no trailing slash |
| `{$WAYSTATION.METRICS.TOKEN}` | empty (secret) | API token with the `metrics` scope, when Zabbix is not in `WAYSTATION_METRICS_ALLOW_CIDRS` |
| `{$WAYSTATION.METRICS.INTERVAL}` | `1m` | Scrape interval of `/metrics` and of the calculated ratios |
| `{$WAYSTATION.ERROR.RATIO.WARN}` | `5` | Overall upstream error ratio (%) that raises a trigger after 10 minutes |
| `{$WAYSTATION.UPSTREAM.ERROR.WARN}` | `10` | Per-repository error ratio (%): "upstream degraded" after 5 minutes |
| `{$WAYSTATION.UPSTREAM.ERROR.HIGH}` | `50` | Per-repository error ratio (%): "upstream down" after 5 minutes |

Items: liveness (`/healthz`), readiness (`/readyz`), the raw `/metrics` master item (not stored) and dependent
items for requests per second (all, from cache, upstream errors), cache hit and upstream error ratio, bytes per
second (served from cache, served while fetching, fetched from upstream), transfers in flight and coalesced
followers. Repository discovery (from the `repo` label) creates, per repository: requests, from-cache and error
rates, hit and error ratios, bytes from cache and from upstream, and the average upstream latency.

Triggers:

| Trigger | Severity |
|---|---|
| Waystation: server is down (`/healthz` not `ok`) | High |
| Waystation: unreachable (no `/healthz` answer for 5 minutes) | High |
| Waystation: MongoDB or Valkey unavailable (`/readyz` unavailable twice in 3 minutes) | High, depends on "server is down" |
| Waystation: no metrics for 10 minutes (usually `WAYSTATION_METRICS_ALLOW_CIDRS`) | Warning, depends on "unreachable" |
| Waystation: upstream error ratio above `{$WAYSTATION.ERROR.RATIO.WARN}`% for 10 minutes | Average |
| Waystation: upstream of `<repo>` is down (error ratio ≥ `{$WAYSTATION.UPSTREAM.ERROR.HIGH}`% for 5 minutes) | High |
| Waystation: upstream of `<repo>` is degraded (error ratio > `{$WAYSTATION.UPSTREAM.ERROR.WARN}`% for 5 minutes) | Warning, depends on "down" |

There is no low-disk trigger because free space is not exported yet; monitor the data directory's filesystem with
your host monitoring (Waystation refuses new cache writes below `WAYSTATION_MIN_FREE_GB`).

Checked by importing it into Zabbix 7.0 through the API, linking it to a host polling a stand-in for Waystation
serving sample metrics: every item and discovered prototype received values, and the liveness and readiness
triggers fired when the stand-in reported down and unavailable.

## 5. Not exported yet

Useful signals that Waystation does not export as metrics today (the UI dashboard and `/api/v1/stats/*` show some
of them):

| Signal | Where it is today |
|---|---|
| Upstream health state per repository (`waystation_upstream_state` is declared but never set) | Upstream health in the UI and `/api/v1/stats/upstreams` |
| Docker Hub pull rate limit (limit, remaining, reset) | Recorded from `ratelimit-*` headers; shown in the UI and `/api/v1/stats/upstreams` |
| Job queue depth, running jobs, retries and dead letters per queue | Valkey streams `jobs:<queue>`, `jobs:dead`; Tasks page planned (T-O04) |
| Worker liveness and count | `workers` collection, container healthcheck |
| Blob store used and free bytes, blob count | `/api/v1/stats/storage` |
| Writes refused by the free-space guard, quota hits | Not counted (quotas: T-O03) |
| Latency of every HTTP route (including cache-hit time to first byte) and API errors | JSON access logs only |
| Connected event-stream clients | Not counted |
| Sign-in failures and lockouts | Audit and logs |
| Process metrics (CPU, memory, open files) and multi-process aggregation | Not exported; use container metrics |
