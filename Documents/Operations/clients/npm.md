# npm clients

How to point npm, pnpm, Yarn and Bun at Waystation npm repositories: **proxy** (a cache of registry.npmjs.org or
another registry), **hosted** (your own packages: `npm publish`) and **group** (one URL for both: hosted first, the
proxy behind it). The repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples
below use `https://waystation.example.com` and repositories called `npm` (proxy), `npm-hosted` and `npm-all` (group).

Status: proxy (packuments full and abbreviated, tarballs, scoped packages, search, audit, ping), hosted (publish,
dist-tags, deprecate, unpublish, search) and group repositories (FORMATS.md "npm"), `npm login --auth-type=legacy` and
`npm logout`. Verified with npm 11 by the real-client harness (`tests/clients/npm/`); pnpm, Yarn and Bun use the same
registry protocol and are expected to work but are not yet in the harness.

## npm

Per user:

```sh
npm config set registry https://waystation.example.com/repository/npm/
```

Per project, `.npmrc` next to `package.json`:

```ini
registry=https://waystation.example.com/repository/npm/
```

The trailing slash matters: npm resolves package paths relative to the registry URL.

### Keep internal host names out of lockfiles

npm writes each package's full tarball URL into `package-lock.json` (`resolved`). Installed through Waystation that
URL is `https://waystation.example.com/repository/npm/...`, so a committed lockfile would carry your internal host
name, and anyone without access to it could not install from the lockfile. To avoid that, add to `.npmrc`:

```ini
omit-lockfile-registry-resolved=true
```

npm then leaves `resolved` out of the lockfile (the `integrity` hashes stay) and fetches tarballs from whatever
registry is configured at install time. Lockfiles already pinned to `https://registry.npmjs.org/...` install fine
through Waystation too: npm swaps the registry part of `resolved` URLs for the configured registry.

## pnpm

```sh
pnpm config set registry https://waystation.example.com/repository/npm/
```

or `registry=...` in the project `.npmrc` (pnpm reads it). pnpm's `pnpm-lock.yaml` stores integrity hashes, not
tarball URLs, for packages from the configured registry.

## Yarn

Yarn 1:

```sh
yarn config set registry https://waystation.example.com/repository/npm/
```

Yarn 2+ (`.yarnrc.yml`):

```yaml
npmRegistryServer: "https://waystation.example.com/repository/npm/"
```

Yarn 1 records `resolved` URLs in `yarn.lock`; Yarn 2+ records only the package reference and checksum.

## Bun

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
registry=https://registry.example.com/repository/npm-all/
//registry.example.com/repository/npm-all/:_authToken=${WAYSTATION_TOKEN}
```

Keep the token in an environment variable (as above) or in the user-level `~/.npmrc`, never in a project `.npmrc`
that is committed. A revoked token makes npm fail with `E401`; a token restricted to other repositories gets `E404`.
A token restricted to a group by name cannot publish through it: publishing writes to the group's hosted member,
which the token must also cover (or use no repository restriction).

### npm login

```sh
npm login --auth-type=legacy --registry=https://registry.example.com/repository/npm-all/
```

npm asks for your Waystation username and password (local or LDAP accounts) and stores a new API token in
`~/.npmrc`. The token carries the repository scopes you hold (`read`, `write`, `delete`), no repository restriction
and no expiry; it is listed as `npm login (<repository>) <time>` on your profile, where you can revoke it.
`npm logout --registry=...` revokes it and removes it from `.npmrc`. Notes:

- `--auth-type=legacy` is required: npm's default web login is not offered. `npm adduser` cannot create accounts.
- Accounts with two-factor sign-in, accounts that must change a one-time password and OIDC accounts cannot use
  `npm login`; create a token in the UI instead.
- You need read access to the repository you log in to. Wrong credentials answer `E401`, or `E404` for a repository
  that anonymous users cannot see (so a private repository never reveals that it exists).
- Failed attempts count towards the account lockout and the sign-in rate limit like any other sign-in.

## Publishing (hosted and group repositories)

Publish to a hosted repository, or to a group with a writable member (the package lands in that hosted member).
Pin the target in `package.json` so a stray `npm publish` never goes to the public registry:

```json
"publishConfig": {
  "registry": "https://registry.example.com/repository/npm-hosted/"
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

## Groups

A group answers installs with the members' packuments merged: every version any member has, the **first member that
has a version wins it** (its metadata and its tarball), and dist-tags come from the first member that has the tag.
Put the hosted repository first so your packages win over a public package of the same name and version, and use a
scope you own (`@your-org/...`) for private packages to avoid dependency confusion: a name that exists only in the
proxy member is still installed from the public registry.

## What the proxy does

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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `E404` for a package that exists upstream | Typo in the registry URL (missing `/repository/<name>/` or trailing slash), the repository is offline, or you cannot read it (hidden repositories answer 404). |
| `EINTEGRITY` | The lockfile's integrity does not match the tarball. Waystation verifies tarballs against the upstream packument, so this usually means a lockfile written against a different registry with different content; regenerate the lockfile entry. |
| `E502` / `ETIMEDOUT` on a fresh package | The upstream is unreachable and nothing is cached yet. Check the repository's upstream health on the dashboard. Cached packages keep working when stale-on-error is on. |
| A newly published version is not found | The packument is cached until its metadata max age passes. Requesting a tarball the cached packument does not list triggers one refresh automatically; `npm view <pkg>` after the max age shows new versions. |
| `npm audit` says the endpoint is not supported | `audit_passthrough` is off for this repository, or the upstream does not offer audits. |
| Lockfile contains the Waystation host name | Add `omit-lockfile-registry-resolved=true` to `.npmrc` (see above). |
| `E403 You cannot publish over the previously published versions` | The hosted repository's write policy is `disable_redeploy` (the default for releases): bump the version. |
| `E400` on `npm publish` | The tarball and its metadata disagree (name, version, integrity) or the archive is invalid; the message says which. |
| `E405` on `npm publish` | The registry is a proxy, or a group without a writable member. |
| `E403` on `npm unpublish` | Unpublishing needs the `delete` permission (and a token with the `delete` scope). |
| `npm login` answers `E404` | The repository is not visible to you (or anonymous), the credentials are wrong on a private repository, or your account cannot read it. |
| `npm login` hangs or opens a browser | Add `--auth-type=legacy`. |
