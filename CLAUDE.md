# Waystation: agent instructions

Waystation is a self-hosted, open-source artifact repository: a pull-through cache for public package
ecosystems (Docker/OCI, npm, PyPI, Maven, apt, yum, Go, Helm, Terraform, Ansible Galaxy, Swift, ...) and a
private registry for the owner's own builds. It is a Nexus Repository / Artifactory alternative with no usage caps.
Components: a Python async **server**, Python async **workers**, an nginx **web** tier serving the Vue/Quasar
UI and cached files, **MongoDB** for metadata and stats, **Valkey** for hot-path coordination.

**Asked to deploy, install or operate Waystation (not to develop it)?** Follow
`Documents/Operations/AGENT_DEPLOYMENT.md` instead of this file: it is the runbook for agents deploying Waystation into
someone's environment. Everything below is for working on the code.

## Read before doing anything
1. `Documents/SPECIFICATION.md`: what the app does, requirements, deployment, build status (living document).
2. `Documents/Architecture/ARCHITECTURE.md`: **enforced** rules. Violating them is a bug.
3. `Documents/Architecture/DATA_MODEL.md`: MongoDB collections, Valkey keys, the storage layout.
4. `Documents/Architecture/FORMATS.md`: the per-format protocol notes. Read the section for any format you touch.
5. `Documents/UI_DESIGN.md`: the design system and every page. The UI must look like professional, shipped software.
6. `Documents/TASKS.md`: pick a task whose dependencies are DONE, mark it IN PROGRESS, and work only on that task.

`appidea.txt` is the original brief. It is local only (gitignored, never committed). Never edit it.

## Non-negotiables
- **No environment details in this repo.** It is public. No real hostnames, IP addresses, domains, usernames,
  registry URLs or credentials anywhere: code, docs, tests, fixtures, lockfiles, commit messages. Use
  `example.com` / `registry.example.com` / RFC 5737 / RFC 3849 addresses. Deployment specifics live outside the repo.
  The only exception: the two source-control URLs in "Source control" below (and SPECIFICATION §13.1).
- **Only the server and workers talk to MongoDB and Valkey.** The web tier talks only to the server (and reads the
  blob store read-only for `X-Accel-Redirect`). The UI talks only to `/api/v1/*` and the event stream.
- **Every request is authorised by the RBAC engine** (`waystation_server.security.access`). Never rely on the UI
  hiding something; never check a role name directly; hidden objects return 404, not 403 (one exception, ADR 0005:
  a request with no credentials on `/repository/<repo>/...` gets the same 401 challenge for hidden and missing repos).
- **Never trust upstream content.** Verify every digest/checksum you are given, bound every size, normalise every
  path (no `..`, no absolute paths, no NUL), and never follow redirects to private address space unless the repo
  allows it (SSRF guard).
- **Never block the event loop.** No sync I/O in `async def`. CPU-heavy work (hashing large files, index
  generation, signing) goes to a thread pool or a worker job.
- **Secrets are write-only through the API**, encrypted at rest (`WAYSTATION_SECRET_KEYS`), never logged.
- Never commit secrets. `.env` is gitignored.

## Source control
- Primary repo: `https://github.com/SlinkySoftware/Waystation` (remote `origin`). Every push also goes to the
  secondary `https://git.example.com/SlinkySoftware/Waystation` (remote `gitea`), whose runners build on
  commit. Push to both: `git push origin <ref> && git push gitea <ref>`.
- Authentication comes from the developer's git credential helpers. Never put credentials in remote URLs or `.git/config`.
- Commit messages: `T-XXX: summary` (task ID first), imperative mood. No AI or tool attribution in commits.
- The git repo is created in task T-F01 (SPECIFICATION §13.1).

## Delivery: speed and parallelism (owner priority)
Development speed matters as much as the end state. **Get to a running, clickable UI with basic real functionality
as fast as possible** so the owner can give UI feedback early, then deepen.
- **Milestone M0 "feedback build" comes first** (TASKS.md "Fast track"). Everything not needed for M0 waits.
- **Work in parallel lanes**: platform, server, UI and formats are independent streams. Use subagents and separate
  git worktrees/branches (one per lane or task), merge small PRs often, and keep `main` always runnable.
- **One integration stack at a time per machine.** `make test-integration` (and so `make check`) queues on a
  machine-wide lock: each run starts MongoDB, Valkey, OpenLDAP and Samba AD, and parallel runs ran the dev box out of
  memory on 2026-11-09. Run unit tests, lint and typecheck in parallel freely; a "waiting" message from
  `test-integration` is normal, so don't work around the lock.
- **Contract first, UI never waits for the server**: the M0 API is written as OpenAPI first (T-V01) with MSW mock
  handlers and a realistic fake live-event generator; the UI lane builds against `npm run dev:mock` from day one and
  switches to the real server when endpoints land.
- **Thin slices**: tasks marked `(min)` in the fast track are delivered as the smallest version that works end to
  end; the full task stays TODO for later. Prefer one working end-to-end path over completeness.
- What speed must **not** cut: the Non-negotiables above, ARCHITECTURE rules (X-Accel data plane, package
  boundaries, the single RBAC decision point), and tests for what you ship (unit + one real-client smoke per slice).
  Perf budgets and full client matrices are warnings until M2, failures after.
- After M0 ships, carry on with M1/M2 lanes while UI feedback is collected; apply feedback as it arrives.

## Working rules
- **Docker only.** No host Python or Node. Everything runs through `make` (`make help` lists targets):
  `make dev-up`, `make test`, `make lint`, `make fmt`, `make typecheck`, `make frontend-test`, `make e2e`,
  `make clients-test`, `make perf`, `make lock`, `make images`.
- Python 3.14, uv workspace (`uv.lock` committed), ruff (single quotes, 120 columns), mypy strict, import-linter
  contracts (ARCHITECTURE §3) enforced in CI.
- Frontend: Quasar 2 / Vue 3 / TypeScript strict, `<script setup lang="ts">` only, Pinia, `openapi-fetch` with types
  generated from the server's OpenAPI schema (`make gen-api`; CI fails if the generated file is stale).
  `package-lock.json` committed. Never render untrusted text with `v-html`.
- API change = server route + schema + regenerated client + UI in the same change.
- New format = a plugin under `waystation_server/formats/<format>/` implementing the `Format` protocol, plus real-
  client tests in `tests/clients/<format>/` (FORMATS.md "Definition of done for a format").
- When you finish a task, **in the same change** update: the task status in TASKS.md, SPECIFICATION §14 build
  status, `Documents/CHANGELOG.md` (dated entry), and any spec/architecture section whose behaviour changed.
  The specification always describes what the app does *now*.
- Architecture changes need an ADR in `Documents/Architecture/adr/` and the owner's approval before merging.
- Keep performance budgets green (`Build/ci/budgets.json`; ARCHITECTURE §9). A regression is a failing build.
