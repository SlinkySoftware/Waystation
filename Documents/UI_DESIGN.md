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
- **Minimal permissions** (T-U10, SPEC §4.10): every page route names the session capability it needs
  (`meta.capability`, the same as its rail item: Repositories/Browse/Search `repositories.browse`, Activity/Storage
  `stats.read`, Cleanup `repositories.manage`, Tasks and the admin pages their admin capability). The rail and the
  command palette show only what the session holds; a link or typed URL to anything else shows the calm "No access
  to this page" page (illustration, one sentence, back to the dashboard) without asking the server. A user without
  `stats.read` gets no live indicator and no event stream; the dashboard becomes a landing: "Your repositories"
  (tiles linking to each, Browse, Search) or, with no role at all, "You are signed in, but have no access yet" with a
  link to the profile. Repository pages hide live transfers, activity and upstream health without `stats.read` and
  the Cleanup/Settings tabs without `repositories.manage`. A load refused with 403 anyway (capabilities changed
  mid-session) shows "Not available to you" instead of an error with a retry; never an error toast or loop.
- **Top bar**: product mark + instance name, global search box (opens command palette `Ctrl/⌘ K`), live
  indicator (pulsing dot + active transfer count; click opens the live drawer), theme toggle, help, user menu
  (profile, my API tokens, sign out).
- **Command palette**: jump to any repo, component, page or action ("create docker proxy", "copy npm config").
- **Breadcrumbs** on detail pages. **Toasts** for results (`useNotify`). **Empty states** with an illustration
  (§8), one sentence and the primary action.
- **Low-disk banner** (T-O03) under the top bar while the low-disk guard is tripped (`GET /storage/guard`, checked
  every minute and at once on a `storage` activity event): free space, the guard, "new cache fills and uploads are
  refused; cached content is still served", a link to Storage. Dismissible for the session; it comes back when the
  disk runs low again.

## 4. Pages

### 4.1 Sign-in
Centered card with logo, local username/password form, one button per OIDC provider ("Continue with
<name>"), LDAP uses the same form (provider auto-resolved or a select when several). Error messages generic.
Two-factor sign-in (T-O08): when the password is accepted for an account with it on, the same card asks for the 6-digit code from the authenticator app (numeric keyboard, `one-time-code` autofill), with "Use a recovery code" (switches the field to `xxxxx-xxxxx`) and "Back"; a wrong code keeps the step with a generic message; an expired step (5 min) returns to the password form with a note.

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
  "Test upstream" button showing reachability, auth result and sample latency). Advanced sections collapse and
  open by themselves when they hold a setting or a field error: **Caching and resilience** (max ages, stale on error,
  offline, private upstream, auto-block threshold and periods) and **Network and TLS** (outbound HTTP proxy, extra
  CA certificates as PEM). The overview's upstream card shows "Auto-blocked: next probe in m:ss" while blocked.
  **Storage quota** (proxy and hosted; T-O03): on/off, size with a MiB/GiB/TiB unit, mode soft (warn) / hard (refuse
  new writes), field errors from the server.
- Quotas (T-O03): a repository with a quota shows a usage meter (bar with an 80 % mark, "used of quota", a state
  badge: ok / warning from 80 % / over quota; icon and label, not colour alone) in the list's Size cell (compact,
  tooltip) and as a strip under the detail page's KPI row (with the mode explained and the note that shared blobs
  count in each repository).
- "Import from Nexus" (header action, administrators): a page with the connection form (URL, admin credentials
  never kept, private-address and CA options, docker alias/default choices), then the plan as a table (Nexus kind →
  Waystation kind, upstream or members, Create / Exists / Not supported / Invalid with the reason, expandable
  field-level differences), checkboxes on the rows it can create (all by default) and "Import N repositories" with a
  confirmation; afterwards each row shows its outcome. Cards on phones.
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
  npm (T-P5-NPMH): hosted repositories show "N versions" and a Versions column instead of the cache columns
  (everything they list is stored); dist-tags and deprecation badges as for proxies.
  Built for Maven (T-P5-MAVEN): artifacts table (artifactId over its groupId, latest, version count, filter by
  groupId or artifactId) → versions table (Maven order, SNAPSHOT badge, files, size, deployed by; a copyable
  `<dependency>` snippet) → the version's files (name, size, modified, copy download URL).
  Built for PyPI (T-P5-PYPI): projects table (name, summary, latest, files held / versions, size) → project page
  (summary, latest, `pip install` snippet; versions newest first with pre-release and yanked badges, expandable to
  their wheels and sdist with kind, size, cache state, PEP 658 metadata badge, uploader, short sha256). Hosted
  repositories add Yank / Un-yank per version (dialog with an optional reason); groups show their members to browse.
- **Go** (T-P5-GO): modules table (module path, latest, tagged versions, cached versions, size) → module page
  (latest, `go get <module>@<latest>` snippet; versions newest first in semver order with pseudo-version,
  pre-release, `+incompatible` and "not in version list" badges, commit time, cache state, expandable to the
  `.info`/`.mod`/`.zip` files held with size, cached time, copyable sha256 and a per-version `go get` snippet).
  Groups show their members to browse. The repository form's Go options: checksum database proxy and verification
  toggles, database name and URL, private module patterns (GONOSUMDB syntax, chips).
- **Terraform** (T-P5-TFP): providers table (address `<hostname>/<namespace>/<type>`, latest, versions listed,
  archives cached, size) → provider page (latest, a copyable `required_providers` entry; versions newest first with
  pre-release and "not in version list" badges, protocols, platform count, cache state, expandable to the archives
  held per platform with size, cached time, copyable `zh:` and `h1:` hashes, and the platforms the registry lists).
  The repository form's Terraform options: origin registries (chips) and the signature verification toggle.
- **apt / yum**: suites/components/arches tree → package list with versions and arch.
- **Raw**: folder listing.

### 4.5 Search
Full-page results with facet sidebar (format, repository, kind, type, cached only), result rows with icon, name, version,
repo, size, last downloaded; digest search ("sha256:..." shows every asset using that blob).

### 4.6 Activity
Live, filterable event log (virtual scroll, follows tail until the user scrolls): time, type icon, principal,
repo, artifact, outcome, duration, bytes. Pause/resume, export CSV for the selected range.
As built (T-U07): newest first; following = new events appear at the top in 4 Hz batches; scrolling away from the
top or pressing Pause holds them behind an "N new events" pill (Resume or scrolling back to the top applies them).
Filters (URL keys `kind`, `repo`, `format`, `user`, `cache`, `outcome`, `range` or `from`/`to`) apply to history and
the live tail alike; a window ending in the past shows "History" and does not follow. Rows open a side drawer with
every field and one-click narrowing. Export CSV downloads the current filter. Columns collapse at < 1360 px (who and
duration move to the drawer) and become two-line rows < 900 px.

### 4.7 Storage
Total and free space (from the data dir), growth chart, by-repo table (bytes, % of total, assets, de-dup savings),
largest artifacts, GC status (last run, bytes reclaimed, next run).
As built (T-U07): KPIs (stored, free, saved by de-duplication, blobs), data volume bar (Waystation blobs / other
files / free, warning under 15 % free), by-format donut, largest repositories bar list, repository table (size,
share bar, artifacts, average artifact size; sort and filter in the URL; link to the repository's activity). Growth
chart, largest artifacts and per-repository de-dup savings wait for storage snapshots. The Blob GC card (T-O02, shared with
the Cleanup page) shows GC status to admins. The **Low-disk guard** card (T-O03) shows the state (with "since"
when tripped), free space, the effective floor and how it is worked out; `settings-admin` users edit the minimum free
GiB and percent in a dialog.

### 4.8 Cleanup
Policies list → editor with criteria builder; **Preview** shows matches (count, bytes, sample table) before
saving; run history with reclaimed bytes.
As built (T-O01/T-O02): the policies table (format, criteria chips with protections marked, attached repositories,
last run with progress, schedule and next run, enabled toggle, run now / history / edit / delete); the editor is a
wide dialog with the form on the left (criteria read as sentences with a switch and an inline value; schedule
presets manual/daily/weekly/custom cron with a plain-language reading) and a **live impact panel** on the right
("N items · X across M repositories", per-repository bars, debounced dry run) that opens the paged candidate table.
A **Blob GC** card on the same page (admins) shows the last run, reclaimed bytes, what waits out the grace period,
the next run and "Run now"; the Storage page shows the same card. The repository detail page has a **Cleanup** tab for
users who may manage the repository: attached policies in run order (attach compatible policies, reorder, detach)
and what they would remove from this repository.

### 4.9 Tasks and workers
Scheduled tasks table (kind, schedule, last result, next run, run now), job runs with progress bars and log
viewer (live), workers list (host, version, queues, current jobs, heartbeat), dead-letter jobs with retry.
As built (T-O04, admin only): a **summary strip** (workers online, jobs running, jobs waiting with the oldest wait,
dead letters tinted when non-zero; each tile opens its tab), then tabs in the URL (`?tab=`): **Scheduled** (task
named after its kind or owning policy, schedule in words plus the raw cron, last run with progress or error, next
run, enabled toggle (pausing the stats rollup asks first); run now, its runs, edit schedule in a dialog with presets
and a live reading, or "open the policy" for cleanup tasks); **Runs** (status, job, trigger and attempt, started,
duration, progress bar or a one-line result, worker; filters status/job/task/attempts/routine in the URL; "Load
more"; rows update live, new runs slide in); **Workers** (a card per worker: leader badge, health pill, version,
concurrency, heartbeat age counting up, queues, running jobs that open their run; a queue table with waiting,
running, delayed, oldest waiting and the consuming workers, warning when none); **Dead letters** (job, last error,
attempts, when it gave up, payload on expand, Retry and a confirmed Discard; malformed entries can only be
discarded). A run opens a **side drawer** (`?run=`) with status, trigger, facts, result figures, the error and a
monospace log viewer (Live badge, Follow toggle, copy). Phones get cards instead of tables.

### 4.10 Administration
Users (source badge local/LDAP/OIDC, roles, last login, disable, reset password for local), Groups (source,
mapped roles), Roles (permission matrix editor: actions × scope rows), Auth providers (LDAP form with "Test
connection" and "Test user lookup"; OIDC form with discovery "Fetch" and "Test login"), API tokens (all tokens,
revoke), Audit log (filters in the URL: time range, actor, action, target, outcome; detail drawer with the
change diff, secrets shown only as "changed"; CSV/NDJSON export), Settings (general, docker routing, security, stats retention, UI),
System (versions, Mongo/Valkey health, storage path, support bundle).
As built (T-U08): **Users**: table (initials, display name and username, source badge Local/LDAP/OIDC with the
provider in a tooltip, direct roles as chips (administrator in amber), local group count, last sign-in, status badge
Active/Disabled/Locked/Password change pending/Awaiting first sign-in), filters in the URL (`q` username prefix,
`source`, `status`, `role`), "Load more" keyset paging, a row menu (open, edit, reset password for local accounts,
unlock, reset two-factor sign-in (when it is on; not for yourself; confirm dialog), sign out everywhere, disable/enable, delete with the username typed to confirm; never disable or delete
yourself). The user page (`/admin/users/<id>`): account facts, access (direct roles, local and mapped groups, the
groups a provider last reported), linked accounts, an effective-permissions panel (roles with where they come from,
global permissions, a repository × action matrix), sessions (browser, address, started, last used, expiry; sign out
everywhere) and the user's API tokens. Create user: password optional (generate button, copy, one-time by default);
roles the caller may not hand out are disabled in the picker. **Groups**: tabs All/Local/Mapped, local groups with
description, roles and a members button (dialog: add by username search, remove); mapped groups show the provider
group and provider and take role changes only. **Roles**: a card per role (built-in/custom, permission summary per
scope, "held by" counts), a dialog editor with the permission matrix (one row per scope × browse/read/write/delete/
manage, write/delete/manage tick browse and read, "matches N repositories" per row, global actions as tiles that
are disabled when the caller does not hold them); built-ins open read-only with "Duplicate". The **Anonymous
access** card below lists which repositories anonymous requests could read. **API tokens**: tab Tokens (search,
owner and status filters, revoke) and tab Service accounts (table; side panel with the account's tokens (create:
the profile's token dialog, secret shown once) and permissions). **Settings**: General (instance name), Two-factor sign-in (toggle "Require two-factor sign-in for
local administrators", saved at once; turning it on for yourself takes you to the profile page), Docker
routing (default repository, aliases), and the environment limits read-only with their variable names. **System**:
version tiles, MongoDB/Valkey cards (status, version, round trip), the data volume bar with a marker at the
free-space floor; refreshes every 15 s. Refusals: 403 shows "Not allowed", 409 "Not possible", with the server's
reason (dialogs for row actions, a banner in forms). Phones: every table becomes cards with the actions inline.
**Webhooks** (T-O05, `/admin/webhooks`, rail item for `admin`): one table (webhook icon, name, target URL in mono,
description; events in words ("All events", "Content: upload, delete"); repository scope; last delivery as a status
pill Delivered/Queued/Retrying/Failed with when, the event and "HTTP 503 · 198 ms"; on/off toggle; row actions send
test event, deliveries, and a menu with edit and delete (confirm)). The editor (dialog): name and on/off, payload URL,
description; events as "Every event" or grouped checkboxes with a line of help each; repositories as chips (names or
patterns, suggestions from the catalogue); signing secret pre-filled with a 64-hex random value generated in the
browser (show/hide, copy, Generate; on edit empty = keep), a collapsible "Verifying requests" note; custom headers as
name/value rows (values masked; on edit the stored names show with "Replace headers"); delivery limits (timeout,
attempts) and "Allow private network targets" with its risk explained. The delivery drawer (right side): tabs
All / Queued-Retrying / Failed / Delivered, rows with status, event, repository, result, attempts and next attempt,
"Load more"; a delivery opens in place (back arrow) with facts (event, ids, times, redelivery link), Redeliver, the
attempts newest first (status icon, HTTP status, latency, error, the answer's first KiB in mono) and the request
(headers with secret values masked, the JSON body pretty-printed). Sending a test event opens its delivery; the
drawer and the table refresh every few seconds while something is queued or retrying. Phones: cards, full-screen
drawer.

**Provisioning** (T-O09, `/admin/provisioning`, rail item for `admin`): without a configured file, an empty state
explaining `WAYSTATION_PROVISION_FILE`. With one: two fact tiles (the file path in mono; the last apply as an
outcome pill, when, by whom and the short sha256), then the plan card: counts (to create / update / delete,
unchanged, failed) in the header, the validation errors in a red block when the file is invalid ("nothing can be
applied", each with its dotted path in mono), and one row per object: an action pill (Create green, Update blue,
Delete red, Unchanged grey), the kind and name, a note (released objects), the apply outcome and error, and the
field changes in mono (`field: before → after`; secrets only "secret changed"). Unchanged rows are hidden behind a
"Show unchanged (n)" toggle; "Everything matches the file" when there is nothing to do. Header actions: Plan changes
and Apply (confirm with the number of changes; disabled when invalid or nothing changes). **Managed objects**: a
small "Managed" label (lock icon, brand outline, tooltip "Declared in the provisioning file...") next to the name of
repositories (list, detail header, settings tab), roles, groups, service accounts, sign-in providers, cleanup
policies and webhooks; their forms open read-only (no Save; the caption repeats the hint), and delete, enable/disable
and edit actions are disabled or hidden.

### 4.11 Profile
Display name, theme, density, password (local accounts; while a one-time password is pending the router keeps
the user here and only this card is usable), two-factor sign-in (local accounts: state badge and recovery codes
left; "Set up" opens a dialog: current password, then the QR code drawn in the browser as an SVG (no external
service; dark on white whatever the theme) with the setup key in groups of four to copy, then a code from the
app; the 10 recovery codes follow in a persistent dialog with copy and download (a text file made in the
browser) that closes only through "I have saved them"; "New recovery codes" (needs a code) and "Turn off"
(password + code or recovery code); a warning when policy requires it, and the router keeps the user here until
it is on), my API tokens (create with scopes/expiry, shown once with copy), sessions.

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
  (`make frontend-build-mock`) serves the same tour. Screenshot tour: `scripts/screenshots.mjs` (Playwright image; `REAL=1` tours a running stack instead of mocks, as
  for the M0 tour in `Documents/Notes/UI_TOUR_M0.md`). The mocks apply the server's route rules (`mocks/access.ts`:
  403 `permission_required`, hidden repositories 404, filtered listings, a refused event stream without
  `stats-read`); sign in as `nina` (no role), `reed` (reader), `gus` (the anonymous-access grants only) or `grace`
  (viewer) to see minimal-permission sessions. `make ui-smoke` (`scripts/role-smoke.mjs`, Playwright on the mock
  build) opens every rail page as each of them and fails on any console error or refused request.
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
