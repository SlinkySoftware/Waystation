# Waystation UI design

Goal: it should look and feel like professional, commercial-grade software from the first screen: calm,
information-dense where it matters, fast, and alive (activity visibly moving). Built with Quasar 2 / Vue 3 /
TypeScript, following the conventions of the owner's other apps (DeviceVault, fw-data-manager, bgpmon) so code
and patterns carry over.

## 1. What to borrow (and what to avoid)
| Source | Borrow | Avoid |
|---|---|---|
| Artifactory | "Set Me Up" per repo per client with copy buttons and token injection; package view grouped by format | Dense, dated chrome; modal-heavy flows |
| Nexus Repository | Repository list as the backbone; browse tree; component/asset split; cleanup preview | Wizard-like admin forms; tiny fonts; no live data |
| Harbor | Project/artifact pages: tags table with digest, size, pushed, pulled; platform badges; layer view | Over-nested navigation |
| GitHub Packages | Package page: clean header, install snippet front and centre, versions list, download sparkline | – |
| Grafana / Datadog | Live charts with time-range picker, crosshair tooltips, consistent units | Dashboard-builder complexity |
| Linear / Vercel | Typography, spacing, keyboard-first (command palette), subtle motion, empty states | – |
| DeviceVault (own) | Design tokens, macOS-style dark mode, `PageCard`, `FormDialog`, `useCrud`, `usePolling`, SVG charts with `--chart-*` vars | – |

## 2. Design system
- **Tokens**: start from DeviceVault `src/css/app.css` (semantic variables, light and dark). Add Waystation
  brand tokens: `--brand-500` (deep teal `#0f766e`), `--brand-400`, `--brand-600`, plus status colours
  (`--status-ok/-degraded/-blocked/-offline`), cache colours (`--cache-hit` blue, `--cache-miss` amber,
  `--cache-stale` violet, `--cache-error` red: colour-blind separable, always paired with an icon or label).
- **Themes**: light, dark (macOS-inspired greys, hairline borders), system. Persist per user (server-side setting
  + localStorage for first paint).
- **Typography**: Inter (variable, self-hosted woff2) for UI, JetBrains Mono for digests, paths, commands.
  Sizes 12/13/14/16/20/24/32; tabular numerals for all metrics (`font-variant-numeric: tabular-nums`).
- **Spacing**: 4 px base grid; cards 16/24 px padding; consistent 8 px gaps in toolbars.
- **Density**: comfortable by default, compact toggle for tables (persisted).
- **Icons**: Material Symbols (outlined) via `@quasar/extras`; **format icons** as a bundled SVG sprite (Docker,
  npm, PyPI, Maven, Debian, Ubuntu, Raspberry Pi, Red Hat/Rocky/Alma, Go, Helm, Terraform, OpenTofu, Ansible,
  Swift, NuGet, Rust, Ruby, PHP, Conda, Alpine, Hugging Face) from Simple Icons (CC0), monochrome by default,
  brand colour on hover/selection.
- **Motion**: 150-200 ms ease-out for panels; progress bars animate smoothly (CSS transitions on width);
  respect `prefers-reduced-motion`.
- **Numbers and units**: binary units (KiB/MiB/GiB) for sizes, SI for rates (MB/s, req/s), relative times with
  absolute tooltip, consistent precision (3 significant digits).
- **No external requests**: fonts, icons, images all bundled (works offline on an isolated LAN).

## 3. Shell
- **Left navigation rail** (collapsible to icons; drawer on mobile): Dashboard, Repositories, Browse, Search,
  Activity, Storage, Cleanup, Tasks, then an Administration group (Users, Groups, Roles, Auth providers,
  API tokens, Audit log, Settings, System). Items hidden when the user lacks permission.
- **Top bar**: product mark + instance name, global search box (opens command palette `Ctrl/⌘ K`), live
  indicator (pulsing dot + active transfer count; click opens the live drawer), theme toggle, help, user menu
  (profile, my API tokens, sign out).
- **Command palette**: jump to any repo, component, page or action ("create docker proxy", "copy npm config").
- **Breadcrumbs** on detail pages. **Toasts** for results (`useNotify`). **Empty states** with an illustration
  (§8), one sentence and the primary action.

## 4. Pages

### 4.1 Sign-in
Centered card with logo, local username/password form, one button per OIDC provider ("Continue with
<name>"), LDAP uses the same form (provider auto-resolved or a select when several). Error messages generic.

### 4.2 Dashboard (landing)
Grid, responsive (12 cols ≥ 1440, 8 cols ≥ 1024, 1 col mobile):
1. **KPI tiles** (each with sparkline over the selected range): Requests/s (live), Cache hit ratio, Bandwidth
   served, **Bandwidth saved**, Storage used (with free space bar), Artifacts cached, Active transfers.
2. **Live transfers** panel (the "alive" centrepiece): rows animate in/out; columns: format icon, repo chip,
   artifact (mono, truncated middle), client (user or IP), cache state badge (SPEC §4.7 vocabulary: HIT,
   FETCHING, COALESCED (with follower count), STALE, REVALIDATED, ERROR), progress bar with bytes and %,
   throughput, ETA, upstream latency. Pausable; filter by repo/format.
   Completed rows fade after 5 s; failures stay highlighted 30 s.
3. **Traffic** chart: stacked area of served bytes split hit vs upstream, or requests by format; range picker
   (15 m, 1 h, 24 h, 7 d, 30 d), crosshair tooltip.
4. **Format breakdown**: donut of storage by format + legend with sizes.
5. **Upstream health**: card per proxy repo: status pill, error rate, p95 latency, last error, auto-block
   countdown, Docker Hub rate-limit gauge ("142 / 200 pulls left, resets in 3 h").
6. **Top artifacts** (downloads / bytes, toggle) and **Recently added**.
7. **Recent activity** feed (pushes, deletes, cleanup runs, failures), linking to details.
All widgets update live via SSE; no page refresh, no polling except as SSE fallback.

### 4.3 Repositories
- Table (or card grid toggle): format icon, name, type chip (Proxy / Hosted / Group with distinct icon), status
  pill, URL (copy button), size, artifacts, hit ratio (24 h), requests (24 h) sparkline, last activity.
  Filters: format, type, status; search box.
- "Create repository" → format picker (large icon tiles) → type → form (FormDialog, sectioned, inline validation,
  "Test upstream" button showing reachability, auth result and sample latency).
- Repository detail page: header (icon, name, type, status, URL copy, **Set me up** button), KPI strip, tabs:
  **Overview** (charts for this repo, upstream health, top artifacts), **Browse**, **Packages**, **Activity**,
  **Settings**, **Cleanup** (attached policies + last runs + preview), **Members** (groups).
- **Set me up** side panel: tabs per client (e.g. docker: daemon.json mirror, docker login, pull example;
  npm: `.npmrc`, `npm login`, yarn/pnpm variants), user's token inserted as a masked placeholder with "generate a
  token" link; copy buttons; OS variants where relevant.

### 4.4 Browse
Two-pane: left virtualised tree (lazy-loaded paths), right asset/folder detail. Format-aware views:
- **Docker**: image list → tags table (tag, digest short, platforms as badges, size, created, last pulled, pulls)
  → manifest page (platform switcher, layers table with sizes and a stacked size bar, config/history view,
  referrers (signatures, SBOM) list, pull commands by tag and by digest).
- **npm / PyPI / Maven / NuGet / Helm / Galaxy / Swift**: package → versions table (version, published, size,
  downloads, deprecated/yanked flags), README rendered safely (sanitised markdown, no raw HTML), dependencies.
- **apt / yum**: suites/components/arches tree → package list with versions and arch.
- **Raw**: folder listing.

### 4.5 Search
Full-page results with facet sidebar (format, repository, kind, type, cached only), result rows with icon, name, version,
repo, size, last downloaded; digest search ("sha256:..." shows every asset using that blob).

### 4.6 Activity
Live, filterable event log (virtual scroll, follows tail until the user scrolls): time, type icon, principal,
repo, artifact, outcome, duration, bytes. Pause/resume, export CSV for the selected range.

### 4.7 Storage
Total and free space (from the data dir), growth chart, by-repo table (bytes, % of total, assets, de-dup savings),
largest artifacts, GC status (last run, bytes reclaimed, next run).

### 4.8 Cleanup
Policies list → editor with criteria builder; **Preview** shows matches (count, bytes, sample table) before
saving; run history with reclaimed bytes.

### 4.9 Tasks and workers
Scheduled tasks table (kind, schedule, last result, next run, run now), job runs with progress bars and log
viewer (live), workers list (host, version, queues, current jobs, heartbeat), dead-letter jobs with retry.

### 4.10 Administration
Users (source badge local/LDAP/OIDC, roles, last login, disable, reset password for local), Groups (source,
mapped roles), Roles (permission matrix editor: actions × scope rows), Auth providers (LDAP form with "Test
connection" and "Test user lookup"; OIDC form with discovery "Fetch" and "Test login"), API tokens (all tokens,
revoke), Audit log (filterable, export), Settings (general, docker routing, security, stats retention, UI),
System (versions, Mongo/Valkey health, storage path, support bundle).

### 4.11 Profile
Display name, theme, density, my API tokens (create with scopes/expiry, shown once with copy), sessions.

## 5. Responsive behaviour
- ≥ 1440: rail expanded, 12-col dashboard. 1024-1439: rail collapsed to icons. 600-1023: drawer nav, 2-col
  tiles, tables keep key columns (others in an expandable row). < 600: single column, tables become cards,
  live transfers show name + progress + speed only, sticky bottom action bar on forms.
- Touch targets ≥ 40 px; no hover-only affordances.

## 6. Accessibility
WCAG 2.2 AA: contrast ≥ 4.5:1 for text in both themes, focus rings visible, every icon button labelled,
charts have an accessible table alternative ("View data"), live regions throttled (announce summaries, not
every progress tick), full keyboard operation, no information by colour alone.

## 7. Front-end engineering rules
- **Mock mode first** (owner priority on speed and early feedback): `npm run dev:mock` runs the whole UI on MSW
  handlers generated from the contract-first OpenAPI (TASKS T-V01) with seeded fixtures and a fake live-event
  generator, so every page can be built and reviewed before its server endpoint exists. Keep mock mode working
  after the real server lands (it is the fastest way to iterate on visuals and to take screenshots).
  Where things live: contract `Application/Web/openapi/waystation-m0.yaml`, generated types `src/api/schema.d.ts`,
  mocks `src/mocks/` (`handlers.ts`, `world.ts` = live generator, `fixtures/`), live store `src/stores/live.ts`
  (mock mode swaps its source for the generator), strings `src/i18n/en.ts`. Run it with `make frontend-mock`
  (http://localhost:9000/ui/, any username/password; password `wrong` shows the error state). A static mock build
  (`make frontend-build-mock`) serves the same tour. Screenshot tour: `scripts/screenshots.mjs` (Playwright image).
- `<script setup lang="ts">` only, strict TS, ESLint + vue-tsc in CI; components < 300 lines; composables for
  data (`useCrud`, `useLiveStream(topic)`, `useStats(range)`, `useNotify`, `useCopy`).
- API only through the generated `openapi-fetch` client in `src/api/`; SSE via one shared `EventSource`
  connection multiplexing topics (`stores/live.ts`), with automatic reconnect + `Last-Event-ID`.
- Lists > 100 rows use `q-virtual-scroll`. Charts are SVG components in `components/charts/` (sparkline, area,
  stacked area, donut, bar list, gauge) driven by CSS variables; ≤ 60 fps, no re-render storms (throttle to 4 Hz).
- Route-level lazy loading; initial route JS < 250 kB gzip (CI bundle budget).
- i18n-ready (strings via a single `t()` with English catalogue), dates in the user's locale and timezone.
- Visual regression: Playwright screenshots of key pages in both themes at 3 widths (CI artifact, reviewed on change).

## 8. Brand
Name: **Waystation**. Tagline: "Every package, one stop."
- **Mark**: a waypoint marker whose head is a hexagon (a package seen end-on) holding a stack: a top slab and two
  layers. Deep teal marker (`--brand-500`) with the glyph in `--text-on-brand`, so it follows light and dark; a
  **mono** variant paints the marker in `currentColor` with the glyph cut out (use it on the brand teal or on photos);
  below 20 px a 16 px-tuned glyph (slab + one layer, pixel-aligned) replaces the full one.
- **Wordmark**: "waystation" in a monoline geometric lowercase drawn for the mark (round caps, one stroke weight,
  16-unit bowls). The lockup is the mark centred on the x-height band, then the wordmark. In the app the top bar and
  sign-in page show the mark next to the instance name (configurable), so the wordmark is for docs, the README and
  external material.
- **Source of truth**: `Application/Web/src/assets/brand/geometry.ts` holds the paths and colours. `BrandMark.vue`
  draws from it; `make brand` (`scripts/gen-brand.mjs`, resvg in the dev image) regenerates the master SVGs next to
  it (`mark`, `mark-dark`, `mark-mono`, `mark-small`, `wordmark`, `lockup`, `lockup-dark`, `app-tile`) and the web
  set in `public/`: `favicon.svg` (switches to the dark-theme colours with `prefers-color-scheme`), `favicon-16/32/48.png`,
  `favicon.ico`, `apple-touch-icon.png` (180, opaque tile), `icon-192/512.png`, `icon-maskable-512.png` (mark inside
  the 80% safe zone) and `manifest.webmanifest`. Outputs are committed; `make brand-check` (part of
  `make frontend-check`) fails when the committed SVGs or manifest drift from the geometry.
- **Empty-state illustrations** (`EmptyIllustration.vue`, used through `EmptyState`'s `illustration` prop): one line
  weight, a brand-tinted backdrop, one accent, painted only through theme variables (no hard-coded colours, no
  requests). Set: `repositories` (no repositories yet), `cache` (nothing cached yet), `search` (no results / no
  match), `activity` (no activity or transfers), `error` (failed to load), `notFound` (404 and hidden objects),
  `soon` (planned page or renderer).
