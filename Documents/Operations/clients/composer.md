# Composer (PHP): client guide

A Composer proxy repository caches a Composer 2 repository (Packagist or any Composer 2 server): its package metadata
and the package archives. A Composer hosted repository stores package zips you upload. A Composer group serves several
Composer repositories as one. Tested with Composer 2.11 (`composer update`/`install` with `auth.json`; upload and
delete with curl; `tests/clients/composer/`). Protocol notes: FORMATS.md "Composer".

Examples use `https://registry.example.com` as the Waystation base URL. The repository URL a client needs is
`https://registry.example.com/repository/<repo>/` (Composer reads `packages.json` there).

## Create the repository

In the UI (Repositories → Create → Composer) or the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: a Composer 2 repository, e.g. `https://repo.packagist.org` (preset "repo.packagist.org"). Option `dist_hosts`: hosts whose package archives Waystation fetches and caches (default `github.com`, `codeload.github.com`, `api.github.com`, `gitlab.com`; the upstream's own host is always allowed). Archives elsewhere keep their original URL and the client downloads them itself. Option `source_mode`: `keep` (default) leaves each version's VCS `source` in the metadata; `strip` removes it, so clients can only install the cached archives (branches without an archive cannot be installed). |
| Hosted | Write policy: `allow_redeploy` (default: uploading an existing version replaces it), `disable_redeploy` (a version is uploaded once; again answers `409`), `read_only` (uploads and deletes answer `403`). |
| Group | Members in order (Composer proxy and hosted repositories). Upload to the hosted repository itself: a group has no writable member. |

Nexus migration: repositories of Nexus's Composer plugin (`composer-proxy`, `composer-hosted`, `composer-group`)
import with the default options.

## Use the repository in a project

```sh
composer config repositories.waystation composer https://registry.example.com/repository/composer-group/
composer config repositories.packagist.org false
composer require monolog/monolog
```

or in `composer.json`:

```json
{
  "repositories": [
    {"type": "composer", "url": "https://registry.example.com/repository/composer-group/"},
    {"packagist.org": false}
  ]
}
```

`"packagist.org": false` stops Composer asking Packagist directly; every package then comes through Waystation. A
plain-HTTP base URL also needs `composer config secure-http false` (prefer HTTPS).

## Upload and delete (hosted)

The upload is the package as a zip with `composer.json` at its root (or in one top-level folder, as GitHub archives
are), sent as the request body to `packages/upload/<vendor>/<name>/<version>`:

```sh
cd my-package
zip -r ../acme-demo-1.0.0.zip . -x '.git/*' 'vendor/*'
curl -u <username>:<api-token> --upload-file ../acme-demo-1.0.0.zip \
  https://registry.example.com/repository/composer-hosted/packages/upload/acme/demo/1.0.0
curl -u <username>:<api-token> -X DELETE \
  https://registry.example.com/repository/composer-hosted/packages/upload/acme/demo/1.0.0
```

The `name` in `composer.json` (when it has one) must be the uploaded `vendor/name`, and a `version` in it (usually
left out) must be the uploaded one. Versions are normalised as Composer does: `v1.0`, `1.0` and `1.0.0` are the same
version; `dev-main` or `1.x-dev` uploads are branches (Composer reads them from the `~dev` metadata). Uploads
need the write permission and deletes the delete permission; both are audited. At most 256 MiB per package.

## Authentication

Composer sends HTTP basic credentials from `auth.json` (in the project or, with `--global`, in `COMPOSER_HOME`). Use
an API token as the password:

```sh
composer config http-basic.registry.example.com <username> <api-token>
```

The key is the host, with `:port` when the base URL has one. Leave it out when anonymous reads are allowed.
Composer also sends the credentials for the archive URLs, which are on the same host.

## How it behaves

- `packages.json` is generated per repository and only points at the repository (`metadata-url`); Composer 1
  (`providers`) is not served.
- A proxy caches each package's `p2` document as metadata (revalidated after the repository's metadata max age) and
  rewrites the archive URLs on allowed hosts to the repository. Only the fields Composer repositories publish are kept
  (no `scripts`).
- Archives are cached forever. GitHub API zipball URLs are fetched from `codeload.github.com` directly (no GitHub API
  rate limit). An archive is checked (a readable zip or tar that is safe to extract), verified against the `shasum`
  the metadata gives (Packagist usually gives none) and its SHA-256 is pinned the first time it is cached: if it is
  removed (cleanup) and refetched, it must be the same bytes, otherwise the download fails with `502` and nothing is
  stored.
- A group answers a package's metadata with every member's versions merged (the first member that has a version wins
  it) and serves archives from the first member that has them.
- Browse (Repositories → a Composer repository) lists packages and their versions newest first in Composer's order
  (stable before release candidates, betas, alphas and branches), with what is cached.
- Prefetch (proxy repositories): warm items `vendor/name` (the newest stable version) or `vendor/name:1.2.3`; "keep
  popular metadata fresh" refreshes `p2` documents.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `The "http://..." file could not be downloaded: ... secure-http` | Plain-HTTP repository: `composer config secure-http false`, or use HTTPS. |
| `401` / Composer asks for credentials | Anonymous reads are not allowed: add the `http-basic` entry with an API token. A wrong or revoked token also answers `401`. |
| A package is not found | Check the name (`vendor/name`), that `packagist.org` is disabled only where Waystation proxies Packagist, and the proxy's upstream status on the repository page. |
| An archive download answers `502` | The upstream archive failed a check (not a safe archive, `shasum` mismatch, or different bytes than the pinned ones). The repository's recent errors show which. |
| An archive URL still points at GitHub or another host | The host is not in the proxy's `dist_hosts`, or the archive is not a zip/tar. Add the host; the metadata picks it up when it is next refreshed. |
| Upload answers `400` | Not a zip, no `composer.json` at the root (or in a single top-level folder), the name or version in it differs from the URL, or the version is not one Composer accepts. |
| Upload answers `409` | The repository does not allow redeploying and the version exists (`1.0` and `1.0.0` are the same version). |
