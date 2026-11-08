# apt (Debian, Ubuntu): client guide

An apt proxy repository caches a Debian-style archive (`deb.debian.org/debian`, `deb.debian.org/debian-security`,
`archive.ubuntu.com/ubuntu`, `security.ubuntu.com/ubuntu`, a vendor repository). Tested with apt on Debian 13 and
Ubuntu 26.04 (`tests/clients/apt/`). Protocol notes: FORMATS.md "apt".

Examples use `https://registry.example.com` as the Waystation base URL and repositories named `debian`,
`debian-security`, `ubuntu` and `ubuntu-security`.

## Create the repositories

One repository per upstream archive, not per suite: every suite of the archive (`trixie`, `trixie-updates`,
`trixie-backports`) goes through the same repository. Security archives live at a different upstream URL, so they
get their own repository.

| Repository | Upstream URL | Distribution |
|---|---|---|
| `debian` | `https://deb.debian.org/debian/` | `trixie` |
| `debian-security` | `https://deb.debian.org/debian-security/` | `trixie-security` |
| `ubuntu` | `https://archive.ubuntu.com/ubuntu/` | `resolute` |
| `ubuntu-security` | `https://security.ubuntu.com/ubuntu/` | `resolute-security` |

*Distribution* is informational (the "Set me up" snippets and the upstream probe use it); it does not limit the
suites clients can use. Set *flat* only for flat repositories (`deb <url> ./`, no `dists/`). The metadata max age
(default 30 minutes) is how long a cached `InRelease` is served before Waystation asks the upstream again.

## Client configuration

Point the `URIs` (or the URL of a one-line entry) at the repository and keep everything else, **including
`Signed-By`**: apt still verifies the distribution's own signature, because Waystation passes `InRelease`,
`Release` and `Release.gpg` through byte for byte. There is no Waystation key to install.

deb822 (Debian 12+, Ubuntu 24.04+), e.g. `/etc/apt/sources.list.d/debian.sources` replacing the stock file:

```text
Types: deb
URIs: https://registry.example.com/repository/debian/
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://registry.example.com/repository/debian-security/
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
```

Ubuntu (`/etc/apt/sources.list.d/ubuntu.sources`):

```text
Types: deb
URIs: https://registry.example.com/repository/ubuntu/
Suites: resolute resolute-updates resolute-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: https://registry.example.com/repository/ubuntu-security/
Suites: resolute-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
```

One-line format (`/etc/apt/sources.list.d/*.list`, older releases):

```text
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://registry.example.com/repository/debian/ trixie main
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://registry.example.com/repository/debian-security/ trixie-security main
```

Then `sudo apt update`. Remove or comment out the stock entries, otherwise apt also talks to the upstream directly.
The repository's **Set me up** tab shows the snippets with your repository URL filled in.

Over plain `http://` (inside a trusted network) apt needs nothing else; for `https://` on Debian before 13 and
Ubuntu before 24.04 the image needs `ca-certificates` (and `apt-transport-https` on very old releases).

### What Waystation checks

- `InRelease`/`Release` are served as the upstream signed them; clients verify the signature.
- Index files (`Packages`, `Sources`, `Translation-*`, ...) are always the ones the `Release` your client received
  lists, checked against its SHA256. apt's `by-hash` requests (`Acquire-By-Hash`) are cached forever and verified
  against the hash in their path. If the upstream changed an index under its `Release`, Waystation fetches the
  `Release` again once, then fails rather than serve a mismatched set.
- Package files under `pool/` are checked against the SHA256 and size of their entry in a cached `Packages` file.
  A package fetched before any `Packages` file that lists it was cached is served and recorded as *unverified*; when
  a listing arrives and disagrees, the cached copy is dropped and fetched again on the next request.

## Authentication

Proxy repositories are readable anonymously by default, so no client configuration is needed. When an
administrator removes anonymous access, apt authenticates with HTTP basic auth: any username and an API token as the
password. Create an API token on your profile page (avatar menu → Profile → API tokens; shown once, copy it then) or, for
CI, on a service account. Give it only the `read` scope and, ideally, restrict it to the repositories the client
needs: a token only ever narrows what its owner may do. A revoked or expired token is refused at once (401); it
never falls back to anonymous access.

Keep the credentials out of the sources file, in `/etc/apt/auth.conf.d/waystation.conf` (`chmod 600`, owned by
root):

```text
machine registry.example.com/repository/
login alice
password <your-api-token>
```

apt applies an entry without a scheme to `https://` sources only. If Waystation is reached over plain HTTP (not
recommended: the token travels in clear), write the scheme: `machine http://registry.example.com/repository/`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `NO_PUBKEY` / `The following signatures couldn't be verified` | `Signed-By` points at the wrong keyring (Debian vs Ubuntu vs vendor key) or the keyring package is old. Waystation never re-signs; use the keyring you used against the upstream. |
| `404  Not Found [IP: ...]` for `dists/<suite>/InRelease` | The suite does not exist under this repository's upstream URL (e.g. `trixie-security` requested from the `debian` repository: it lives in `debian-security`), or the upstream URL's base path is wrong. |
| `404` for an index file (`.../Packages.xz`) | The `Release` the suite is served with does not list that file (wrong component or architecture, or a compression the archive does not publish). Waystation only serves indexes it can verify. |
| `File has unexpected size` / `Hash Sum mismatch` | Should not happen through Waystation (index sets always match the served `Release`). If it does, the upstream changed files mid-update: run `apt update` again; persistent failures are a bug worth reporting with the repository's activity log. |
| `502 Bad Gateway` on `apt update` | The upstream answered an error, an index failed verification twice (the upstream's `Release` and files disagree), or a `Release` file was malformed. The repository's upstream health card shows the last error. |
| `504 Gateway Timeout` | The upstream did not answer in time. A stale cached `InRelease` is served instead when one exists and *stale on error* is on. |
| Updates appear late | The cached `InRelease` is served until it is older than the metadata max age (default 30 minutes). Lower it for security repositories if needed. |
| `401` | Anonymous access was removed for this repository: add credentials (above). |
