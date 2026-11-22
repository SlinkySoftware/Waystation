# npm client guide

How to point npm, pnpm, Yarn and Bun at Waystation npm repositories, and how to publish with npm. The repository's
**Set me up** tab shows the same snippets with your real URL filled in. Examples below use
`https://waystation.example.com` and repositories called `npm` (proxy of registry.npmjs.org), `npm-hosted` (hosted)
and `npm-all` (a group of both: hosted first, the proxy behind it).

## Overview

Waystation serves the npm registry API: packuments (full and abbreviated), tarballs, scoped packages, search, audit,
ping, publish, dist-tags, deprecate, unpublish, and `npm login --auth-type=legacy` / `npm logout`. Protocol notes:
[FORMATS.md](../../Architecture/FORMATS.md), section "npm".

Every repository answers at `https://waystation.example.com/repository/<name>/`. The trailing slash matters: npm
resolves package paths relative to the registry URL.

Tested with npm 11 (installs, publish, login) and, against a proxy, pnpm 12, Yarn 1.22, Yarn 4 and Bun 1.4: frozen
installs from each client's own committed lockfile (left unchanged) and each client's audit command (`pnpm audit`,
`yarn audit`, `yarn npm audit`, `bun audit`). Credentials (npm login, `_authToken` from an LDAP or OIDC user) are
covered by the client authentication tests.

## Repository types

### Proxy

A cache of registry.npmjs.org or another npm registry.

- **Packuments** (`GET /<pkg>`, `/@scope%2f<pkg>`, `/@scope/<pkg>`): the full document and the abbreviated one
  (`Accept: application/vnd.npm.install-v1+json`) are cached separately and revalidated with the upstream after the
  repository's metadata max age. Every `dist.tarball` URL is rewritten to this repository.
- **Tarballs** are cached as immutable content and verified while they download against the packument's
  `dist.integrity` (sha512) or `dist.shasum` (sha1). A tarball that fails verification is not cached and the
  client gets an error; a tarball the packument does not list is 404.
- **Search** (`npm search`) is proxied and cached for 5 minutes per query.
- **Audit** (`npm audit`) is relayed to the upstream without caching. Turn it off per repository with the
  `audit_passthrough` option (the endpoint then answers 404 and `npm audit` reports that the registry does not
  support audits).
- `npm ping` is answered by Waystation.

### Hosted

Your own packages: `npm publish`, dist-tags, deprecate, unpublish and search. See "Publishing".

### Group

A group answers installs with the members' packuments merged: every version any member has, the **first member that
has a version wins it** (its metadata and its tarball), and dist-tags come from the first member that has the tag.
Put the hosted repository first so your packages win over a public package of the same name and version, and use a
scope you own (`@your-org/...`) for private packages to avoid dependency confusion: a name that exists only in the
proxy member is still installed from the public registry. Publishing to a group lands in its writable member.

## Client setup

### npm

Per user:

```sh
npm config set registry https://waystation.example.com/repository/npm/
```

Per project, `.npmrc` next to `package.json`:

```ini
registry=https://waystation.example.com/repository/npm/
```

#### Keep internal host names out of lockfiles

npm writes each package's full tarball URL into `package-lock.json` (`resolved`). Installed through Waystation that
URL is `https://waystation.example.com/repository/npm/...`, so a committed lockfile would carry your internal host
name, and anyone without access to it could not install from the lockfile. To avoid that, add to `.npmrc`:

```ini
omit-lockfile-registry-resolved=true
```

npm then leaves `resolved` out of the lockfile (the `integrity` hashes stay) and fetches tarballs from whatever
registry is configured at install time. Lockfiles already pinned to `https://registry.npmjs.org/...` install fine
through Waystation too: npm swaps the registry part of `resolved` URLs for the configured registry.

### pnpm

```sh
pnpm config set registry https://waystation.example.com/repository/npm/
```

or `registry=...` in the project `.npmrc` (pnpm reads it). pnpm's `pnpm-lock.yaml` stores integrity hashes, not
tarball URLs, for packages from the configured registry, so `pnpm install --frozen-lockfile` works unchanged.

### Yarn

Yarn 1:

```sh
yarn config set registry https://waystation.example.com/repository/npm/
```

Yarn 2+ (`.yarnrc.yml`):

```yaml
npmRegistryServer: "https://waystation.example.com/repository/npm/"
```

Yarn 2+ records only the package reference and checksum in `yarn.lock`, so an existing lockfile installs through
Waystation unchanged (`yarn install --immutable`). Over plain `http://` (a test instance) Yarn 2+ also needs
`unsafeHttpWhitelist: ["<host>"]`.

Yarn 1 records `resolved` tarball URLs in `yarn.lock` and fetches **those URLs verbatim**, whatever registry is
configured: a lockfile that names `https://registry.npmjs.org/...` bypasses Waystation (and fails where the public
registry is unreachable). Generate the lockfile through Waystation, or rewrite the registry part of the URLs once,
for example `sed -i 's#https://registry.npmjs.org/#https://waystation.example.com/repository/npm/#' yarn.lock`.
The `integrity` lines stay valid.

### Bun

`bunfig.toml`:

```toml
[install]
registry = "https://waystation.example.com/repository/npm/"
```

## Authentication

Proxy repositories are read anonymously when the deployment allows anonymous reads (the default for proxies).
Hosted and group repositories, and proxies without anonymous reads, need credentials: an API token as `_authToken`
for the registry URL (npm sends it as `Authorization: Bearer`).

### An API token in .npmrc

Create an API token on your profile page (avatar menu → Profile → API tokens; shown once, copy it then) or, for CI,
on a service account. Give it only the scopes it needs (`read` to install, `write` to publish, `delete` to
unpublish) and, ideally, restrict it to the repositories the client needs: a token only ever narrows what its owner
may do. A revoked or expired token is refused at once (401); it never falls back to anonymous access.

```ini
registry=https://waystation.example.com/repository/npm-all/
//waystation.example.com/repository/npm-all/:_authToken=${WAYSTATION_TOKEN}
```

Keep the token in an environment variable (as above) or in the user-level `~/.npmrc`, never in a project `.npmrc`
that is committed. A revoked token makes npm fail with `E401`; a token restricted to other repositories gets `E404`.
A token restricted to a group by name cannot publish through it: publishing writes to the group's hosted member,
which the token must also cover (or use no repository restriction).

### npm login

```sh
npm login --auth-type=legacy --registry=https://waystation.example.com/repository/npm-all/
```

npm asks for your Waystation username and password (local or LDAP accounts) and stores a new API token in
`~/.npmrc`. The token carries the repository scopes you hold (`read`, `write`, `delete`), no repository restriction
and no expiry; it is listed as `npm login (<repository>) <time>` on your profile, where you can revoke it.
`npm logout --registry=...` revokes it and removes it from `.npmrc`. Notes:

- `--auth-type=legacy` is required: npm's default web login is not offered. `npm adduser` cannot create accounts.
- Accounts with two-factor sign-in, accounts that must change a one-time password and OIDC accounts cannot use
  `npm login`; create a token in the UI instead.
- You need read access to the repository you log in to. Wrong credentials answer `E401`. On a repository that
  anonymous users cannot see, every failure (wrong credentials, no access, a malformed request) answers exactly like a
  repository that does not exist (`E401` with the Basic challenge,
  [ADR 0005](../../Architecture/adr/0005-anonymous-challenge-on-format-routes.md)), so a private repository never
  reveals that it exists.
- Failed attempts count towards the account lockout and the sign-in rate limit like any other sign-in.

## Publishing

Publish to a hosted repository, or to a group with a writable member (the package lands in that hosted member).
Pin the target in `package.json` so a stray `npm publish` never goes to the public registry:

```json
"publishConfig": {
  "registry": "https://waystation.example.com/repository/npm-hosted/"
}
```

```sh
npm publish                                     # needs write permission
npm dist-tag add my-package@1.2.0 beta          # write
npm dist-tag rm my-package beta                 # write (latest cannot be removed)
npm deprecate my-package@1.0.0 "use 1.2.0"      # write; an empty message un-deprecates
npm unpublish my-package@1.0.0                  # delete permission
npm unpublish my-package --force                # every version; delete permission
```

- Each publish is checked before it is stored: the tarball must match the `integrity`/`shasum` npm computed, be a
  valid gzip tar archive (bounded size and entry count, safe entry names) and contain a `package.json` with the same
  name and version. The registry computes `dist.integrity` (sha512), `shasum`, `fileCount` and `unpackedSize` itself.
- Publish bodies are limited to 100 MiB (about 75 MiB of tarball).
- **Write policy** (repository settings): `disable_redeploy` refuses a version that exists ("You cannot publish over
  the previously published versions", as npmjs.org); `allow_redeploy` replaces it; `read_only` refuses publishes.
- The first published version becomes `latest`; `npm publish --tag beta` sets only `beta` (`latest` stays, or is the
  newest release when nothing else names one). Removing the version a tag points at removes the tag.
- `npm deprecate` and `npm unpublish` through a group act on the group's writable member only.
- Each publish, tag change, deprecation and unpublish is audited (and can trigger webhooks).

## Limitations

- Only `npm login --auth-type=legacy` is supported; npm's web login is not offered and `npm adduser` cannot create
  accounts. Two-factor, must-change-password and OIDC accounts use an API token instead.
- Publish bodies are limited to 100 MiB.
- Proxy repositories are read-only; a group without a writable member refuses publishes.
- Yarn 1 fetches the tarball URLs recorded in `yarn.lock` verbatim; lockfiles pointing at another registry bypass
  Waystation until rewritten.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `E404` for a package that exists upstream | Typo in the registry URL (missing `/repository/<name>/` or trailing slash), the repository is offline, or you cannot read it (hidden repositories answer 404). |
| `EINTEGRITY` | The lockfile's integrity does not match the tarball. Waystation verifies tarballs against the upstream packument, so this usually means a lockfile written against a different registry with different content; regenerate the lockfile entry. |
| `E502` / `ETIMEDOUT` on a fresh package | The upstream is unreachable and nothing is cached yet. Check the repository's upstream health on the dashboard. Cached packages keep working when stale-on-error is on. |
| A newly published version is not found | The packument is cached until its metadata max age passes. Requesting a tarball the cached packument does not list triggers one refresh automatically; `npm view <pkg>` after the max age shows new versions. |
| `npm audit` says the endpoint is not supported | `audit_passthrough` is off for this repository, or the upstream does not offer audits. |
| Lockfile contains the Waystation host name | Add `omit-lockfile-registry-resolved=true` to `.npmrc` (see "Client setup"). |
| `E403 You cannot publish over the previously published versions` | The hosted repository's write policy is `disable_redeploy`: bump the version. |
| `E400` on `npm publish` | The tarball and its metadata disagree (name, version, integrity) or the archive is invalid; the message says which. |
| `E405` on `npm publish` | The registry is a proxy, or a group without a writable member. |
| `E403` on `npm unpublish` | Unpublishing needs the `delete` permission (and a token with the `delete` scope). |
| `npm login` answers `E401` | Wrong credentials, or on a repository anonymous users cannot see (or that does not exist): any failure, including an account that cannot read it. |
| `npm login` answers `E404` | The repository is readable anonymously but your account cannot read it. |
| `npm login` hangs or opens a browser | Add `--auth-type=legacy`. |
