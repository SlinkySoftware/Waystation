# yum / dnf (RHEL family, Rocky Linux, AlmaLinux, Oracle Linux, EPEL, Fedora): client guide

A yum proxy repository caches an RPM repository tree (`dl.rockylinux.org/pub/rocky`, `repo.almalinux.org/almalinux`,
`yum.oracle.com`, `dl.fedoraproject.org/pub/epel`, a vendor repository). The upstream's layout is kept below the
repository, so one Waystation repository serves every yum repository under its upstream URL (`BaseOS`, `AppStream`,
`extras`, every `$releasever` and `$basearch`). Tested with dnf on Rocky Linux 10 and AlmaLinux 10
(`tests/clients/yum/`). Protocol notes: FORMATS.md "yum / dnf".

Examples use `https://registry.example.com` as the Waystation base URL.

## Create the repositories

One repository per upstream tree:

| Repository | Upstream URL |
|---|---|
| `rocky` | `https://dl.rockylinux.org/pub/rocky/` |
| `almalinux` | `https://repo.almalinux.org/almalinux/` |
| `oracle` | `https://yum.oracle.com/` |
| `epel` | `https://dl.fedoraproject.org/pub/epel/` |

**Use a concrete base URL.** Mirrorlist and metalink URLs (`mirrors.rockylinux.org/mirrorlist?...`,
`mirrors.fedoraproject.org/metalink?...`) are not proxied: point the repository at one mirror's tree instead.

Options (all optional):

- *Repository path* (`repo_path`): the path of the yum repository below the upstream URL, the directory holding
  `repodata/` (e.g. `10/extras/$basearch/os/`). Only the "Set me up" snippet and "test upstream" use it. Leave it
  empty for the well-known upstreams above: the snippet then lists their usual repositories (BaseOS, AppStream,
  extras; Oracle BaseOS/AppStream; EPEL Everything). For any other upstream an empty path means the upstream URL is
  itself a yum repository.
- *GPG key* (`gpg_key`): the `gpgkey` the snippet uses. Empty: the distribution's key in `/etc/pki/rpm-gpg/` for the
  well-known upstreams, else a placeholder to replace.

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
  one that is not a well-formed `repomd.xml` (or carries a DTD) is refused with `502` and not cached.
- The files `repomd.xml` lists (`primary`, `filelists`, `other`, `updateinfo`, `modules`, `comps`, in any of `.gz`,
  `.xz`, `.zst`, `.bz2` or plain) are always the ones the `repomd.xml` your client received lists, checked against
  its checksum (SHA-1, SHA-256 or SHA-512) and size. If the upstream changed a file under its `repomd.xml`,
  Waystation fetches `repomd.xml` again once, then fails rather than serve a mismatched set. Files with the checksum
  in their name (the createrepo default) are cached forever.
- Packages (`.rpm`) are checked against the checksum and size of their entry in the repository's `primary.xml`.
  A package requested before Waystation has its `primary.xml` makes Waystation fetch and index that first; a package
  no cached `primary.xml` lists is served and recorded as *unverified*, and when a listing arrives and disagrees the
  cached copy is dropped and fetched again on the next request.

## Authentication

M0: proxy repositories are readable anonymously, so no client configuration is needed. When an administrator
removes anonymous access, dnf authenticates with HTTP basic auth using a username and an API token once API tokens
land (T-S04): add to each section (and keep the file `chmod 600`):

```ini
username=alice
password=<your-api-token>
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Curl error (37): Couldn't read a file:// file` / `GPG key retrieval failed` | The `gpgkey` path does not exist on this machine (wrong distribution or `$releasever`). Use the key of the distribution you proxy, e.g. `ls /etc/pki/rpm-gpg/`. |
| `repomd.xml GPG signature verification error` | `repo_gpgcheck=1` with a key that did not sign this repository's metadata (some vendors only sign packages). Use the right key or set `repo_gpgcheck=0`; `gpgcheck=1` still verifies every package. |
| `404` for `repodata/repomd.xml` | The `baseurl` path does not exist below the repository's upstream URL (check `$releasever`, the repository name's case, `os/`), or the upstream URL is wrong. |
| `404` for a `repodata/...` file | The cached `repomd.xml` does not list it and its name carries no checksum, so it cannot be verified. Run `dnf clean metadata` and retry. |
| `Downloading successful, but checksum doesn't match` | Should not happen through Waystation (repodata always matches the served `repomd.xml`). If it does, the upstream changed files mid-update: run `dnf makecache` again; persistent failures are a bug worth reporting with the repository's activity log. |
| `502 Bad Gateway` | The upstream answered an error, a repodata file failed verification twice (the upstream's `repomd.xml` and files disagree), a package did not match its `primary.xml` entry, or `repomd.xml` was malformed. The repository's upstream health card shows the last error. |
| `504 Gateway Timeout` | The upstream did not answer in time. A stale cached `repomd.xml` is served instead when one exists and *stale on error* is on. |
| Updates appear late | The cached `repomd.xml` is served until it is older than the metadata max age (default 30 minutes), and dnf keeps its own copy for `metadata_expire`. Lower either if needed. |
| `401` | Anonymous access was removed for this repository: add credentials (above). |
| Repository configured with `mirrorlist=` or `metalink=` | Not supported: Waystation needs a `baseurl`, and the repository's upstream must be a concrete mirror URL. |
