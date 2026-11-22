# Waystation REST API

Waystation's web UI is built entirely on a public REST API under `/api/v1/`, so everything the UI does (repositories,
users, roles, tokens, cleanup, tasks, statistics, audit, settings) can be scripted.

- **Reference**: [REFERENCE.md](REFERENCE.md) lists every operation (parameters, request and response bodies) and
  every schema. It is generated from the server's own OpenAPI 3.1 document by `make api-docs`
  (`Build/ci/api_docs.py`); a unit test fails when it no longer matches the code, so the reference in a release
  tag describes exactly that release.
- **Machine-readable**: [openapi.json](openapi.json) is the same document, for client generators and API tools. A
  running server also serves it at `GET /api/v1/openapi.json` (no interactive documentation UI is served).
- **Format protocols** (`/repository/<repo>/...`, `/v2/`, `/auth/token`, `/.well-known/terraform.json`) are not
  part of the REST API: they follow each ecosystem's own specification. See `../Architecture/FORMATS.md` and the
  client guides in `../Operations/clients/`.

The examples use `https://waystation.example.com` as the base URL (`WAYSTATION_BASE_URL`).

## Versioning

The API version is the path prefix: `/api/v1/`. Within v1, changes are additive (new operations, new optional
parameters, new response fields); clients must ignore fields they do not know. A breaking change would only ship
under a new prefix (`/api/v2/`).

## Authentication

| Method | Used by | How |
|---|---|---|
| API token | scripts, CI, integrations | `Authorization: Bearer wst_...` (also `Authorization: Token wst_...`, or HTTP basic with the token as the password and any username) |
| Session cookie | the web UI | `POST /api/v1/auth/login` sets the `ws_session` cookie (HttpOnly); unsafe methods must echo the CSRF token (below) |

- **API tokens** start with `wst_`, are shown once when created and are stored only as a keyed hash. Create them on
  the profile page (or `POST /api/v1/auth/me/tokens`, which needs an interactive session: a token cannot create
  tokens), or for a service account under Administration → API tokens. A token carries scopes (`read`, `write`,
  `delete`, `manage`, `stats-read`, `metrics`, `admin`), optional repository patterns and an optional expiry; it can
  only **narrow** what its owner may do, never extend it.
- **CSRF** (cookie sessions only): `POST /api/v1/auth/login` and `GET /api/v1/auth/me` return `csrf_token`, also
  set in the readable cookie `ws_csrf`. Every `POST`, `PUT`, `PATCH` and `DELETE` with a session cookie must send
  it back in `X-CSRF-Token`. Requests authenticated with a token are exempt.
- **Two-factor sign-in**: for an account with TOTP, `POST /api/v1/auth/login` answers `202` with
  `second_factor: "totp"`; finish with `POST /api/v1/auth/login/totp`.
- **Anonymous** requests act as the `anonymous` principal (Administration → Roles → Anonymous access). An
  `Authorization` header that does not resolve to a principal is refused with `401`; it is never treated as
  anonymous.

```bash
export WS=https://waystation.example.com
export WS_TOKEN=wst_...                       # from the profile page

curl -fsS -H "Authorization: Bearer $WS_TOKEN" "$WS/api/v1/repositories?format=docker&limit=20"
```

## Authorization

Every request is checked by the RBAC engine: permissions are `(action, scope)` pairs granted through roles
(SPECIFICATION §4.10). The reference does not repeat the permission each operation needs; the rules are:

- `401` when the operation needs a signed-in principal and none was presented (or the credentials are invalid).
- `403` with problem code `permission_required` when the principal is known but lacks the permission.
- `404` for a repository (or other object) the principal may not see: hidden and missing objects look the same.
- Lists and search results are filtered to what the principal may see; totals and facet counts are too.
- `GET /api/v1/auth/me` returns the caller's coarse capabilities and `GET /api/v1/auth/me/permissions` the
  effective permissions (narrowed by the token in use).

## Errors

Every API error is an RFC 9457 problem document with media type `application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request has invalid fields.",
  "code": "invalid",
  "errors": [
    {"field": "upstream.url", "message": "must be an http(s) URL", "code": "value_error"}
  ]
}
```

- `status`, `title` and `detail` are always present. `detail` is meant for people; do not parse it.
- `code`, when present, is a stable machine-readable reason (for example `not_found`, `permission_required`,
  `password_change_required`, `totp_setup_required`, `second_factor_expired`).
- `errors[]` appears on `422` (and on other refusals tied to one field): `field` is a dotted path into the request
  (`name`, `upstream.url`, `group.members.0`), `message` explains, `code` classifies.
- The generated reference names FastAPI's `HTTPValidationError` schema for `422` responses; the body actually sent
  is the `Problem` document above.
- `429` (sign-in and token-failure rate limits) carries `Retry-After`.
- Every response carries `X-Request-Id`; quote it when reporting a problem (it is in the server logs and the audit
  log).

## Conventions

- **JSON** request and response bodies; times are RFC 3339 UTC strings; sizes are bytes.
- **Paging**: list operations take `limit` (default and maximum per operation) and an opaque `cursor`, and return
  `{"items": [...], "next_cursor": "...", ...}`. Pass `next_cursor` back as `cursor` until it is `null`. A filtered
  page may hold fewer than `limit` items while `next_cursor` is still set: keep paging.
- **HEAD** is answered for every `GET`.
- **Secrets are write-only**: upstream passwords, bind passwords, client secrets, webhook secrets and custom header
  values are accepted on create and update but never returned; reads show `*_set: true`. Omitting a secret on update
  keeps the stored value.
- **Caching**: API answers carry `Cache-Control: no-store` unless an operation sets its own caching.
- **Exports** (`/api/v1/audit/export`, `/api/v1/activity/export`) stream CSV or NDJSON.

## Live events

`GET /api/v1/events/stream` is a Server-Sent Events stream (`text/event-stream`) of `LiveEvent` documents
(see `LiveEvent` in the reference):

- `topics` (comma-separated): `transfers`, `stats`, `activity`, `tasks` (jobs and workers; needs `admin`),
  `repo:<name>`. The stream needs `stats-read` and only carries events of repositories the caller may see.
- On connect the server sends `retry: 3000` and a `transfers.snapshot` of the transfers in flight; `stats.tick`
  follows once a second; a heartbeat comment every 15 s keeps proxies from closing the connection.
- `activity` events carry an id; reconnect with `Last-Event-ID` (or the `last_event_id` query parameter, as
  `EventSource` cannot set headers) to replay what was missed (up to 1000 events).

```bash
curl -N -H "Authorization: Bearer $WS_TOKEN" "$WS/api/v1/events/stream?topics=transfers,activity"
```

## Common tasks

| Task | Operation |
|---|---|
| List, create, update, delete repositories | `GET/POST /api/v1/repositories`, `GET/PUT/DELETE /api/v1/repositories/{name}` |
| Client configuration snippets ("Set me up") | `GET /api/v1/repositories/{name}/setup` |
| Search packages, images and files; where a digest is used | `GET /api/v1/search`, `GET /api/v1/digests/{digest}/usage` |
| Run a task now, list job runs | `POST /api/v1/tasks/scheduled/{task_id}/run`, `GET /api/v1/tasks/runs` |
| Cleanup policy dry run, run now | `POST /api/v1/cleanup-policies/preview`, `POST /api/v1/cleanup-policies/{policy_id}/run` |
| Statistics | `GET /api/v1/stats/summary`, `/stats/traffic`, `/stats/top/artifacts` |
| Audit log export | `GET /api/v1/audit/export?format=ndjson` |
| Provisioning plan and apply | `POST /api/v1/provisioning/plan`, `POST /api/v1/provisioning/apply` |

Exact paths and parameters are in [REFERENCE.md](REFERENCE.md).
