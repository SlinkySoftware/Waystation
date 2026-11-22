# OWASP ASVS 4.0.3 Level 2: Waystation checklist

Review T-R01, 2026-11-23, against the code at that date (claims checked in code, not taken from the docs). Status:
**Pass** (met, reference given), **Fixed** (a finding of this review, fixed with a regression test), **N/A** (the
requirement does not apply to Waystation, reason given), **Accepted** (a known gap, with the reason it is acceptable
for v1.0). Paths are relative to `Application/` unless they start with `tests/`, `Build/` or `Documents/`; `S/` is
`Server/waystation_server/`, `C/` is `Common/waystation_common/`, `W/` is `Worker/waystation_worker/`,
`ST/` is `Server/tests/`. Requirements that are Level 3 only, or are process rather than code (for example V1.1.x secure
development lifecycle), are left out. Findings with severities: "Findings" at the end.

## V1 Architecture

| Req | Status | Evidence |
|---|---|---|
| 1.2.1–1.2.3 Component auth, single auth mechanism | Pass | One decision point `S/security/access.py` (`enforce` app-wide dependency, fail closed when a route has no rule); `ST/test_access.py::test_every_route_declares_its_access_rule`, `ST/test_security_matrix.py::test_the_non_api_routes_are_the_reviewed_set` |
| 1.4.1 Access control enforced server side at trusted points | Pass | Router authorises before any plugin runs (`S/routing.py::_dispatch`); the UI is never trusted (CLAUDE.md non-negotiable) |
| 1.4.4 Single, vetted access-control mechanism | Pass | `access.check` / `filter_repos` / `filter_query`; import-linter keeps formats on the decision API only (ARCHITECTURE §3) |
| 1.5.1–1.5.4 Input/output trust boundaries, serialisation, output encoding | Pass | `C/paths.py` (one normaliser), `defusedxml`, `yaml.safe_load` + merge keys refused, no `pickle`/`eval` (grep), Vue templates (no `v-html`; `Web/src/components/MarkdownView.vue` renders an allow-list) |
| 1.6.1–1.6.4 Key management, rotation | Pass | `C/crypto.py` (AES-256-GCM keyring, key id per ciphertext), `W/rotate.py` (`secrets.rotate`), `Documents/Operations/SECRET_KEY_ROTATION.md` |
| 1.8.1–1.8.2 Sensitive data classified and protected | Pass | `C/db/secrets.py::ENCRYPTED_FIELDS` (test-enforced list of every ciphertext) |
| 1.9.1–1.9.2 Encrypted communication between components | Accepted | Server ↔ MongoDB/Valkey run on the internal `backend` network (`internal: true`, `Build/Containers/compose.yaml`) without TLS by default; split-host deployments set TLS in the connection URIs (`DEPLOYMENT.md`). Web → server is plain HTTP inside the `frontend` network |
| 1.11.1–1.11.2 Business logic flows, concurrency | Pass | Single-flight fills across replicas (`S/proxy/fills.py`, Valkey leases), compare-and-set writes (rotation, provisioning) |
| 1.12.2 Uploaded files served safely | Fixed | Artifacts are untrusted content on the UI's origin: `Content-Security-Policy: sandbox` on every non-UI answer (`Build/Containers/web/15-waystation.sh`); raw inline answers of active types are `text/plain` (`S/formats/raw/files.py::safe_type`); `tests/web/run.sh` checks the headers live |
| 1.14.1–1.14.6 Configuration and deployment architecture | Pass | Non-root images, `cap_drop: [ALL]`, `no-new-privileges`, read-only root filesystems, only `web` publishes a port (`Build/Containers/compose*.yaml`); see V14 |

## V2 Authentication

| Req | Status | Evidence |
|---|---|---|
| 2.1.1–2.1.2 Password length 12+, up to 128+ | Pass | `S/security/passwords.py::password_problem` (`MIN_LENGTH` 12, `MAX_LENGTH`) |
| 2.1.3–2.1.6, 2.1.8–2.1.12 No truncation, Unicode, no composition rules, change needs current password | Pass | argon2id over the full value; `S/api/account.py::change_password` |
| 2.1.7 Breached-password check | Accepted | No HIBP lookup (offline installations, no third-party calls). Length 12+, argon2id, lockout and optional TOTP; directories (LDAP/AD/OIDC) apply their own policy |
| 2.2.1 Anti-automation | Pass | Per-address rate limit (`S/security/ratelimit.py`; login, `/auth/token`, TOTP, password change, OIDC start) and per-account lockout (`S/security/login.py` `login_lockout_failures`/`_minutes`); API token failures rate-limited (`S/security/tokens.py`) |
| 2.2.2–2.2.3 Weak authenticators, notifications | Pass | API tokens are 256-bit random (`tokens.py::generate`); sign-ins and lockouts audited (`auth.login`, `auth.lockout`) |
| 2.4.1–2.4.5 Password storage | Pass | argon2id (RFC 9106 profile, `S/security/passwords.py`), constant-cost verify for unknown users (`_DUMMY`) |
| 2.5.1–2.5.6 Recovery | Pass | No self-service reset; administrators set a one-time password that must be changed (`must_change_password`); TOTP recovery codes hashed and keyed (`totp_recovery_kid`) |
| 2.7.x / 2.8.x Out-of-band / TOTP | Pass | RFC 6238, replay protection in Valkey + MongoDB, recovery codes (`S/security/totp.py`, `second_factor.py`) |
| 2.9.x Cryptographic authenticators | Pass | Registry tokens EdDSA JWT 5 min (`S/security/registry_token.py`), refresh tokens re-check the subject; revocation cut-off (`registry_revocation.py`) |
| 2.10.1–2.10.4 Service authentication, no default credentials | Pass | Service accounts use API tokens; bootstrap admin password random or from `_FILE`, one-time (`S/bootstrap.py`); worker → server prefetch uses HMAC system grants, 5 min, one repository (`C/system_grants.py`) |

## V3 Session management

| Req | Status | Evidence |
|---|---|---|
| 3.1.1 No session token in URLs | Pass | Cookie only; download grants in URLs are per-file, 5 min, read-only (adr/0010; `test_security_matrix.py::test_a_download_grant_reads_one_file_of_one_repository`) |
| 3.2.1–3.2.3 New token on sign-in, 128+ bits, stored safely | Pass | `S/security/sessions.py` (256-bit, new on every sign-in, only sha256 stored, HttpOnly cookie) |
| 3.3.1–3.3.4 Logout, idle and absolute timeouts, revoke other sessions | Pass | `session_idle_minutes` / `session_max_hours`; sessions revoked on password change, disable, delete, "sign out everywhere" |
| 3.4.1 `Secure` cookie | Accepted | Set when `WAYSTATION_BASE_URL` is `https://` (`sessions.py`: `secure = base_url.startswith('https://')`); a plain-HTTP deployment is the operator's choice and documented |
| 3.4.2–3.4.5 HttpOnly, SameSite, path | Pass | `ws_session` HttpOnly + `SameSite=Lax`, path `/`; the 2FA challenge cookie `SameSite=Strict` |
| 3.5.1–3.5.3 Token-based sessions | Pass | API tokens revocable within 1 s on every replica (`revocations` pub/sub); registry JWTs signed EdDSA, short-lived, re-checked against the API token (`tid`) |
| 3.7.1 Re-authentication for sensitive changes | Pass | Password change needs the current password; TOTP disable needs a code |

## V4 Access control

| Req | Status | Evidence |
|---|---|---|
| 4.1.1–4.1.5 Enforced server side, least privilege, deny by default, fail securely | Pass | `access.enforce` (no rule = 403 + error log); unsafe methods are `write`/`delete` in every format except reviewed exceptions (`test_security_matrix.py::test_unsafe_methods_are_writes_in_every_format`) |
| 4.2.1 IDOR / hidden objects | Pass | Hidden = 404 (ADR 0005 401 for anonymous on format routes, identical for missing); per-format matrix `test_security_matrix.py::test_publish_matrix_on_every_hosted_format`, `test_read_matrix_on_every_proxy_format`, `ST/test_access.py` (REST routes × principals) |
| 4.2.2 CSRF | Pass | Double-submit token for every unsafe request authenticated by the session (`access._check_csrf`), `SameSite=Lax` |
| 4.3.1 Admin interfaces protected (MFA available) | Pass | Global `admin` actions; an instance policy requires TOTP for local administrators (`require_totp_for_admins`, `S/security/second_factor.py::setup_required`) |
| 4.3.2 No directory browsing / metadata exposure | Pass | nginx `location / { return 404; }`; `/_blobs/` is `internal`; `/_internal/` is not proxied by the web tier and needs a system grant |
| Credentials out of place (not ASVS-numbered; this review) | Pass | Bare-token `Authorization` and `X-NuGet-ApiKey` only on `/repository/`; system grants only on the prefetch route; download grants only for GET/HEAD of one file; `X-Waystation-Connector` only from internal hops (`test_security_matrix.py`) |

## V5 Validation, sanitisation and encoding

| Req | Status | Evidence |
|---|---|---|
| 5.1.1–5.1.5 Input validation, allow lists, structured data | Pass | pydantic models for every API body; `C/paths.normalise`; per-format name rules |
| 5.1.4 / 12.1.x Size limits | Fixed | Bounded readers for every archive and compressed index (`C/archives.py`, per-format limits); see Findings 2–6 |
| 5.2.1–5.2.5 Sanitisation, no `eval`, template injection | Pass | No `eval`/`exec`/`pickle`; Markdown allow-list renderer; Lua scripts in Valkey are constants |
| 5.2.6 SSRF | Fixed | `C/ssrf.py` checks the connected address at connect time (no rebinding window without an outbound proxy); every redirect hop re-checked; IPv6 transition forms fixed (Finding 1); corpus `tests/security/test_ssrf_corpus.py` |
| 5.2.7 SVG / scriptable content | Pass | Artifacts sandboxed by CSP (V1.12.2); the SPA's SVG icons are bundled |
| 5.3.1–5.3.10 Output encoding, injection | Pass | MongoDB queries built from dicts (no string queries); LDAP filters escaped (`S/security/ldap.py`, `escape_filter_chars`); no shell (`subprocess` unused) |
| 5.5.1–5.5.4 Deserialisation | Fixed | JSON bounded and nesting errors refused cleanly (Finding 7); XML via `defusedxml` (entity expansion refused: corpus); YAML `safe_load` with merge keys refused (Finding 5) |
| 12.3.1–12.3.6 Path traversal, file inclusion | Pass | `C/paths.normalise` (100 % branch coverage, `tests/security/test_path_corpus.py` with a fuzzer); blob files are content-addressed; upload ids `^[0-9a-f]{32}$` (`C/storage.py`) |
| 12.4.1 Archive extraction (zip/tar slip, links) | Fixed | Slip names, absolute, backslash and NUL names refused; links leaving the archive refused or dropped; Swift links through links (Finding 9); corpus `tests/security/test_archive_corpora.py` |

## V6 Stored cryptography

| Req | Status | Evidence |
|---|---|---|
| 6.2.1–6.2.5 Approved algorithms, no custom crypto | Pass | AES-256-GCM (`cryptography`), HMAC-SHA256 (tokens, grants), argon2id, EdDSA (registry tokens), OpenPGP on `cryptography` primitives (`C/openpgp.py`, `C/signing.py`; adr/0006, 0007) |
| 6.2.6 Nonces | Pass | 96-bit random nonce per encryption (`C/crypto.py`) |
| 6.3.1–6.3.2 CSPRNG | Pass | `secrets` / `os.urandom` everywhere a secret is made |
| 6.4.1–6.4.2 Key management | Pass | `WAYSTATION_SECRET_KEYS` (first encrypts, all decrypt), domain-separated HKDF labels per use (`system grant v1`, `download url v1`, ...), rotation job |
| Constant-time comparisons | Pass | `hmac.compare_digest` for CSRF, grants, tokens |

## V7 Errors and logging

| Req | Status | Evidence |
|---|---|---|
| 7.1.1–7.1.4 No secrets in logs, needed events logged | Pass | `C/logs.py::redact` (keys such as password, token, authorization, cookie), nginx log omits headers and query strings (`Build/Containers/web/nginx.conf`); audit records sanitised (`C/audit.py`) |
| 7.2.1–7.2.2 Authentication and access decisions logged | Pass | Audit `auth.login`, `auth.lockout`, token use; access log with principal per request |
| 7.3.1–7.3.4 Log injection, protection, time | Pass | JSON lines (escaped), UTC timestamps; logs leave the container (json-file driver) |
| 7.4.1–7.4.3 Generic error messages | Fixed | `S/problems.py` answers unhandled errors `internal error`, validation errors without input echo; archive and deep-JSON failures that used to be 500s are now the format's clean refusal (Findings 6–7). Remaining: deep JSON at a few non-archive sites (Accepted, Finding 11) |

## V8 Data protection

| Req | Status | Evidence |
|---|---|---|
| 8.1.1–8.1.4 Server-side caches, temp files | Pass | Upload temp files under the data dir with 0640, swept; per-request metadata caches keyed by repository |
| 8.2.1 Anti-caching headers for sensitive data | Fixed | `Cache-Control: no-store` on `/api/` and `/auth/` answers that set none (`S/middleware.py`; `test_security_matrix.py::test_api_and_sign_in_answers_are_not_stored`) |
| 8.2.2–8.2.3 Browser storage | Pass | Session in an HttpOnly cookie; the SPA keeps only UI preferences and recently viewed items in `localStorage` (`Web/src/utils/storage.ts`) |
| 8.3.1–8.3.8 Sensitive data in URLs / responses, write-only secrets | Pass | Secrets write-only through the API, encrypted at rest; tokens shown once; support bundle redacts by key (`S/api/support.py`) |

## V9 Communications

| Req | Status | Evidence |
|---|---|---|
| 9.1.1–9.1.3 TLS for clients, modern protocols | Pass | `web` TLS 1.2/1.3 only, HTTP→HTTPS 308, HTTP/2 (`15-waystation.sh`), or a documented reverse proxy (`REVERSE_PROXIES.md`) |
| 9.2.1–9.2.4 Outbound TLS verified, revocation | Pass | Upstream clients verify certificates (system store + per-repository CA, `S/proxy/http.py`); LDAP over TLS with a CA (`ldap.py`); OCSP not checked (Accepted: library default) |

## V10 Malicious code

| Req | Status | Evidence |
|---|---|---|
| 10.3.1–10.3.3 Integrity of updates, no unauthorised calls home | Pass | No auto-update, no telemetry; upstream content verified against digests before commit (ARCHITECTURE §6.4) |

## V11 Business logic

| Req | Status | Evidence |
|---|---|---|
| 11.1.1–11.1.8 Sequential flows, limits, anti-automation | Pass | Write policies (allow redeploy/never), quotas (`C/quota.py`), rate limits; publish handlers validate names and versions per format |

## V12 Files and resources

| Req | Status | Evidence |
|---|---|---|
| 12.1.1–12.1.3 Large files, compressed files, quotas | Fixed | Every reader bounds decompressed size, entry counts and metadata size; tar extension headers bounded (Findings 2–4) |
| 12.2.1 File type checks | Pass | Each format parses what it stores; content types forced for active inline types (raw) |
| 12.5.1–12.5.2 Served file types, `nosniff` | Fixed | `nosniff` on every answer from the web tier (was only on some format answers) |
| 12.6.1 SSRF allow lists for outbound requests | Fixed | See 5.2.6; per-repository `allow_private_upstream`, `WAYSTATION_SSRF_ALLOW_CIDRS`, Hugging Face `cdn_hosts` redirect allow list |

## V13 API and web services

| Req | Status | Evidence |
|---|---|---|
| 13.1.1–13.1.5 Same encoding, admin URLs, no keys in URLs, authorisation at the resource | Pass | REST under `/api/v1/` only; API tokens in headers (download grants excepted, V3.1.1) |
| 13.2.1–13.2.6 Methods, schema validation, CSRF, content types | Pass | Explicit methods per route, pydantic schemas, CSRF for session writes, JSON only on the REST API |
| 13.4.x GraphQL | N/A | No GraphQL |

## V14 Configuration

| Req | Status | Evidence |
|---|---|---|
| 14.1.1–14.1.5 Build and deploy | Pass | Reproducible locks (`uv.lock`, `package-lock.json`), CI on every change (`make check`) |
| 14.2.1–14.2.3 Dependencies up to date, no vulnerable components | Fixed | `make security-scan` (pip-audit, npm audit, Trivy); fixable HIGH image findings removed (Finding 10); remaining OS findings have no fix and are unreachable (`Documents/Security/DEPENDENCIES.md`) |
| 14.2.4–14.2.6 Third-party components, attack surface | Fixed | pip removed from the runtime images; `server_tokens off` |
| 14.3.1–14.3.3 No debug output, no version disclosure | Pass | No debug mode in production images; nginx `server_tokens off`; errors generic |
| 14.4.1 `Content-Type` with charset | Pass | Starlette/FastAPI responses set it; formats set their native types |
| 14.4.2 `Content-Disposition` on downloads | Pass | Raw sets `attachment` or `inline` per repository option with a safe file name |
| 14.4.3 CSP | Fixed | UI: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; ...; frame-ancestors 'none'` (inline style attributes are Quasar's); everything else `sandbox` (verified with the minimal-permission smoke and an administrator walk of every page under the CSP: no violations) |
| 14.4.4 `X-Content-Type-Options: nosniff` | Fixed | Every answer from the web tier |
| 14.4.5 HSTS | Fixed | `Strict-Transport-Security: max-age=31536000` when `web` terminates TLS; behind a reverse proxy that terminates TLS, the proxy's own HSTS setting applies |
| 14.4.6 `Referrer-Policy` | Fixed | `same-origin` |
| 14.4.7 Framing | Fixed | `X-Frame-Options: DENY` + `frame-ancestors 'none'` |
| 14.5.1–14.5.3 HTTP methods, Origin, CORS | Pass | No CORS headers anywhere (same-origin SPA); unknown methods 405 |
| 14.5.4 Trusted proxy headers | Pass | `X-Real-IP`, `X-Forwarded-*` and `X-Waystation-Connector` honoured only from internal hops (`S/clientip.py`, `S/routing.py::connector_port`) |

## Findings

| # | Severity | Finding | Status | Fix / regression test |
|---|---|---|---|---|
| 1 | High | SSRF guard let IPv6 forms of private IPv4 addresses through: NAT64 `64:ff9b::/96` (e.g. the cloud metadata address via a NAT64 gateway), IPv4-compatible addresses (`::/96`), deprecated site-local addresses (RFC 3879) (Python calls them global) | Fixed | `C/ssrf.py::embedded_ipv4`; `tests/security/test_ssrf_corpus.py` |
| 2 | High | tar extension headers (pax, GNU long name) read whole into memory: a 500 KB `.tgz` used 1 GB; chained headers hit `RecursionError` (500); sparse maps unbounded. Every tar reader (13 call sites) | Fixed | `C/archives.py::BoundedTarInfo`; `test_tar_extension_header_bomb_is_refused_in_bounded_memory`, `test_tar_chained_extension_headers_are_refused`, `test_tar_sparse_map_bomb_is_refused` |
| 3 | High | `.conda` `metadata.json` read with `ZipFile.read` regardless of its declared size (lying-header zip bomb, ~1000× amplification) | Fixed | bounded read in `S/formats/conda/package.py`; `test_zip_bomb_stops_at_the_limit[lying-conda-v2-zip]` |
| 4 | High | RPM header file list built as `dirs[i] + base` for every name: a small header expands to gigabytes | Fixed | `PATHS_MAX` in `S/formats/yum/rpm.py`; `test_rpm_file_list_expansion_is_refused` |
| 5 | Medium | YAML merge-key (`<<`) CPU bomb in gem `metadata.gz`, `Chart.yaml` and proxied Helm `index.yaml` (2^n work in < 1 KB) | Fixed | merge keys refused (`rubygems/package.py`, `helm/index.py`); `test_yaml_merge_bombs_are_refused` |
| 6 | Medium | Compressed stream-mode tars (`r|gz`, `r|*`) inflate a whole buffer per read (Helm, Terraform modules, Swift); zipfile/zlib/zstd exceptions became 500s in nine readers and the apt index decoder | Fixed | file modes; `archives.READ_ERRORS`; `test_zip_damaged_copies_are_refused_cleanly`, `test_tar_damaged_copies_are_refused_cleanly`, `test_damaged_compressed_indexes_are_refused_cleanly` |
| 7 | Low | Deeply nested JSON in package metadata (composer, npm, conda, cargo, galaxy) raised `RecursionError` (500) | Fixed | `test_deeply_nested_json_is_refused_cleanly` |
| 8 | Low | PyPI tar sdist had no total unpacked-size bound; duplicate zip entries in Composer/PyPI let the checked copy differ from the one a client extracts | Fixed | `MAX_UNPACKED` in `pypi/distmeta.py`; duplicates refused; `test_zip_duplicate_metadata_is_refused` |
| 9 | Low | Swift: a symlink whose target walks through another symlink (`d -> .`, `l -> d/../x`) passed the lexical inside check | Fixed | `swift/archive.py::_through_link`; `test_swift_links_through_other_links_are_refused` |
| 10 | Medium | No CSP, framing protection, `nosniff` (UI, API), `Referrer-Policy` or HSTS from the web tier: proxied HTML (for example a Hugging Face repository file served `text/html`) could run script on the UI's origin; the UI could be framed; API answers carrying tokens were cacheable | Fixed | `15-waystation.sh` header includes, `S/middleware.py` no-store; `tests/web/run.sh` (live header checks), `test_api_and_sign_in_answers_are_not_stored` |
| 11 | Medium | Images: fixable HIGH findings (Debian `zlib1g`; Alpine `libxml2`; `urllib3`, `msgpack`, `setuptools` vendored in the base image's unused pip) | Fixed | `apt-get upgrade` / `apk upgrade` at build, pip removed (`Build/Containers/*/Dockerfile`); `make security-scan` |
| 12 | Low | `RecursionError` from deeply nested JSON bodies at non-archive sites (docker manifests, Hugging Face, Go, Terraform registry documents) still answers 500 | Accepted | Bounded bodies, one request each, logged; no amplification |
| 13 | Low | npm (`package.json`) and conda `.tar.bz2` (`index.json`): the first of duplicate members wins while an extracting client keeps the last | Accepted | Metadata only (name/version are re-checked against the publish request); no file is extracted by Waystation |
| 14 | Low | RubyGems `check_specs` inflates a proxied gzip index without a CPU bound (memory bounded) | Accepted | Upstream-administered content, size-bounded download |
| 15 | Info | Docker upstream token negotiation sends the repository's credentials to the realm the upstream's challenge names (as the docker CLI does) | Accepted | Realm fetched through the SSRF guard; credentials are the repository's own upstream credentials |
| 16 | Info | MongoDB and Valkey without authentication/TLS on the internal network in the reference compose | Accepted | `backend` network is `internal: true`, only server/worker/init join it; split hosts configure auth/TLS in the URIs |
| 17 | Info | Rate limits fail open when Valkey is down | Accepted | Availability over throttling; the per-account lockout in MongoDB still applies |
