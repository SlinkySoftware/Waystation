# PyPI clients

How to point pip, uv, Poetry and PDM at a Waystation **PyPI** repository, and how to upload with twine and
`uv publish`. The repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples below
use `https://waystation.example.com` and repositories called `pypi-proxy` (a proxy of pypi.org), `pypi-internal`
(hosted) and `pypi` (a group of both).

Status (T-P5-PYPI): proxy, hosted and group repositories. Verified by the real-client harness (`tests/clients/pypi/`)
with pip 26, uv 0.13, twine 7, Poetry 2.5 and PDM 2.29: installs through a proxy of pypi.org (PEP 691 JSON pages,
PEP 658 `.metadata` files), `twine upload` and `uv publish` to a hosted repository and through a group, `poetry
install` and `pdm install` (lock + install) through a group, yanking, a token narrowed to other repositories.

## Repository types

- **Proxy**: caches an upstream index. The upstream URL is the index root, as in Nexus: `https://pypi.org` (pages
  are fetched from `<url>/simple/<project>/`). A URL that already ends in `/simple/` (or devpi's `/+simple/`) is used
  as the simple root itself. Project pages are revalidated after the repository's metadata max age; files are kept
  (they never change) and verified against the page's `sha256` before they are cached.
- **Hosted**: your own uploads (twine, `uv publish`, `poetry publish`). Wheels and sdists (`.tar.gz`, `.zip`).
- **Group**: one index URL for several PyPI repositories. Project pages are merged (the first member listing a file
  name wins, so put hosted repositories first); uploads go to the group's **writable member** (a hosted repository).

Every repository answers at `https://waystation.example.com/repository/<name>/simple/`.

## pip

```sh
pip install --index-url https://waystation.example.com/repository/pypi/simple/ requests
```

or for every pip command, `~/.config/pip/pip.conf` (Linux/macOS) or `%APPDATA%\pip\pip.ini` (Windows):

```ini
[global]
index-url = https://waystation.example.com/repository/pypi/simple/
```

Plain-HTTP deployments also need `trusted-host = waystation.example.com` (pip ignores insecure hosts otherwise).

## uv

```sh
uv pip install --index-url https://waystation.example.com/repository/pypi/simple/ requests
```

Per project (`pyproject.toml`):

```toml
[[tool.uv.index]]
name = "waystation"
url = "https://waystation.example.com/repository/pypi/simple/"
default = true
```

`uv.lock` records the file URLs it resolved (`https://waystation.example.com/repository/pypi/packages/...`). Those
URLs keep working: they are served through the repository (and its cache) by project and file name.

## Poetry

```sh
poetry source add --priority=primary waystation https://waystation.example.com/repository/pypi/simple/
poetry config http-basic.waystation <username> <api-token>
```

## PDM

```sh
pdm config pypi.url https://waystation.example.com/repository/pypi/simple/
```

or per project (`pyproject.toml`; PDM expands environment variables in the URL):

```toml
[[tool.pdm.source]]
name = "pypi"
url = "https://__token__:${WAYSTATION_TOKEN}@waystation.example.com/repository/pypi/simple/"
```

A plain-HTTP deployment also needs `verify_ssl = false` on the source: PDM ignores untrusted hosts otherwise.

## Uploading (twine, uv publish)

Uploads use the legacy upload API (`POST` to the repository URL). The URL is the repository itself, not
`.../simple/`. `~/.pypirc`:

```ini
[distutils]
index-servers = waystation

[waystation]
repository = https://waystation.example.com/repository/pypi-internal/
username = <username>
password = <api-token>
```

```sh
twine upload --repository waystation dist/*
uv publish --publish-url https://waystation.example.com/repository/pypi-internal/ \
  --username <username> --password <api-token> dist/*
```

Uploading to a group (`.../repository/pypi/`) lands in its writable member; you need `write` on both.

What the server checks before a file is stored:

- the file name is a wheel or an sdist (`.tar.gz`, `.zip`) and names a valid project and version;
- the archive is sound: its core metadata (`*.dist-info/METADATA` of a wheel, `PKG-INFO` of an sdist) is read with
  size limits, and an archive with absolute paths, `..` entries or links outside it is refused;
- the metadata's `Name` and `Version` agree with the file name and the form (`name`, `version`), compared as
  normalised names and PEP 440 versions;
- `sha256_digest`, when sent (twine and uv always send it), matches the bytes.

A wheel's `METADATA` is also served as `<file>.metadata` (PEP 658), so installers resolve dependencies without
downloading wheels.

The repository's **write policy** decides what happens to a file name that exists already:

| Policy | Same file name, different content | Same bytes again |
|---|---|---|
| `allow_redeploy` (default) | replaced | accepted |
| `disable_redeploy` | `400 File already exists` (`twine upload --skip-existing` skips it) | accepted |
| `read_only` | `403` | `403` |

## Yanking (PEP 592)

A hosted release can be yanked from the repository's **Browse** tab (open the project, then **Yank** on the version,
with an optional reason) or through the API:

```sh
curl -X POST -H 'Content-Type: application/json' -H 'Authorization: Bearer <api-token>' \
  https://waystation.example.com/api/v1/repositories/pypi-internal/pypi/yank \
  -d '{"project": "my-lib", "version": "1.2.0", "yanked": true, "reason": "broken build"}'
```

Installers skip a yanked release unless it is pinned exactly (`my-lib==1.2.0`). `"yanked": false` un-yanks it. Needs
`write`; audited as `content.update`.

## Authentication

Proxy repositories are read anonymously when the deployment allows anonymous reads (the default for proxies). Hosted
and group repositories, and uploads, need an API token: create one on your profile page (avatar menu → Profile → API
tokens; shown once) or, for CI, on a service account. PyPI clients send it with HTTP basic auth: any username (for
example `__token__`) and the token as the password, for example in the index URL
`https://__token__:<api-token>@waystation.example.com/repository/pypi/simple/`, uv's
`UV_INDEX_WAYSTATION_USERNAME` / `UV_INDEX_WAYSTATION_PASSWORD`, Poetry's `http-basic`, or twine's `.pypirc`. Account
passwords are not accepted by package clients. A repository the token cannot read answers `404`. An index URL without
credentials gets the `401` Basic challenge from a repository anonymous users cannot see (ADR 0005), so pip can take
the credentials from a keyring (`--keyring-provider`) after the challenge.

## Troubleshooting

- **`404` for a project that exists upstream**: the URL must end in `/simple/<project>/`; Waystation redirects
  non-normalised names (`Foo_Bar` → `foo-bar`) and a missing trailing slash with `301`. Check the repository's
  upstream URL (`https://pypi.org`, not `https://pypi.org/pypi`).
- **`502` for a file**: the upstream bytes did not match the page's hash (the file is not cached; retry or check the
  upstream) or the upstream is down with nothing cached.
- **pip ignores the index** on a plain-HTTP deployment: add `--trusted-host`.
- **A project only on your hosted repository is also requested from pypi.org**: a group asks every member for a page
  it merges, including proxies (as Nexus does). Name internal projects so they cannot be registered publicly, or use
  the hosted repository directly for them.
- The legacy JSON API (`/pypi/<project>/json`) is proxied for proxies of a root upstream URL (option `json_api`) and
  answered by groups from their first proxy member that has it; hosted repositories do not serve it.
