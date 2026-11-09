# Performance suite (k6)

Measures the SPECIFICATION §5.1 budgets against a throwaway Waystation stack and compares them with
`Build/ci/budgets.json` (ARCHITECTURE §9).

```
make perf                                  # CI profile: a few minutes, modest load
make perf PROFILE=full                     # SPEC sizes: 1M assets, 500 concurrent downloads, 4 vCPU server (nightly)
make perf STRICT=1                         # fail on every budget over its limit (what M2 switches on)
PERF_SCENARIOS="hit_small api" make perf   # some scenarios only
PERF_LARGE_MIB=10240 make perf PROFILE=full  # any profile value can be overridden (here: a 10 GiB cached file)
KEEP=1 make perf                           # leave the stack running afterwards (docker compose -p <project> down -v)
```

Host needs bash, docker and flock. Results land in `tests/perf/.results/` (gitignored): `report.md` (the run
report: budgets table, every scenario's figures, container CPU/memory), `measured.json` (the budget values read by
`make budgets`), the raw `k6-<scenario>.json`, `sse.json` and `stats-<scenario>.csv`, and the warnings and errors
the stack logged (`server.log`: server and worker, `mongodb.log`, `web.log`: the last nginx lines). k6 logs the
first three failed requests of each client (URL, status, error).

## Machine safety

A run starts MongoDB, Valkey, the server, worker and web tier, an nginx origin and k6, so it holds the machine-wide
integration-test lock (`TEST_LOCK`, the one `make test-integration` takes) from start to finish: a perf run and an
integration run never overlap ("waiting" is normal). Every container has CPU and memory limits from the profile
(CI: server 2 CPUs / 2 GB, MongoDB 2 CPUs / 1.5 GB, k6 2 CPUs / 2 GB, web 2 CPUs, origin 1 CPU), and the stack and
its volumes are removed when the run ends, whatever the outcome. Large files on the origin are sparse; what
Waystation caches (CI: about 700 MB, full: about 6 GB) goes away with the project's volumes.

## How it works

```
k6 / sse_probe ──► waystation-web (nginx) ──► server ──► perf-origin (nginx, synthetic content)
      └──────────────── direct (miss baselines) ───────────►┘
```

1. `origin/generate.py` (dev image) writes synthetic upstream content for the profile into `tests/perf/.run/origin`:
   raw files, npm packuments + tarballs (real sha1/SRI), PyPI simple pages + wheels (sha256 fragments),
   `maven-metadata.xml`, an apt suite (`Release` listing the `Packages` hashes), a yum repository (`repomd.xml`
   listing a `primary.xml.gz`), Go module version lists, OCI images (manifests, configs,
   layers) and the origin's nginx config (Docker manifest locations with `Docker-Content-Digest`, a bandwidth limit
   on the miss paths: `PERF_ORIGIN_RATE`, 100 MiB/s by default, roughly a 1 Gbit/s upstream).
2. `compose.perf.yaml` on top of `Build/Containers/compose.yaml` starts the stack as compose project
   `waystation-perf-<checkout>` with production images built from this checkout (tag `perf`; no source mounts, no
   reload), its own container names and no published ports.
3. `prepare.py` (dev image) signs in as the per-run bootstrap admin, creates one proxy repository per format
   (`allow_private_upstream`, metadata max age one day) and warms the cache with every file the hit scenarios read.
4. `seed_assets.py` (server image, the application's own `StoredAsset` model and search keys) bulk-inserts
   `PERF_BULK_ASSETS` cached-file records into repository `perf-bulk` (`insert_many`, 10 000 per batch: 100 000
   in about 3 s) for the API scenario. Layout: `group-<g>/pkg-<p>/<version>/artifact-<n>.tar.gz`.
5. `k6/main.js` runs once per scenario (`grafana/k6` pinned by digest), so each scenario has the stack to itself and
   its own resource samples (`docker stats` every ~2 s).
6. `sse_probe.py` (dev image) measures the live event stream.
7. `collect.py` derives the budget values and the report; `Build/ci/budgets.py` compares them with the limits.

## Scenarios and budgets

Latency scenarios run at a fixed request rate (k6 `constant-arrival-rate`: CI 200 requests/s, full 500/s; the API
50/s and 100/s), so p95 describes the code path under a steady load rather than a saturated box; the concurrency
scenario is the saturation test. TTFB is k6's `http_req_waiting` measured on the same Docker network (server-side
TTFB plus one bridge hop).

| Scenario | What it does | Budget (`budgets.json` id) |
|---|---|---|
| `hit_small` | random cached 4 KiB raw files | `cache_hit_content_ttfb_p95_ms`: highest p95 TTFB of the three content scenarios |
| `hit_range` | 64 KiB ranges at random offsets of the large cached file (206) | same |
| `hit_large` | the whole large cached file (CI 256 MiB, full 2 GiB), one at a time; MiB/s reported (line rate) | same |
| `metadata` | npm packuments (abbreviated and full), PyPI pages (JSON and HTML), `maven-metadata.xml`, apt `Release` and `Packages.gz`, yum `repomd.xml` and `primary.xml.gz`, Go `@v/list` and `@latest` | `cache_hit_metadata_p95_ms` (p95 duration) |
| `docker` | cached manifest HEAD and GET, by tag and by digest, with an anonymous pull token | `docker_manifest_cached_p95_ms` |
| `miss_small` | each never-cached 4 KiB file once straight from the origin, then through Waystation | `cache_miss_added_ttfb_ms`: median TTFB difference |
| `miss_large` | each never-cached large file (CI 128 MiB, full 1 GiB) from the origin, then through Waystation | `cache_miss_throughput_loss_pct`: 1 - median MiB/s ratio |
| `sse` | subscribes to `/api/v1/events/stream?topics=transfers`, downloads rate-limited uncached files (about 4 s each) | `live_event_latency_ms`: the higher p95 of (arrival - event `ts`) and (arrival of `transfer.started` - request) |
| `api` | search (name prefix, per repository, page 2 by cursor, everything), browse (root, a package folder), repository list, as the admin | `api_list_search_p95_ms` (seeded: CI 100 000 assets, full 1 000 000; the SPEC states 1M) |
| `concurrent` | CI 100 / full 500 clients downloading cached 4 KiB and 1 MiB files without pause | `concurrent_download_errors` |

Every hit scenario also counts responses whose `X-Ws-Cache` is not `HIT` (`not_hit` in the report).
`dashboard_first_meaningful_paint_ms` needs a browser and is not measured here; the initial-route bundle size is
enforced by `make frontend-build`.

## Profiles

`profiles/ci.env` and `profiles/full.env` hold every size, rate, duration and container limit. Any `PERF_*` variable
set in the environment overrides the profile.

## Enforcement

`budgets.json` says `"enforce": "warn"` for every k6 budget until milestone M2 (CLAUDE.md): over-budget values are
reported (and annotated in CI) but do not fail the run. `STRICT=1` (`budgets.py --strict`) fails on any budget over
its limit; at M2 the budgets switch to `"fail"`. A scenario that cannot run (stack, setup, k6 or probe failure)
always fails `make perf`.

## CI

`.gitea/workflows/perf.yml` (Gitea runners, no cost): the CI profile on manual runs, the full profile nightly; each
run uploads `tests/perf/.results/` as an artifact. See `Documents/Operations/CI.md`.
