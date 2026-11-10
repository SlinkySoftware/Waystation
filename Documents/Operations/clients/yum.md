# yum / dnf (RHEL family, Rocky Linux, AlmaLinux, Oracle Linux, EPEL, Fedora): client guide

A yum proxy repository caches an RPM repository tree (`dl.rockylinux.org/pub/rocky`, `repo.almalinux.org/almalinux`,
`yum.oracle.com`, `dl.fedoraproject.org/pub/epel`, a vendor repository). The upstream's layout is kept below the
repository, so one Waystation repository serves every yum repository under its upstream URL (`BaseOS`, `AppStream`,
`extras`, every `$releasever` and `$basearch`). Tested with dnf on Rocky Linux 10 (Rocky `extras` and EPEL 10),
AlmaLinux 10 (`extras`) and Oracle Linux 9 (`addons`) (`tests/clients/yum/`). Protocol notes: FORMATS.md "yum / dnf".

Examples use `https://registry.example.com` as the Waystation base URL.

## Create the repositories

One repository per upstream tree:

| Repository | Upstream URL |
|---|---|
| `rocky` | `https://dl.rockylinux.org/pub/rocky/` |
| `almalinux` | `https://repo.almalinux.org/almalinux/` |
| `oracle` | `https://yum.oracle.com/repo/OracleLinux/` |
| `epel` | `https://dl.fedoraproject.org/pub/epel/` |

**Use a concrete base URL.** Mirrorlist and metalink URLs (the `mirrorlist=` / `metalink=` lines of the stock
`.repo` files) are not proxied: point the repository at one mirror's tree instead.

Options (all optional):

- *Repository path* (`repo_path`): the path of the yum repository below the upstream URL, the directory holding
  `repodata/` (e.g. `10/extras/$basearch/os/`). Only the "Set me up" snippet and "test upstream" use it. Leave it
  empty for the well-known upstreams above: the snippet then lists their usual repositories (BaseOS, AppStream,
  extras; Oracle BaseOS/AppStream; EPEL Everything). For any other upstream an empty path means the upstream URL is
  itself a yum repository.
- *GPG key* (`gpg_key`): the `gpgkey` the snippet uses. Empty: the distribution's key in `/etc/pki/rpm-gpg/` for the
  well-known upstreams, else a placeholder to replace.
- *Verify repomd.xml with these keys* (`repomd_keys`): the upstream's armored OpenPGP public key(s), e.g. the content
  of `RPM-GPG-KEY-Rocky-10` (several blocks may be pasted). When set, Waystation fetches `repomd.xml.asc` together
  with every new `repomd.xml` and caches the `repomd.xml` only when the signature verifies with one of these keys
  (version 4 RSA, ECDSA or EdDSA keys with valid self-signatures; SHA-2 or SHA-3 signatures, SHA-1 is refused); otherwise the request fails with `502` (a stale
  copy is served when *stale on error* is on) and nothing is cached. The verified `repomd.xml.asc` is served with
  it. This protects every client, including ones running with `repo_gpgcheck=0`, against an upstream or mirror
  serving tampered metadata. Only for upstreams that sign `repomd.xml`: Rocky Linux and AlmaLinux do; EPEL and Oracle
  Linux do not (their `repomd.xml.asc` answers 404, so every `repomd.xml` would be refused).

The create dialog offers presets for Rocky Linux, AlmaLinux, Oracle Linux and EPEL.

The metadata max age (default 30 minutes) is how long a cached `repomd.xml` is served before Waystation asks the
upstream again.

## Client configuration

Create `/etc/yum.repos.d/<name>.repo` with a `baseurl` into the repository and keep the distribution's key and
`gpgcheck=1`: dnf still verifies every package with the distribution's own signature, because Waystation passes
packages and `repomd.xml` through byte for byte. There is no Waystation key to install. Rocky Linux 10 / AlmaLinux 10:

```ini
[rocky-baseos]
name=Rocky Linux $releasever - BaseOS (Waystation)
baseurl=https://registry.example.com/repository/rocky/$releasever/BaseOS/$basearch/os/
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-$releasever

[rocky-appstream]
name=Rocky Linux $releasever - AppStream (Waystation)
baseurl=https://registry.example.com/repository/rocky/$releasever/AppStream/$basearch/os/
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-$releasever
```

(AlmaLinux: `.../repository/almalinux/...` and `RPM-GPG-KEY-AlmaLinux-$releasever`.) Set `repo_gpgcheck=1` to
also have dnf check `repomd.xml` against the upstream's `repomd.xml.asc` (Rocky and AlmaLinux sign it; the harness
runs with it on).

Then turn off the distribution's own repositories so dnf does not also fetch from the internet, and refresh:

```shell
sudo dnf config-manager --set-disabled baseos appstream extras
sudo dnf makecache
```

The repository's **Set me up** tab shows the snippets with your repository URL filled in.

### What Waystation checks

- `repomd.xml` is served as the upstream published it (dnf verifies `repomd.xml.asc` itself when `repo_gpgcheck=1`);
  one that is not a well-formed `repomd.xml` (or carries a DTD) is refused with `502` and not cached. With
  `repomd_keys` set, Waystation also verifies its signature before caching it (above).
- The files `repomd.xml` lists (`primary`, `filelists`, `other`, `updateinfo`, `modules`, `comps`, in any of `.gz`,
  `.xz`, `.zst`, `.bz2` or plain) are always the ones the `repomd.xml` your client received lists, checked against
  its checksum (SHA-1, SHA-256 or SHA-512) and size. If the upstream changed a file under its `repomd.xml`,
  Waystation fetches `repomd.xml` again once, then fails rather than serve a mismatched set. Files with the checksum
  in their name (the createrepo default) are cached forever.
- Packages (`.rpm`) are checked against the checksum and size of their entry in the repository's `primary.xml`.
  A package requested before Waystation has its `primary.xml` makes Waystation fetch and index that first; a package
  no cached `primary.xml` lists is served and recorded as *unverified*, and when a listing arrives and disagrees the
  cached copy is dropped and fetched again on the next request.

## Hosted repositories

A hosted yum repository stores your own RPMs and serves them with repodata Waystation builds and signs. Create one
with type *Hosted* (no options). Groups are not supported for yum.

Upload with any HTTP client (the `write` permission; an API token as the password):

```shell
# PUT: stored at the path you give (any directory layout)
curl -fS -u alice:<api-token> --upload-file mypackage-1.0-1.el10.x86_64.rpm \
  https://registry.example.com/repository/myrpms/
curl -fS -u alice:<api-token> --upload-file mypackage-1.0-1.el10.x86_64.rpm \
  https://registry.example.com/repository/myrpms/Packages/m/mypackage-1.0-1.el10.x86_64.rpm
# POST: the body is the package, stored as <name>-<version>-<release>.<arch>.rpm in that directory
curl -fS -u alice:<api-token> --data-binary @mypackage-1.0-1.el10.x86_64.rpm \
  -H 'Content-Type: application/x-rpm' https://registry.example.com/repository/myrpms/
# Delete (the delete permission)
curl -fS -u alice:<api-token> -X DELETE \
  https://registry.example.com/repository/myrpms/mypackage-1.0-1.el10.x86_64.rpm
```

Waystation reads the package header (binary and source RPMs; refused with `400` when it is not a well-formed RPM)
and a worker rebuilds `repodata/` within seconds: `primary`, `filelists` and `other` (gzip) and `repomd.xml`, signed
with the repository's own OpenPGP key into `repomd.xml.asc`. The key (RSA 3072) is created on the first build and
kept encrypted in the data directory; its public half is served at `RPM-GPG-KEY` (and `repodata/repomd.xml.key`).
The new repodata appears at once (old and new files never mix); the previous generation's files stay available for
clients that are mid-update. Until the first package is uploaded, `repodata/repomd.xml` answers `404`.

Write policy: *allow redeploy* replaces a package uploaded to the same path, *disable redeploy* refuses it (`409`),
*read only* refuses every upload and delete (`403`). The same name, epoch, version, release and architecture at a
different path is always refused (`409`): delete the old one first. Nothing can be uploaded under `repodata/`.

Client configuration (the repository's **Set me up** tab shows it filled in):

```ini
[myrpms]
name=myrpms (Waystation)
baseurl=https://registry.example.com/repository/myrpms/
enabled=1
repo_gpgcheck=1
gpgcheck=0
gpgkey=https://registry.example.com/repository/myrpms/RPM-GPG-KEY
username=alice
password=<your-api-token>
```

`repo_gpgcheck=1` makes dnf verify `repomd.xml` against Waystation's signature; dnf asks to import the key on first
use (compare the fingerprint: `curl -fsS https://registry.example.com/repository/myrpms/RPM-GPG-KEY | gpg --show-keys`).
Waystation does not sign packages: keep `gpgcheck=0` for unsigned builds, or sign them yourself (`rpm --addsign`)
and set `gpgcheck=1` with your key added to `gpgkey`. After an upload, `dnf makecache --refresh` (or
`dnf --refresh upgrade`) sees it.

## Browse

The repository's **Browse** tab lists the packages of every `primary.xml` Waystation has cached (all directories:
releases, BaseOS/AppStream, architectures), with the newest `epoch:version-release` (RPM version order) and the
architectures; a package shows its files newest first with directory, checksum, size and whether the file is
cached. *Files* switches to the folder tree of everything cached. A package fetched before any listing named it
(unverified) shows only in the folder tree. The API is `GET /api/v1/repositories/<name>/yum/packages` and
`/yum/versions?package=<name>`.

## Authentication

Repositories anonymous users may read need no client configuration. Otherwise (hosted repositories, or when an
administrator removes anonymous access) dnf authenticates with HTTP basic auth: your username and an API token
(Profile → API tokens, scope `read`; `write` to upload) as the password. Add to each section and keep the file
`chmod 600`:

```ini
username=alice
password=<your-api-token>
```

A revoked or expired token answers `401` (`dnf makecache` fails with "Status code: 401"). The harness checks both.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Curl error (37): Couldn't read a file:// file` / `GPG key retrieval failed` | The `gpgkey` path does not exist on this machine (wrong distribution or `$releasever`). Use the key of the distribution you proxy, e.g. `ls /etc/pki/rpm-gpg/`. |
| `repomd.xml GPG signature verification error` | `repo_gpgcheck=1` with a key that did not sign this repository's metadata (some vendors only sign packages). Use the right key or set `repo_gpgcheck=0`; `gpgcheck=1` still verifies every package. |
| `404` for `repodata/repomd.xml` | The `baseurl` path does not exist below the repository's upstream URL (check `$releasever`, the repository name's case, `os/`), or the upstream URL is wrong. |
| `404` for a `repodata/...` file | The cached `repomd.xml` does not list it and its name carries no checksum, so it cannot be verified. Run `dnf clean metadata` and retry. |
| `Downloading successful, but checksum doesn't match` | Should not happen through Waystation (repodata always matches the served `repomd.xml`). If it does, the upstream changed files mid-update: run `dnf makecache` again; persistent failures are a bug worth reporting with the repository's activity log. |
| `502 Bad Gateway` for `repomd.xml` with *Verify repomd.xml with these keys* set | The upstream's `repomd.xml.asc` is missing or was not made by one of the configured keys (wrong distribution's key, a key rotated upstream, an upstream that does not sign `repomd.xml`). The server log says which. Paste the right key or clear the option. |
| `502 Bad Gateway` | The upstream answered an error, a repodata file failed verification twice (the upstream's `repomd.xml` and files disagree), a package did not match its `primary.xml` entry, or `repomd.xml` was malformed. The repository's upstream health card shows the last error. |
| `504 Gateway Timeout` | The upstream did not answer in time. A stale cached `repomd.xml` is served instead when one exists and *stale on error* is on. |
| Updates appear late | The cached `repomd.xml` is served until it is older than the metadata max age (default 30 minutes), and dnf keeps its own copy for `metadata_expire`. Lower either if needed. |
| `401` | Anonymous access was removed for this repository: add credentials (above). |
| Hosted: `repomd.xml` `404` | Nothing has been uploaded yet, or the worker has not built the repodata yet (seconds; check the Tasks page for `index.yum`). |
| Hosted: `repomd.xml GPG signature verification error` | The key dnf imported is not this repository's (for example a repository deleted and created again under the same name has a new key): `rpm -e gpg-pubkey-<id>` for the old one and refresh. |
| Hosted upload `409` | The same package is already at another path, or the write policy refuses replacing it. |
| Repository configured with `mirrorlist=` or `metalink=` | Not supported: Waystation needs a `baseurl`, and the repository's upstream must be a concrete mirror URL. |
