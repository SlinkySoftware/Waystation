# Raw (generic files) client guide

How to download files through a Waystation raw repository with curl, wget or any HTTP client, and how to upload,
delete and list files in a hosted repository. The repository's **Set me up** tab shows the same commands with your
real URL filled in. Examples use `https://waystation.example.com` as the Waystation base URL and a repository named
`files`.

## Overview

A raw repository serves plain files by path: installers, ISOs, release binaries, checksums, install scripts. Any
HTTP client works; curl and wget are tested. Protocol notes: [FORMATS.md](../../Architecture/FORMATS.md), section
"Raw".

URL layout: `https://waystation.example.com/repository/<repo>/<path>`. For a proxy, `<repo>/<path>` is fetched from
`<upstream URL>/<path>`; for a hosted repository the path is where the file is stored.

Responses carry `X-Ws-Cache` (`FETCHING`, `HIT`, `REVALIDATED`, `STALE`), a digest `ETag` (`"sha256:<hex>"`),
`Last-Modified`, `Content-Disposition` and `X-Content-Type-Options: nosniff`. Cached files answer
`If-None-Match`/`If-Modified-Since` with `304` and `Range` with `206`, so `curl -z`, `wget -N`, resumed downloads
and download managers work.

### Directory listings

Every raw repository answers directory listings at its root and at any path ending in `/`: HTML in a browser, JSON
for scripts (`Accept: application/json`; paged: follow `next_cursor` with `?cursor=`). A proxy lists what it has
cached (it never asks the upstream for a listing), a group the members' listings merged (the first page of each).
Listing needs the `browse` permission besides `read`.

## Repository types

### Proxy

Mirrors a plain HTTP(S) file tree. Create one in the UI (Repositories → Create → Raw files → Proxy) or the API:

| Setting | Meaning |
|---|---|
| Upstream URL | The base of the remote tree, e.g. `https://downloads.example.com/releases/`. `<repo>/<path>` is fetched from `<upstream URL>/<path>`. |
| Metadata path regex (`metadata_path_regex`) | Paths that change in place (`latest`, `index.json`, `SHA256SUMS` of a rolling release). Matched against `/<path>`, anywhere in it: `(^\|/)latest$`, `.*/(index\.json\|latest)$`, `^/nightly/`. Matching files are revalidated with the upstream after the metadata max age; everything else is cached for the content max age (default: forever). |
| Content disposition | `attachment` (default): browsers download files. `inline`: browsers may display them; HTML, SVG and XML are then served as plain text. |
| Negative cache TTL | How long an upstream 404 is remembered before asking again. |

### Hosted

Stores files you upload with HTTP `PUT` (Create → Raw files → Hosted). The **write policy** is `allow_redeploy`
(default: an upload replaces the file at that path), `disable_redeploy` (a path is written once; uploading it again
answers `409`, deleting stays possible) or `read_only` (every upload and delete answers `403`).

### Group

Serves several raw repositories under one URL (Create → Raw files → Group). It lists its **members** in order
(hosted and proxy raw repositories) and optionally a **writable member**, the hosted member that receives uploads
sent to the group. Files are read from the first member that has them (`X-Ws-Group-Member` names it).

## Client setup

Replace the upstream host and base path with the repository URL; the rest of the path stays the same.

### curl

```sh
# download, keep the file name, fail on HTTP errors, follow redirects
curl -fSLO https://waystation.example.com/repository/files/v1.2.3/tool-linux-amd64.tar.gz

# resume a partial download (ranges are served from the cache)
curl -fSL -C - -O https://waystation.example.com/repository/files/images/installer.iso
```

### wget

```sh
wget https://waystation.example.com/repository/files/v1.2.3/tool-linux-amd64.tar.gz
```

### Scripts and installers

Scripts that take a download base URL point at the repository:

```sh
DOWNLOAD_BASE=https://waystation.example.com/repository/files ./install.sh
```

## Authentication

Proxy repositories are readable anonymously by default (the built-in `anonymous` role has browse + read on
proxies), so no client configuration is needed. When an administrator removes anonymous access, or to read or write
a hosted repository, send an API token: as the password of HTTP basic auth (any username), or as a bearer token.
Create an API token on your profile page (avatar menu → Profile → API tokens; shown once, copy it then) or, for
CI, on a service account. Give it only the scopes it needs (`read` to download; `write` / `delete` to upload and
delete) and, ideally, restrict it to the repositories the client needs: a token only ever narrows what its owner
may do. A revoked or expired token is refused at once (401); it never falls back to anonymous access.

```sh
curl -fSLO -u <username>:<your-api-token> https://waystation.example.com/repository/files/path/to/file
curl -fSLO -H 'Authorization: Bearer <your-api-token>' https://waystation.example.com/repository/files/path/to/file
wget --user=<username> --password='<your-api-token>' https://waystation.example.com/repository/files/path/to/file
wget --header 'Authorization: Bearer <your-api-token>' https://waystation.example.com/repository/files/path/to/file
```

Basic auth with an account password is refused (401): format clients always use API tokens. Tokens of LDAP and OIDC
users work the same as local users' tokens.

Prefer a `~/.netrc` entry (`machine waystation.example.com login <username> password <token>`, `chmod 600`) with
`curl -n` / wget's built-in netrc support over tokens on the command line.

## Publishing

Upload to a hosted repository, or to a group with a writable member.

```sh
# upload (PUT); the path after the repository name is where the file is stored
curl -fS -u <username>:<your-api-token> --upload-file app-1.0.tar.gz \
  https://waystation.example.com/repository/files/releases/1.0/app-1.0.tar.gz

# upload with a checksum the server verifies before storing anything (400 on a mismatch)
curl -fS -u <username>:<your-api-token> -H "X-Checksum-Sha256: $(sha256sum app-1.0.tar.gz | cut -d' ' -f1)" \
  --upload-file app-1.0.tar.gz https://waystation.example.com/repository/files/releases/1.0/app-1.0.tar.gz

# delete
curl -fS -u <username>:<your-api-token> -X DELETE \
  https://waystation.example.com/repository/files/releases/1.0/app-1.0.tar.gz

# list a folder as JSON
curl -fsS -u <username>:<your-api-token> -H 'Accept: application/json' \
  https://waystation.example.com/repository/files/releases/
```

- `X-Checksum-Sha1` and `X-Checksum-Md5` (hex) are verified too; send any combination.
- An upload answers `201` with the stored file's `ETag` (`"sha256:<hex>"`) and `X-Checksum-Sha256`.
- Uploads are limited to `WAYSTATION_MAX_UPLOAD_GB` (`413` beyond it) and need the `write` permission (an API token
  with the `write` scope); deletes need `delete`.
- The stored Content-Type comes from the file extension, never from the client.
- Through a group, uploads and deletes go to the writable member, and you need `write`/`delete` on that member too.
  Without a writable member the group answers `405` to uploads.

## Limitations

- Proxy repositories are read-only and list only what they have cached; they never fetch upstream directory
  listings.
- A file cached as immutable content is not re-fetched until it is older than the content max age (default:
  forever); use the metadata path regex for paths that change in place.
- Upstream HTML is never rendered on the Waystation origin: with `inline` disposition, HTML, SVG and XML are served
  as plain text.
- Upload size is bounded by `WAYSTATION_MAX_UPLOAD_GB`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `404` for a file that exists upstream | The upstream URL's base path is wrong (check it ends where your paths start; a trailing `/` is implied), or an earlier 404 is still in the negative cache: wait for the negative cache TTL or fix the upstream and retry later. A folder path (ending in `/`) answers 404 when nothing is stored or cached under it. Hosted repositories are hidden from clients without access: a token that cannot read the repository gets 404 (a request without any credentials gets the `401` Basic challenge, as for a missing repository). |
| `502 Bad Gateway` | The upstream answered an error (5xx, 401/403: check upstream credentials in the repository settings), redirected to a private address (allowed only with *allow private upstream*), or sent more than the size limit (`WAYSTATION_MAX_UPLOAD_GB`). The repository's upstream health card shows the last error. |
| `504 Gateway Timeout` | The upstream did not answer in time. A stale cached copy is served instead when one exists and *stale on error* is on. |
| Download stops part-way on the first request | The upstream connection broke while the file was streamed through; nothing was cached. Retry. |
| An updated upstream file is still served old | Its path does not match the metadata regex, so it is treated as immutable content. Add the path to `metadata_path_regex`. A file already cached as content keeps its kind until it is fetched again, so also set a content max age (cached content is then revalidated with the upstream once it is older). |
| The browser shows text instead of rendering a page | Intended: upstream HTML is never rendered on the Waystation origin. |
| `401` | Anonymous access was removed for this repository: send credentials (see "Authentication"). |
| `405 Method Not Allowed` on `PUT` | Uploads go to hosted raw repositories (or a group with a writable member), never to a proxy. |
| `409 Conflict` on `PUT` | The hosted repository's write policy is `disable_redeploy` and the path exists: upload under a new path (a new version). |
| `400` on `PUT` | A checksum header does not match the body (nothing was stored), a checksum header is malformed, or the URL ends in `/` (name the file). |
| `507` on `PUT` | The repository's hard quota is reached, or the server is low on disk space: delete files or ask an administrator to raise the quota. Downloads keep working. |
| `403` on `PUT`/`DELETE` | The token lacks the `write`/`delete` scope or permission, the repository is `read_only`, or (through a group) you may not write to its writable member. |
