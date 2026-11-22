# Waystation tasks

This board lists open work only. Completed and dropped tasks are archived in
[History/BUILD_HISTORY.md](History/BUILD_HISTORY.md); user-facing release notes are in [CHANGELOG.md](CHANGELOG.md).

How tasks are worked:
- Pick a TODO task whose dependencies are done, set it to IN PROGRESS, and work only on that task. One branch and
  PR per task; commit messages start with the task ID (`T-XXX: summary`).
- Statuses: TODO, IN PROGRESS, BLOCKED (say why). A task that will not be done is moved to the history as DROPPED
  with the reason.
- Acceptance criteria are the minimum; the Definition of Done for formats is in
  [FORMATS.md](Architecture/FORMATS.md).
- Finishing a task, in the same change: remove its row here and append it to
  [History/BUILD_HISTORY.md](History/BUILD_HISTORY.md) as DONE with the date and a summary of what landed; add a
  dated [CHANGELOG.md](CHANGELOG.md) entry; update the specification and architecture documents so they describe
  the system as built.
- New follow-up work found while finishing a task gets its own row in the backlog below.

## Release

| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-R03 | v1.0.0 release: multi-arch images, SBOM, provenance, release notes | R01, R02 | TODO | `v1.0.0` tag matching `VERSION`; `release.yml` publishes multi-arch (amd64, arm64) images with SBOM and provenance attestations; CHANGELOG `[1.0.0]` section and GitHub release notes |

## Backlog (post-1.0)

Follow-ups recorded when the 1.0 tasks closed. None blocks the release.

| ID | Area | Task | Status |
|---|---|---|---|
| T-B01 | Docker | Referrers API for proxy repositories; podman in the client harness | TODO |
| T-B02 | Groups | Browse and search views of a group repository (members are browsed on their own today) | TODO |
| T-B03 | npm | pnpm, Yarn and Bun publish in the client harness; drop a cleaned-up hosted version from the packument immediately; `npm star`/`owner`/`access`/`team` and web login | TODO |
| T-B04 | apt | Hosted: key import, rotation and export; source packages; several distributions per repository; `Contents`/`Translation` indexes; Ubuntu and Raspberry Pi OS hosted client runs. Run `apt.refresh` through the prefetch route | TODO |
| T-B05 | yum | yum 3 (EL7) clients; worker-side `primary` refresh and re-verification of unverified packages; hosted key import, rotation and export; remove a deleted repository's key file; zstd repodata | TODO |
| T-B06 | PyPI | Substring search | TODO |
| T-B07 | Helm | `helm cm-push` in the client harness; global search over charts; provenance verification | TODO |
| T-B08 | Ansible Galaxy | k6 perf scenario; global search over collections; collection signature verification; `ansible-galaxy collection download`/`verify` in the harness; role publishing | TODO |
| T-B09 | Swift | Proxy of another package registry; signature verification; asynchronous publication; release-list pagination; nightly live smoke and k6 perf scenario | TODO |
| T-B10 | Prefetch | Warm items for PyPI (`project==version`), Helm (`chart@version`), Galaxy, Terraform and Swift; a real-client harness step for prefetch | TODO |
| T-B11 | NuGet | Symbol packages (`.snupkg`); the autocomplete resource; NuGet.exe in the harness; nightly live smoke | TODO |
| T-B12 | Composer | Composer 1 `providers`; search/list API and security advisories; a writable group member; nightly live smoke | TODO |
| T-B13 | Conda | JLAP and sharded repodata; group repositories; conda and pixi in the client harness; nightly live smoke | TODO |
| T-B14 | RubyGems | Gem signature verification; Bundler before 2.5 (MD5 ETags); a writable group member; nightly live smoke | TODO |
| T-B15 | Alpine apk | Group repositories; apk v3 packages; upstream index signature checks; nightly live smoke | TODO |
| T-B16 | Hugging Face | Hosted repositories (Hub upload API); group repositories; Spaces; Xet passthrough; `paths-info`; nightly live smoke | TODO |
| T-B17 | Administration | Editable event retention and UI defaults | TODO |
| T-B18 | Tasks | GC grace and parameter editor; retry-all for dead letters; retries-per-kind metric; run blob verification in deep or report-only mode from the UI | TODO |
| T-B19 | Monitoring | Export sign-in failures and process CPU/memory metrics; validate the Grafana dashboard and Zabbix template against live installations | TODO |
| T-B20 | Access | Minimal-permission smoke against a running stack in CI (`REAL=1`); per-repository action hints on repository pages | TODO |
| T-B21 | Migration | Side-by-side validation of the Nexus import against a real Nexus instance | TODO |
