# Declarative provisioning

Waystation can take its configuration from a YAML file kept in version control (GitOps): repositories, cleanup
policies, custom roles, local groups, service accounts, sign-in providers (with their group → role mapping),
webhooks and instance settings. The file is applied **idempotently**: applying it twice changes nothing, and only
what the file declares is touched.

- Point `WAYSTATION_PROVISION_FILE` at the file (mount it read-only into the `init` and `server` containers at the
  same path). `init` applies it at every start; an invalid file or a failed change fails `init`, so the server does
  not start on a configuration other than the declared one.
- **Administration → Provisioning** (`admin`) shows the file, how its last apply went and the plan (what applying
  it now would change, field by field), and applies it on demand. The page and the API (`GET /api/v1/provisioning`,
  `POST /api/v1/provisioning/plan|apply`) only ever read the configured file: nothing is uploaded.
- Objects the file manages are labelled **Managed** in the UI and open read-only, with a hint to change the file
  instead. The API still accepts edits (an emergency fix is possible), but the next apply puts the declared values
  back.

A complete example, with `example.com` names: [`provisioning.example.yaml`](provisioning.example.yaml). The JSON
Schema for editors (VS Code's YAML extension: `# yaml-language-server: $schema=...`) is
[`provisioning.schema.json`](provisioning.schema.json), generated from the server's models by
`make provision-schema` (a unit test fails when it is stale).

## Commands

| Command | What it does |
|---|---|
| `make provision-check FILE=<path>` | Syntax and schema check, offline (no database; secret references are not read) |
| `make provision-plan [PREFIX=waystation] [JSON=1]` | The plan of the configured file in a running stack (`docker exec <prefix>-server ...`) |
| `make provision-schema` | Regenerate `Documents/Operations/provisioning.schema.json` |
| `python -m waystation_server.provisioning plan\|apply [FILE] [--json]` | Inside a server container: plan or apply (exit 1 when invalid or a change failed) |

## The file

```yaml
version: 1                 # required
prune: {repositories: false, roles: false, ...}   # per section, default false (see "Ownership and prune")
roles: [...]
cleanup_policies: [...]
repositories: [...]
groups: [...]
service_accounts: [...]
auth_providers: [...]
webhooks: [...]
settings: {...}
```

Every section is optional. **A section that is absent is not managed at all**: its objects are never created,
changed, released or deleted. A section that is present (even as `[]`) manages the objects it lists.

The entries use the same field names and limits as the REST API's write bodies (OpenAPI `RepositoryWrite`,
`RoleCreate`, `CleanupPolicyWrite`, `AuthProviderWrite`, `WebhookWrite`, ...), and are validated by the same rules
(names, per-format options, group members, upstream URLs and the SSRF rule, LDAP filters, OIDC issuers, ...):

| Section | Identified by | Notes |
|---|---|---|
| `roles` | `id` | Custom roles: `id`, `name`, `description`, `permissions[{action, scope}]`. Built-in role ids are refused |
| `cleanup_policies` | `name` | `description`, `format` (or `*`), `criteria`, `schedule`, `enabled` |
| `repositories` | `name` (case-insensitive) | Every format, type and option of the API; `online` defaults to `true`; `cleanup_policies: [names]` attaches policies in run order (an omitted list detaches them); group members may be repositories the same file creates |
| `groups` | `name` | Local groups: `description`, `roles` (members are managed in the UI, not here). Groups mapped from a provider are declared in that provider's `group_mapping` |
| `service_accounts` | `name` | `description`, `roles`, `disabled`. Tokens are not provisioned (they are shown once): create them on the Service accounts page |
| `auth_providers` | `name` (case-insensitive) | `kind` (`ldap`/`oidc`, cannot change), `enabled`, `order`, `ldap` or `oidc` as in the API, `group_mapping[{external_group, roles}]` |
| `webhooks` | `name` | `url`, `events`, `repositories`, `enabled`, `secret` (required), `headers`, `allow_private`, `timeout_s`, `max_attempts` |
| `settings` | (each field) | `instance_name`, `require_totp_for_admins`, `anonymous_access` (roles; `[]` = none), `default_docker_repository`, `storage_guard{min_free_bytes, min_free_percent}`; a field left out is not managed |

Role references (groups, service accounts, provider mappings, anonymous access) may name built-in roles, existing
custom roles and roles the same file declares. `default_docker_repository` names a docker repository; when the file
declares it, its `format_options.default_repo` is set (another declared repository must not set it too).

## Secrets

Secrets are **never written in the file**. Where a value is a password, token or key, the file holds a reference:

```yaml
secret: {file: /run/secrets/docker_hub_token}    # the file's content; a trailing newline is removed
secret: {env: DOCKER_HUB_TOKEN}                  # an environment variable of the applying process
```

| Where | Field |
|---|---|
| Proxy repositories | `proxy.auth.secret` (`basic` and `bearer`) |
| LDAP providers | `ldap.connection.bind_password` (with a `bind_dn`) |
| OIDC providers | `oidc.client_secret` (unless `client_auth: none`) |
| Webhooks | `secret` (HMAC signing key) and every `headers` value |

- A plain string there is refused (`inline secrets are not allowed`); the error never repeats the value.
- References are read when the file is planned or applied (files: absolute paths, at most 64 KiB, UTF-8, not empty).
  `init` and the server must both see them: Docker/compose secrets mounted at the same path, or variables set on
  both services.
- Waystation's own settings variables (`WAYSTATION_<SETTING>` and their `_FILE` forms) cannot be referenced, so a
  file cannot copy the encryption keys or database credentials into a webhook header.
- Stored secrets are encrypted with `WAYSTATION_SECRET_KEYS` like secrets entered in the UI. To compare, a plan
  decrypts the stored value in memory; a changed secret appears in plans and audit records only as
  `redacted` (`***`), never as a value. A rotated secret file is picked up by the next apply.

## Plan and apply

A plan lists every object of the managed sections:

- `create`, `update` (with field changes: `field`, `before`, `after`; secrets only `redacted`), `unchanged`,
  `prune` (deleted because `prune` is on and the file no longer declares it);
- `errors` when the file is invalid: YAML or schema problems (line and column; the text around them is never
  quoted), unresolvable secret references and validation errors, each with a dotted path into the file
  (`repositories.2.proxy.upstream_url`).

**An invalid file applies nothing.** A valid file is applied in dependency order: roles, cleanup policies,
repositories (proxy and hosted first, then groups; then cleanup attachments), local groups, service accounts,
sign-in providers, webhooks, settings, then prunes in reverse order (groups before their members). A change that
fails at that point (a repository renamed by someone else meanwhile, a role still held by a user) is reported on its
item (`outcome: failed`, `error`) and the rest is applied; the apply then counts as failed (`init` exits 1).

Every change is audited with its usual action (`repository.create`, `role.update`, `cleanup_policy.delete`,
`settings.update`, anonymous access as `role.update` on `anonymous_access`, ...) and `details.source =
provisioning` plus `details.file_sha256`. The actor is the administrator who applied it, or `system` for `init` and
the command line. Repository changes reach webhooks like any other. The last apply (when, by whom, the file's
sha256, the outcome and the counts) is shown on the Provisioning page.

## Ownership and prune

Objects the file creates or updates carry `managed_by: provisioning`. An existing object with a declared name that
the file did not create (made in the UI, imported from Nexus) is **adopted** by the first apply: the plan shows it as
an `update` of `managed_by`.

When the file stops declaring a managed object:

- without `prune` for its section, the object stays and is **released** (the tag is removed; it becomes an ordinary,
  editable object);
- with `prune: {<section>: true}` it is **deleted**. Only objects the file manages are ever deleted, never ones
  made in the UI. The API's guards still apply: a role still held by a user, group or service account, a repository
  still a member of a group, or the last administrator's group are refused (the item fails).

Deleting a repository removes its content records (blobs are reclaimed by garbage collection later), so turn
`prune.repositories` on deliberately.

## Concurrency and multiple replicas

Apply from one place at a time. `init` runs before the servers start; an apply from the Provisioning page runs on
the replica that answers it (one apply at a time per process) and the other replicas pick the changes up within
seconds, as with changes made in the UI (repository catalog change stream, `grant_version`, provider and webhook
caches).
