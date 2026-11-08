# npm clients

How to point npm, pnpm, Yarn and Bun at a Waystation **npm proxy** repository. The repository's **Set me up** tab
shows the same snippets with your real URL filled in. Examples below use `https://waystation.example.com` and a
repository called `npm`.

Status (M0): proxy repositories only (packuments full and abbreviated, tarballs, scoped packages, search, audit,
ping). Hosted (publish, dist-tags, unpublish) and group repositories come later (FORMATS.md "npm"). Verified with
npm 11 by the real-client harness (`tests/clients/npm/`); pnpm, Yarn and Bun use the same registry protocol and
are expected to work but are not yet in the harness.

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
Otherwise put an API token in `.npmrc` as `_authToken` for the registry URL (npm sends it as
`Authorization: Bearer`). Create an API token on your profile page (avatar menu → Profile → API tokens; shown once, copy it then) or, for
CI, on a service account. Give it only the `read` scope and, ideally, restrict it to the repositories the client
needs: a token only ever narrows what its owner may do. A revoked or expired token is refused at once (401); it
never falls back to anonymous access.

```ini
registry=https://registry.example.com/repository/npm/
//registry.example.com/repository/npm/:_authToken=${WAYSTATION_TOKEN}
```

Keep the token in an environment variable (as above) or in the user-level `~/.npmrc`, never in a project `.npmrc`
that is committed. `npm login` (the CouchDB login flow) is not supported; create the token in the UI. A revoked token
makes npm fail with `E401`; a token restricted to other repositories gets `E404`.

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
