# Go clients

How to point the go command at a Waystation **Go modules** repository. The repository's **Set me up** tab shows the
same snippets with your real URL filled in. Examples below use `https://waystation.example.com` and repositories
called `go-proxy` (a proxy of proxy.golang.org) and `go` (a group).

Status (T-P5-GO): proxy and group repositories (Go has no hosted repositories: modules are published by tagging them
in version control). Verified by the real-client harness (`tests/clients/go/`) with go 1.25: `go list -m -versions`,
`go mod download` and `go run` of a small module with two public dependencies through a proxy of proxy.golang.org,
every module verified against sum.golang.org through the repository's checksum database proxy, then through a group
with an API token, and a token narrowed to other repositories.

## Repository types

- **Proxy**: caches a module proxy (default `https://proxy.golang.org`; any GOPROXY-protocol server works, e.g.
  Athens or another Waystation). Version lists (`@v/list`) and `@latest` are metadata, revalidated after the
  repository's metadata max age; `.info`, `.mod` and `.zip` files of a version are content and kept (they never
  change). A proxy repository also proxies the **checksum database** (below).
- **Group**: one GOPROXY URL for several Go proxy repositories. Version lists are merged; every file comes from the
  first member that has it.

Every repository answers at `https://waystation.example.com/repository/<name>/` (the GOPROXY URL).

## The go command

```sh
go env -w GOPROXY=https://waystation.example.com/repository/go
go mod download
```

Without `,direct` in `GOPROXY` every module comes through Waystation and the go command never reaches a version
control host. For one command: `GOPROXY=https://waystation.example.com/repository/go go get example.com/mod@latest`.

## Checksum database (GOSUMDB)

Leave `GOSUMDB` at its default (`sum.golang.org`). Before contacting the database the go command asks its proxy for
`<GOPROXY>/sumdb/sum.golang.org/supported`; a Waystation proxy repository (option **checksum database proxy**, on by
default) answers yes, so lookups and tiles go through Waystation too and machines without internet access still
verify every module. The go command checks the database's signatures and inclusion proofs itself, as it would
against sum.golang.org directly.

The repository options:

| Option | Default | Meaning |
|---|---|---|
| `sumdb_proxy` | on | Serve `sumdb/<sumdb_name>/...` (`supported`, `latest`, `lookup/<module>@<version>`, `tile/...`). Off: `supported` answers `404` and the go command contacts the database directly. |
| `sumdb_name` | `sum.golang.org` | The database name (GOSUMDB) served. |
| `sumdb_url` | `https://sum.golang.org` | Where the database is fetched from (the SSRF guard applies as for the upstream). |
| `verify_checksums` | on | Check `.mod` and `.zip` files against the database's `go.sum` lines before caching them. |
| `nosumdb` | empty | Module path patterns (GONOSUMDB syntax) never looked up in the database: private modules. |

What Waystation verifies (`verify_checksums`): before a `.mod` or `.zip` file is cached (and before the client gets
its last bytes) the module version's lookup record is fetched (and cached) through the repository, and the file's
`h1:` hash (the go command's `dirhash`, computed over the zip's files in a thread, bounded in entries and size) must
match the record's `go.sum` line; a mismatch answers `502` and nothing is cached. A module the database does not know
(`404`/`410`, e.g. a private one) is cached unverified; a database that cannot be reached fails the download (`502`).
`.info` files must be JSON naming the requested version. Waystation does not check the database's signature or the
record's inclusion in the tree: the go command does that for its own downloads.

Database tiles are cached forever (full tiles never change; hash tiles must have the exact size), partial tiles and
`latest` are revalidated.

## Private modules

Modules not in the public checksum database (your own, fetched through a proxy of another GOPROXY server):

```sh
go env -w GONOSUMDB=example.com/private/*
```

and add the same pattern to the repository's `nosumdb` option, so Waystation does not look them up either.
`GOPRIVATE=...` also sets `GONOPROXY`, which makes the go command bypass the proxy for those paths: use it only for
modules you fetch directly from version control.

## Authentication

Proxy repositories are read anonymously when the deployment allows anonymous reads (the default for proxies). Groups
and restricted repositories need an API token: create one on your profile page (avatar menu → Profile → API tokens;
shown once) or, for CI, on a service account. The go command sends credentials from `~/.netrc` (`%USERPROFILE%\_netrc`
on Windows; `NETRC` names another file), **over HTTPS only**:

```text
machine waystation.example.com
login <username>
password <api-token>
```

Any username works with a token. Credentials in the URL also work, and are the only way over plain HTTP:
`GOPROXY=https://<username>:<api-token>@waystation.example.com/repository/go`. Account passwords are not accepted by
package clients. A repository the token cannot read answers `404` (the go command reports "not found").

## Behaviour notes

- Module paths and versions are case-encoded in URLs (`github.com/Azure/x` → `github.com/!azure/x`). Waystation
  decodes, validates and re-encodes every path, so an upper-case request names the same cached file; a malformed
  escape (`!A`) or an invalid path answers `404`.
- Upstream `404` and `410` both answer `404` (the go command treats them alike). `.mod` and `.zip` need a canonical
  version (`v1.2.3`, pre-releases, pseudo-versions, `+incompatible`); `.info` also answers queries (`master.info`),
  cached as metadata.
- Size limits: version lists 4 MiB, `.info` 1 MiB, `.mod` 16 MiB, `.zip` 500 MiB (the go command's own limits).
- Groups answer `@v/list` merged and newest first (`ETag`, `304`), files from any member's cached copy first, then the
  members in order (`X-Ws-Group-Member` names the member), and the checksum database from the first member that
  proxies it.
- Browse: the repository's **Browse** tab lists modules (latest, tagged and cached versions, size) and each module's
  versions in semantic version order (pseudo-version, pre-release and `+incompatible` badges, commit time, the
  `.info`/`.mod`/`.zip` files held). Search finds modules by path.

## Troubleshooting

- **`verifying module: ... 404`** on sum.golang.org or a private module: add it to `GONOSUMDB` (and the repository's
  `nosumdb`).
- **`502` for a `.zip` or `.mod`**: the upstream bytes did not match the checksum database (nothing was cached; check
  the upstream), the database could not be reached, or the upstream is down with nothing cached.
- **The go command contacts sum.golang.org directly**: the repository's checksum database proxy is off, or `GOSUMDB`
  names another database. Check `curl https://waystation.example.com/repository/go/sumdb/sum.golang.org/supported`.
- **`go: ... unrecognized import path`** or a version control fetch: `GOPROXY` ends in `,direct` or the path matches
  `GOPRIVATE`/`GONOPROXY`.
