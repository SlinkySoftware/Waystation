# Raw (generic files): client guide

A raw proxy repository mirrors a plain HTTP(S) file tree: installers, ISOs, release binaries, checksums, install
scripts. Any HTTP client works; curl and wget are tested (`tests/clients/raw/`). Protocol notes: FORMATS.md "Raw".

Examples use `https://registry.example.com` as the Waystation base URL and a repository named `files`.

## Create the repository

In the UI (Repositories → Create → Raw files → Proxy) or the API, set:

| Setting | Meaning |
|---|---|
| Upstream URL | The base of the remote tree, e.g. `https://downloads.example.com/releases/`. `<repo>/<path>` is fetched from `<upstream URL>/<path>`. |
| Metadata path regex (`metadata_path_regex`) | Paths that change in place (`latest`, `index.json`, `SHA256SUMS` of a rolling release). Matched against `/<path>`, anywhere in it: `(^\|/)latest$`, `.*/(index\.json\|latest)$`, `^/nightly/`. Matching files are revalidated with the upstream after the metadata max age; everything else is cached for the content max age (default: forever). |
| Content disposition | `attachment` (default): browsers download files. `inline`: browsers may display them; HTML, SVG and XML are then served as plain text. |
| Negative cache TTL | How long an upstream 404 is remembered before asking again. |

## Client configuration

Replace the upstream host and base path with the repository URL; the rest of the path stays the same.

```sh
# curl: download, keep the file name, fail on HTTP errors, follow redirects
curl -fSLO https://registry.example.com/repository/files/v1.2.3/tool-linux-amd64.tar.gz

# wget
wget https://registry.example.com/repository/files/v1.2.3/tool-linux-amd64.tar.gz

# scripts that take a download base URL
DOWNLOAD_BASE=https://registry.example.com/repository/files ./install.sh

# resume a partial download (ranges are served from the cache)
curl -fSL -C - -O https://registry.example.com/repository/files/images/installer.iso
```

The repository's **Set me up** tab shows the same commands with your repository URL filled in.

Responses carry `X-Ws-Cache` (`FETCHING`, `HIT`, `REVALIDATED`, `STALE`), a digest `ETag`
(`"sha256:<hex>"`), `Last-Modified`, `Content-Disposition` and `X-Content-Type-Options: nosniff`. Cached files
answer `If-None-Match`/`If-Modified-Since` with `304` and `Range` with `206`, so `curl -z`, `wget -N` and download
managers work.

## Authentication

Proxy repositories are readable anonymously by default (the built-in `anonymous` role has browse + read on
proxies), so no client configuration is needed. When an administrator removes anonymous access, or to write to a
hosted repository, send an API token: as the password of HTTP basic auth (any username), or as a bearer token.
Create an API token on your profile page (avatar menu → Profile → API tokens; shown once, copy it then) or, for
CI, on a service account. Give it only the `read` scope and, ideally, restrict it to the repositories the client
needs: a token only ever narrows what its owner may do. A revoked or expired token is refused at once (401); it
never falls back to anonymous access.

```sh
curl -fSLO -u alice:<your-api-token> https://registry.example.com/repository/files/path/to/file
curl -fSLO -H 'Authorization: Bearer <your-api-token>' https://registry.example.com/repository/files/path/to/file
wget --user=alice --password='<your-api-token>' https://registry.example.com/repository/files/path/to/file
```

Basic auth with an account password is refused (401): format clients always use API tokens.

Prefer a `~/.netrc` entry (`machine registry.example.com login alice password <token>`, `chmod 600`) with
`curl -n` / wget's built-in netrc support over tokens on the command line.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `404` for a file that exists upstream | The upstream URL's base path is wrong (check it ends where your paths start; a trailing `/` is implied), or an earlier 404 is still in the negative cache: wait for the negative cache TTL or fix the upstream and retry later. The repository root and paths ending in `/` answer 404: directory listings are not available yet. |
| `502 Bad Gateway` | The upstream answered an error (5xx, 401/403: check upstream credentials in the repository settings), redirected to a private address (allowed only with *allow private upstream*), or sent more than the size limit (`WAYSTATION_MAX_UPLOAD_GB`). The repository's upstream health card shows the last error. |
| `504 Gateway Timeout` | The upstream did not answer in time. A stale cached copy is served instead when one exists and *stale on error* is on. |
| Download stops part-way on the first request | The upstream connection broke while the file was streamed through; nothing was cached. Retry. |
| An updated upstream file is still served old | Its path does not match the metadata regex, so it is treated as immutable content. Add the path to `metadata_path_regex`. A file already cached as content keeps its kind until it is fetched again, so also set a content max age (cached content is then revalidated with the upstream once it is older), or wait for deleting single cached files (browse actions, a later task). |
| The browser shows text instead of rendering a page | Intended: upstream HTML is never rendered on the Waystation origin. |
| `401` | Anonymous access was removed for this repository: send credentials (above). |
| `405 Method Not Allowed` / `401` on `PUT` | Uploads go to hosted raw repositories (not available yet), never to a proxy. |
