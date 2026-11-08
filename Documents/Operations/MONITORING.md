# Monitoring Waystation

Waystation exposes health endpoints and Prometheus metrics from the server. The repository ships a Grafana
dashboard and a Zabbix 7 template for them in `Build/monitoring/`. This page lists exactly what is exported and the
few signals that are not yet. Deployment: [DEPLOYMENT.md](DEPLOYMENT.md).

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

The worker has no HTTP listener. Its presence is exported by the server (`waystation_workers`, from the heartbeat
every worker writes to MongoDB every 10 s), and its container healthcheck (a heartbeat file the job loop renews;
`docker inspect --format '{{.State.Health.Status}}' waystation-worker`) still tells a wedged worker apart. For
Zabbix, the "Docker by Zabbix agent 2" template reports container health.

## 2. Metrics

All from the server (no process, Python runtime or Go-style default collectors are exported). There are two kinds.

**Process metrics** are updated where things happen (an O(1) counter update; nothing extra on the request path).
Counters also have the usual `_created` series (except with several server processes, below).

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `waystation_requests_total` | counter | `repo`, `format`, `status`, `cache` | Format requests answered by the proxy engine (`/repository/...`, `/v2/...`). `cache` is `HIT` (from cache), `REVALIDATED` (metadata checked upstream, unchanged), `STALE` (from cache because the upstream failed), `FETCHING` (miss, this request fetched it), `COALESCED` (miss, joined another request's fetch) or `ERROR` (the upstream failed; status 502). Negative-cache answers count as `HIT` with status 404 |
| `waystation_bytes_total` | counter | `repo`, `direction` | `out_cache`: sent to clients from the blob store (upstream bandwidth saved). `out_upstream`: sent to clients while streaming from upstream. `in_upstream`: downloaded from upstreams |
| `waystation_upstream_latency_seconds` | histogram | `repo` | Upstream time to response headers. Buckets 5 ms to 60 s |
| `waystation_coalesce_total` | counter | `repo`, `event` | Request coalescing: `owner` (fetched for others), `solo` (fetched without Valkey coordination), `tail` (followed a fetch by streaming its file), `wait` (waited for its commit), `takeover` (took over from an owner that went away) |
| `waystation_http_request_duration_seconds` | histogram | `route`, `method`, `status` | Server time of every HTTP request (format, REST API, health, token service) except the long-lived event stream. `route` is the matched route template (`/api/v1/repositories/{name}`, `/repository/{repo_name}/{path}`; `unmatched` for unknown paths), never the raw path; `method` is the HTTP method (`OTHER` for unusual ones); `status` is the class (`2xx`, `3xx`, `4xx`, `5xx`). Buckets 5 ms to 30 s. For format requests served by `X-Accel-Redirect` this is the server's part only (lookup and headers), not the transfer |
| `waystation_storage_refusals_total` | counter | none | Cache fills and uploads refused by the free-space guard (free space would drop below `WAYSTATION_MIN_FREE_GB`) |
| `waystation_inflight_transfers` | gauge | none | Live transfers right now |
| `waystation_sse_clients` | gauge | none | Connected event-stream clients (open UI pages) |

**Cluster figures** describe the whole installation. The server reads them from Valkey, MongoDB and the data
filesystem when `/metrics` is scraped (a few pipelined Valkey commands, two indexed MongoDB counts, the storage
totals the UI already caches, one `statvfs`), caches them for 10 s and bounds each source to 2 s. A source that
fails or times out is reported by `waystation_metrics_source_up` and its metrics are left out of that scrape; the
scrape itself never fails. Every server process and replica reports the same values, so aggregate them with `max`,
never `sum`.

| Metric | Labels | Meaning |
|---|---|---|
| `waystation_upstream_state` | `repo` | Per proxy repository: 0 ok, 1 degraded (an upstream request failed in the last 10 minutes and none succeeded since), 2 blocked (reserved for auto-block, T-S07), 3 offline (switched offline by an administrator, or the repository is disabled). The same rule as the UI |
| `waystation_upstream_consecutive_failures` | `repo` | Upstream failures since its last success |
| `waystation_upstream_ratelimit_limit`, `waystation_upstream_ratelimit_remaining` | `repo` | The latest rate limit the upstream reported (Docker Hub `ratelimit-limit` / `ratelimit-remaining`); only repositories whose upstream reports one |
| `waystation_upstream_ratelimit_window_seconds` | `repo` | Its window (Docker Hub: `w=21600`, 6 hours) |
| `waystation_upstream_ratelimit_updated_timestamp_seconds` | `repo` | When the upstream last reported it (Unix time) |
| `waystation_jobs_waiting` | `queue` | Jobs queued and not yet picked up by a worker (queues `index`, `maintenance`, `stats`, `prefetch`, `import`) |
| `waystation_jobs_running` | `queue` | Jobs delivered to a worker and not finished (a job of a worker that died stays here until another worker reclaims it) |
| `waystation_jobs_delayed` | `queue` | Jobs waiting for a retry backoff or a later start time |
| `waystation_jobs_oldest_waiting_seconds` | `queue` | Age of the oldest job not yet picked up (0 when none waits) |
| `waystation_jobs_dead_letters` | none | Entries in the dead-letter stream: jobs that failed every attempt (the stream keeps about the last 10 000) |
| `waystation_workers` | none | Workers whose heartbeat is newer than 60 s |
| `waystation_scheduler_leaders` | none | Live workers holding the scheduler lease (1 expected; 0 means scheduled jobs do not start) |
| `waystation_blobstore_used_bytes`, `waystation_blobstore_blobs` | none | Bytes held by blobs (each stored once) and their number (MongoDB totals, refreshed at most every 30 s) |
| `waystation_blobstore_free_bytes`, `waystation_blobstore_size_bytes` | none | Free and total bytes of the filesystem holding the data directory |
| `waystation_blobstore_min_free_bytes` | none | The free-space guard (`WAYSTATION_MIN_FREE_GB` in bytes) |
| `waystation_metrics_source_up` | `source` | 1 when `valkey`, `mongodb` or `filesystem` answered while building the cluster figures, else 0 |

Derived figures used by the dashboard and the template:

- **Cache hit ratio** = requests with `cache` in `HIT|REVALIDATED|STALE` / all requests.
- **Upstream error ratio** = requests with `cache="ERROR"` / all requests (catches partial failures that
  `waystation_upstream_state` does not, since one success resets it).
- **Bandwidth saved** = `waystation_bytes_total{direction="out_cache"}`.
- **Free space %** = `waystation_blobstore_free_bytes / waystation_blobstore_size_bytes`.

**Several server processes** (`WAYSTATION_SERVER_WORKERS` above 1): `python -m waystation_server` points
`PROMETHEUS_MULTIPROC_DIR` at a fresh, empty directory on the server container's tmpfs (`/tmp/waystation-metrics`)
before uvicorn starts the processes, and each process keeps its process metrics in memory-mapped files there.
Whichever process answers a scrape merges all of them (`prometheus_client` multiprocess mode): counters and
histograms are summed, including processes that have exited (so they never go backwards); the two gauges are summed
over live processes and refreshed by each process every 2 s. Nothing to configure: scrape `/metrics` as usual. The
`_created` series are not exported in this mode. A process that crashes (instead of stopping) leaves its last gauge
values in the sum until the container restarts. Several **replicas** (server containers) are separate Prometheus
targets: scrape each one, sum process metrics across them and take the `max` of the cluster figures.

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
Panels, top to bottom:

- **Traffic**: requests per second, cache hit ratio, bytes served from cache and fetched from upstream, upstream
  error ratio and transfers in flight (stat tiles); requests by cache state; throughput by direction; hit ratio and
  upstream errors per repository; upstream latency p95; coalescing events; a per-repository table.
- **Upstreams**: upstream state over time (OK, Degraded, Blocked, Offline); upstream rate limit remaining (%); a
  table of the current state, consecutive failures and rate limit per repository.
- **Jobs and workers**: workers, scheduler leader, jobs waiting and running, oldest waiting job, dead letters
  (tiles); jobs by queue; oldest waiting job by queue.
- **Storage**: free space, free space %, blob store used, blobs, writes refused, metric sources down (tiles); the
  data filesystem (free, guard, used); writes refused per second.
- **HTTP**: API latency p95 by route; requests by status class; event-stream clients; a per-route table (requests,
  p95, 5xx).

Ratios and totals in the tiles and tables cover the selected time range.

Checked by loading it into Grafana 12.2 through file provisioning, with Prometheus 3.7 scraping sample output of
the real `waystation_server.metrics.Metrics` class plus cluster figures with the real names: every panel query
returned data.

## 4. Zabbix

Import `Build/monitoring/zabbix/waystation_by_http.yaml` (Data collection → Templates → Import; Zabbix 7.0 or
later), link "Waystation by HTTP" to a host (no agent or interface needed) and set the macros:

| Macro | Default | Meaning |
|---|---|---|
| `{$WAYSTATION.URL}` | `https://registry.example.com` | Base URL, no trailing slash |
| `{$WAYSTATION.METRICS.TOKEN}` | empty (secret) | API token with the `metrics` scope, when Zabbix is not in `WAYSTATION_METRICS_ALLOW_CIDRS` |
| `{$WAYSTATION.METRICS.INTERVAL}` | `1m` | Scrape interval of `/metrics` and of the calculated items |
| `{$WAYSTATION.ERROR.RATIO.WARN}` | `5` | Overall upstream error ratio (%) that raises a trigger after 10 minutes |
| `{$WAYSTATION.UPSTREAM.ERROR.WARN}` | `10` | Per-repository error ratio (%): "upstream degraded" after 5 minutes |
| `{$WAYSTATION.UPSTREAM.ERROR.HIGH}` | `50` | Per-repository error ratio (%): "upstream down" after 5 minutes |
| `{$WAYSTATION.UPSTREAM.STATE.PERIOD}` | `10m` | How long an upstream must stay degraded before "upstream is failing" |
| `{$WAYSTATION.RATELIMIT.REMAINING.WARN}` | `10` | Upstream rate limit left (% of the limit) that raises a trigger |
| `{$WAYSTATION.WORKERS.MIN}` | `1` | Fewest live workers expected |
| `{$WAYSTATION.JOBS.AGE.WARN}` | `1800` | Age (s) of the oldest waiting job that raises a trigger after 10 minutes |
| `{$WAYSTATION.DISK.FREE.WARN}` | `15` | Free space (% of the data filesystem) below which "low disk space" fires |
| `{$WAYSTATION.API.LATENCY.WARN}` | `1` | Average REST API latency (s) that raises a trigger after 10 minutes |

Items: liveness (`/healthz`), readiness (`/readyz`), the raw `/metrics` master item (not stored) and dependent
items for requests per second (all, from cache, upstream errors), cache hit and upstream error ratio, bytes per
second (served from cache, served while fetching, fetched from upstream), transfers in flight, coalesced followers,
workers, scheduler leaders, jobs waiting and running, oldest waiting job, dead letters, blob store free, size,
used, guard and blobs, free space %, writes refused per second, event-stream clients, HTTP 5xx per second, API
requests per second and average API latency, and whether all metric sources answered. Cluster figures keep no value
while their source is down (rather than a misleading 0).

Discovery:

- **Repositories** (labels of `waystation_requests_total`): per repository requests, from-cache and error rates,
  hit and error ratios, bytes from cache and from upstream, average upstream latency.
- **Upstreams** (labels of `waystation_upstream_state`: every proxy repository, even before its first request): state
  (value map OK / Degraded / Blocked / Offline), consecutive failures, rate limit and rate limit remaining.
- **Job queues** (labels of `waystation_jobs_waiting`): waiting, running, delayed and oldest waiting job per queue.

Triggers:

| Trigger | Severity |
|---|---|
| Waystation: server is down (`/healthz` not `ok`) | High |
| Waystation: unreachable (no `/healthz` answer for 5 minutes) | High |
| Waystation: MongoDB or Valkey unavailable (`/readyz` unavailable twice in 3 minutes) | High, depends on "server is down" |
| Waystation: no metrics for 10 minutes (usually `WAYSTATION_METRICS_ALLOW_CIDRS`) | Warning, depends on "unreachable" |
| Waystation: cluster figures unavailable for 10 minutes | Warning, depends on "MongoDB or Valkey unavailable" |
| Waystation: upstream error ratio above `{$WAYSTATION.ERROR.RATIO.WARN}`% for 10 minutes | Average |
| Waystation: upstream of `<repo>` is down (error ratio ≥ `{$WAYSTATION.UPSTREAM.ERROR.HIGH}`% for 5 minutes) | High |
| Waystation: upstream of `<repo>` is degraded (error ratio > `{$WAYSTATION.UPSTREAM.ERROR.WARN}`% for 5 minutes) | Warning, depends on "down" |
| Waystation: upstream of `<repo>` is blocked (state 2) | High |
| Waystation: upstream of `<repo>` is failing (state degraded for `{$WAYSTATION.UPSTREAM.STATE.PERIOD}`) | Warning |
| Waystation: `<repo>` is offline (state 3, an administrator's choice) | Information |
| Waystation: `<repo>`: upstream rate limit below `{$WAYSTATION.RATELIMIT.REMAINING.WARN}`% | Warning |
| Waystation: fewer than `{$WAYSTATION.WORKERS.MIN}` workers for 5 minutes | High |
| Waystation: no scheduler leader for 5 minutes | Average, depends on "fewer than ... workers" |
| Waystation: jobs waiting longer than `{$WAYSTATION.JOBS.AGE.WARN}`s (for 10 minutes) | Warning, depends on "fewer than ... workers" |
| Waystation: a job was dead-lettered (the dead-letter count changed; close it manually) | Warning |
| Waystation: blob store full (free space at or below the free-space guard: new writes refused) | High |
| Waystation: low disk space (below `{$WAYSTATION.DISK.FREE.WARN}`% free for 5 minutes) | Warning, depends on "blob store full" |
| Waystation: writes refused for lack of disk space (in the last 5 minutes) | Average, depends on "blob store full" |
| Waystation: API slower than `{$WAYSTATION.API.LATENCY.WARN}`s on average for 10 minutes | Warning |

Checked by importing it into Zabbix 7.0 through the API and linking it to a host polling a stand-in serving sample
output of the real `Metrics` class plus cluster figures: every item and discovered prototype received values, and
the liveness, readiness, failing-upstream, rate-limit, job-age, refused-writes, low-disk and blob-store-full
triggers fired on matching sample values.

## 5. Not exported yet

| Signal | Where it is today |
|---|---|
| Docker Hub rate-limit reset time | Docker Hub reports only the window (`w=21600`), exported as `waystation_upstream_ratelimit_window_seconds` |
| Job retries and failures per job kind | Job runs in MongoDB (`job_runs`); Tasks page planned (T-O04) |
| Quota hits | Quotas are not built yet (T-O03) |
| Sign-in failures and lockouts | Audit and logs |
| Per-repository storage | `/api/v1/stats/storage` and the UI |
| Process metrics (CPU, memory, open files) | Not exported; use container metrics |
