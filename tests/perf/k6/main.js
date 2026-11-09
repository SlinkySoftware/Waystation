// Waystation perf suite: one k6 run per scenario (tests/perf/README.md, SPECIFICATION §5.1).
//   k6 run -e SCENARIO=<name> -e PERF_BASE=http://waystation-web:8080 ... /perf/k6/main.js
// run.sh calls it once per scenario, so each scenario gets the stack to itself and its own resource samples.
// handleSummary writes /results/k6-<scenario>.json: the scenario's custom metrics (ms unless the name says otherwise).
import http from 'k6/http';
import { check, fail } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import exec from 'k6/execution';

const BASE = __ENV.PERF_BASE || 'http://waystation-web:8080';
const SCENARIO = __ENV.SCENARIO || 'hit_small';
const T = JSON.parse(open(__ENV.PERF_TARGETS || '/run-dir/targets.json'));
const num = (name, fallback) => Number(__ENV[name] || fallback);

const HIT_VUS = num('PERF_HIT_VUS', 10);
const HIT_DURATION = `${num('PERF_HIT_DURATION_S', 15)}s`;
const API_VUS = num('PERF_API_VUS', 10);
const API_DURATION = `${num('PERF_API_DURATION_S', 15)}s`;
const CONC_VUS = num('PERF_CONC_VUS', 100);
const CONC_DURATION = `${num('PERF_CONC_DURATION_S', 20)}s`;
const LARGE_ITERATIONS = num('PERF_LARGE_ITERATIONS', 3);
const BULK_COUNT = num('PERF_BULK_ASSETS', 100000);

// ---------------------------------------------------------------------------- metrics
const metrics = {
  hit_content_ttfb: new Trend('ws_hit_content_ttfb', true), // every content hit: small, range, large
  hit_small_ttfb: new Trend('ws_hit_small_ttfb', true),
  hit_range_ttfb: new Trend('ws_hit_range_ttfb', true),
  hit_large_ttfb: new Trend('ws_hit_large_ttfb', true),
  hit_large_mbps: new Trend('ws_hit_large_mbps'),
  metadata: new Trend('ws_hit_metadata', true),
  docker_manifest: new Trend('ws_docker_manifest', true),
  docker_manifest_head: new Trend('ws_docker_manifest_head', true),
  docker_manifest_get: new Trend('ws_docker_manifest_get', true),
  miss_ttfb_proxied: new Trend('ws_miss_ttfb_proxied', true),
  miss_ttfb_direct: new Trend('ws_miss_ttfb_direct', true),
  miss_mbps_proxied: new Trend('ws_miss_mbps_proxied'),
  miss_mbps_direct: new Trend('ws_miss_mbps_direct'),
  api: new Trend('ws_api', true),
  concurrent: new Trend('ws_concurrent_duration', true),
  errors: new Counter('ws_errors'),
  not_hit: new Counter('ws_not_hit'),
};
const API_ENDPOINTS = ['search', 'search_repo', 'search_page2', 'search_all', 'browse_root', 'browse_folder', 'repositories'];
const apiTrends = Object.fromEntries(API_ENDPOINTS.map((e) => [e, new Trend(`ws_api_${e}`, true)]));

// ---------------------------------------------------------------------------- scenarios
const constant = (fn, vus, duration) => ({ executor: 'constant-vus', exec: fn, vus, duration, gracefulStop: '30s' });
const serial = (fn, iterations, maxDuration) => ({
  executor: 'shared-iterations',
  exec: fn,
  vus: 1,
  iterations,
  maxDuration,
});
const SCENARIOS = {
  hit_small: constant('hitSmall', HIT_VUS, HIT_DURATION),
  hit_range: constant('hitRange', HIT_VUS, HIT_DURATION),
  hit_large: serial('hitLarge', LARGE_ITERATIONS, '30m'),
  metadata: constant('metadata', HIT_VUS, HIT_DURATION),
  docker: constant('docker', HIT_VUS, HIT_DURATION),
  miss_small: serial('missSmall', T.miss_small.length, '15m'),
  miss_large: serial('missLarge', T.miss_large.length, '30m'),
  api: constant('api', API_VUS, API_DURATION),
  concurrent: constant('concurrent', CONC_VUS, CONC_DURATION),
};
if (!SCENARIOS[SCENARIO]) fail(`unknown SCENARIO ${SCENARIO}`);

export const options = {
  scenarios: { [SCENARIO]: SCENARIOS[SCENARIO] },
  discardResponseBodies: true,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
  noConnectionReuse: false,
  insecureSkipTLSVerify: false,
  setupTimeout: '120s',
};

// ---------------------------------------------------------------------------- helpers
const pick = (list) => list[Math.floor(Math.random() * list.length)];

function ok(res, expected, label) {
  const good = res.status === expected && res.error_code === 0;
  if (!good) {
    metrics.errors.add(1, { scenario: SCENARIO });
    if (exec.vu.iterationInScenario < 3) {
      console.warn(`${label}: status ${res.status} error ${res.error_code} ${res.error || ''}`);
    }
  }
  return good;
}

function countHit(res) {
  const state = res.headers['X-Ws-Cache'];
  if (state !== 'HIT' && state !== 'REVALIDATED') metrics.not_hit.add(1);
}

function mbps(bytes, res) {
  const seconds = res.timings.duration / 1000;
  return seconds > 0 ? bytes / 1048576 / seconds : 0;
}

// Docker pull tokens: anonymous, from the registry's own 401 challenge, cached per VU and image for 4 minutes
// (tokens live 5).
const tokens = {};
function dockerToken(url) {
  const cached = tokens[url];
  if (cached && cached.until > Date.now()) return cached.token;
  const res = http.get(BASE + url, { responseType: 'none' });
  if (res.status !== 401) return '';
  const challenge = res.headers['Www-Authenticate'] || '';
  const params = {};
  for (const m of challenge.matchAll(/(\w+)="([^"]*)"/g)) params[m[1]] = m[2];
  const realm = params.realm.startsWith('http') ? params.realm : BASE + params.realm;
  const query = ['service', 'scope']
    .filter((k) => params[k])
    .map((k) => `${k}=${encodeURIComponent(params[k])}`)
    .join('&');
  const tokenRes = http.get(`${realm}?${query}`, { responseType: 'text' });
  if (tokenRes.status !== 200) fail(`docker token: ${tokenRes.status}`);
  const body = tokenRes.json();
  const token = body.token || body.access_token;
  tokens[url] = { token, until: Date.now() + 240000 };
  return token;
}

// ---------------------------------------------------------------------------- scenario functions
export function hitSmall() {
  const res = http.get(BASE + pick(T.hit_small));
  if (ok(res, 200, 'hit_small')) {
    metrics.hit_small_ttfb.add(res.timings.waiting);
    metrics.hit_content_ttfb.add(res.timings.waiting);
    countHit(res);
  }
}

export function hitRange() {
  const offset = Math.floor(Math.random() * (T.large_bytes - 65536));
  const res = http.get(BASE + T.hit_large, { headers: { Range: `bytes=${offset}-${offset + 65535}` } });
  if (ok(res, 206, 'hit_range')) {
    metrics.hit_range_ttfb.add(res.timings.waiting);
    metrics.hit_content_ttfb.add(res.timings.waiting);
    countHit(res);
  }
}

export function hitLarge() {
  const res = http.get(BASE + T.hit_large, { timeout: '600s' });
  if (ok(res, 200, 'hit_large')) {
    metrics.hit_large_ttfb.add(res.timings.waiting);
    metrics.hit_content_ttfb.add(res.timings.waiting);
    metrics.hit_large_mbps.add(mbps(T.large_bytes, res));
    countHit(res);
  }
}

export function metadata() {
  const [url, accept] = pick(T.metadata);
  const res = http.get(BASE + url, { headers: { Accept: accept } });
  if (ok(res, 200, 'metadata')) {
    metrics.metadata.add(res.timings.duration);
    countHit(res);
  }
}

export function docker() {
  const [url, accept] = pick(T.docker);
  const token = dockerToken(url);
  const params = { headers: { Accept: accept, ...(token ? { Authorization: `Bearer ${token}` } : {}) } };
  const head = http.head(BASE + url, params);
  if (ok(head, 200, 'docker HEAD')) {
    metrics.docker_manifest.add(head.timings.duration);
    metrics.docker_manifest_head.add(head.timings.duration);
  }
  const get = http.get(BASE + url, params);
  if (ok(get, 200, 'docker GET')) {
    metrics.docker_manifest.add(get.timings.duration);
    metrics.docker_manifest_get.add(get.timings.duration);
    countHit(get);
  }
}

// Misses: the same never-fetched file straight from the origin, then through Waystation (a miss), one at a time.
export function missSmall() {
  const i = exec.scenario.iterationInTest;
  const direct = http.get(T.miss_small_direct[i]);
  if (ok(direct, 200, 'miss_small direct')) metrics.miss_ttfb_direct.add(direct.timings.waiting);
  const proxied = http.get(BASE + T.miss_small[i]);
  if (ok(proxied, 200, 'miss_small proxied')) metrics.miss_ttfb_proxied.add(proxied.timings.waiting);
}

export function missLarge() {
  const i = exec.scenario.iterationInTest;
  const direct = http.get(T.miss_large_direct[i], { timeout: '600s' });
  if (ok(direct, 200, 'miss_large direct')) metrics.miss_mbps_direct.add(mbps(T.miss_large_bytes, direct));
  const proxied = http.get(BASE + T.miss_large[i], { timeout: '600s' });
  if (ok(proxied, 200, 'miss_large proxied')) metrics.miss_mbps_proxied.add(mbps(T.miss_large_bytes, proxied));
}

export function setup() {
  if (SCENARIO !== 'api') return {};
  const res = http.post(
    `${BASE}/api/v1/auth/login`,
    JSON.stringify({ username: 'admin', password: __ENV.PERF_ADMIN_PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, responseType: 'text' },
  );
  if (res.status !== 200) fail(`login: ${res.status}`);
  const cookie = Object.entries(res.cookies)
    .map(([name, values]) => `${name}=${values[0].value}`)
    .join('; ');
  return { cookie };
}

function apiGet(data, endpoint, path, responseType = 'none') {
  const res = http.get(BASE + path, { headers: { Cookie: data.cookie }, responseType, tags: { endpoint } });
  if (ok(res, 200, `api ${endpoint}`)) {
    metrics.api.add(res.timings.duration);
    apiTrends[endpoint].add(res.timings.duration);
  }
  return res;
}

export function api(data) {
  const n = Math.floor(Math.random() * BULK_COUNT);
  const group = String(Math.floor(n / 10000)).padStart(3, '0');
  const pkg = String(Math.floor(n / 100) % 100).padStart(2, '0');
  const prefix = `artifact-${String(n).padStart(7, '0').slice(0, 5)}`;
  switch (exec.vu.iterationInScenario % API_ENDPOINTS.length) {
    case 0:
      apiGet(data, 'search', `/api/v1/search?q=${prefix}&limit=50`);
      break;
    case 1:
      apiGet(data, 'search_repo', `/api/v1/search?q=${prefix}&repository=perf-bulk&facets=false&limit=50`);
      break;
    case 2: {
      const first = apiGet(data, 'search_page2', `/api/v1/search?q=artifact&facets=false&limit=50`, 'text');
      const cursor = first.status === 200 ? first.json('next_cursor') : null;
      if (cursor) apiGet(data, 'search_page2', `/api/v1/search?q=artifact&facets=false&limit=50&cursor=${encodeURIComponent(cursor)}`);
      break;
    }
    case 3:
      apiGet(data, 'search_all', '/api/v1/search?limit=50');
      break;
    case 4:
      apiGet(data, 'browse_root', '/api/v1/repositories/perf-bulk/browse?limit=50');
      break;
    case 5:
      apiGet(data, 'browse_folder', `/api/v1/repositories/perf-bulk/browse?path=group-${group}/pkg-${pkg}/&limit=50`);
      break;
    default:
      apiGet(data, 'repositories', '/api/v1/repositories');
  }
}

export function concurrent() {
  const url = Math.random() < 0.8 ? pick(T.hit_small) : pick(T.hit_medium);
  const res = http.get(BASE + url, { timeout: '120s' });
  if (ok(res, 200, 'concurrent')) metrics.concurrent.add(res.timings.duration);
}

// ---------------------------------------------------------------------------- results
export function handleSummary(data) {
  const out = { scenario: SCENARIO, metrics: {} };
  for (const [name, metric] of Object.entries(data.metrics)) {
    if (name.startsWith('ws_') || name === 'http_reqs' || name === 'http_req_failed' || name === 'iterations') {
      out.metrics[name] = metric.values;
    }
  }
  return {
    [`${__ENV.PERF_RESULTS || '/results'}/k6-${SCENARIO}.json`]: JSON.stringify(out, null, 1),
    stdout: `${SCENARIO}: ${Object.entries(out.metrics)
      .filter(([n]) => n.startsWith('ws_'))
      .map(([n, v]) => `${n} ${v['p(95)'] !== undefined ? `p95=${v['p(95)'].toFixed(2)} med=${v.med.toFixed(2)} n=${v.count}` : `count=${v.count}`}`)
      .join('\n  ')}\n`,
  };
}
