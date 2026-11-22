# Swift Package Registry: client guide

A Swift hosted repository is a package registry (SE-0292) you publish your own packages to with
`swift package-registry publish` (SE-0391). A Swift proxy repository is **SCM-backed**: there is no public Swift
registry to cache, so it serves packages straight from a Git host (GitHub, GitLab, Gitea/Forgejo): a package's
releases are its repository's semantic-version tags, and each release's source archive is built from the tag the first
time a client asks for it, then pinned. Tested with Swift 6.4 on Linux (`tests/clients/swift/`); Swift 5.9 and later
speak the same API. Protocol notes: FORMATS.md "Swift Package Registry".

Examples use `https://registry.example.com` as the Waystation base URL.

## Create the repository

In the UI (Repositories → Create → Swift) or the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: the Git host itself, e.g. `https://github.com/` or `https://gitlab.com/` (presets). Git host type (`archive_layout`): `github`, `gitlab` or `gitea` (Gitea and Forgejo), which says where the host serves a tag's tarball. Scope mapping (`scopes`): scope → owner path on the host, e.g. `pointfree = pointfreeco` or a GitLab group path; unlisted scopes are the owner name itself. `mapped_scopes_only`: serve only the listed scopes (an allowlist of owners). Upstream credentials (basic or bearer) are sent to the Git host for private repositories. |
| Hosted | Write policy: `allow_redeploy` or `disable_redeploy` (either way a published version can never be replaced), `read_only` (publishing and removing answer `403`). |

Package identities are `<scope>.<name>` and compare case-insensitively. For a proxy, `apple.swift-argument-parser`
is the Git repository `https://github.com/apple/swift-argument-parser`. A repository whose name contains a `.` cannot
be served (registry names allow letters, digits, `-` and `_` only).

## Client configuration

SwiftPM talks to registries over **HTTPS only**: Waystation must be served over TLS (directly or behind a reverse
proxy, Documents/Operations/REVERSE_PROXIES.md).

```sh
# per project (.swiftpm/configuration/registries.json)
swift package-registry set https://registry.example.com/repository/swift/
# only for one scope (several registries side by side)
swift package-registry set --scope mona https://registry.example.com/repository/swift-hosted/
# for every project on this machine (~/.swiftpm/configuration/registries.json)
swift package-registry set --global https://registry.example.com/repository/swift/
```

Depend on packages by identity in `Package.swift`:

```swift
dependencies: [
    .package(id: "apple.swift-argument-parser", from: "1.5.0"),
],
targets: [
    .executableTarget(name: "Tool", dependencies: [.product(name: "ArgumentParser", package: "apple.swift-argument-parser")]),
]
```

Or keep `url:` dependencies and let the registry serve them where it knows the URL:

```sh
swift package resolve --replace-scm-with-registry   # also: swift build --replace-scm-with-registry
```

SwiftPM then asks the registry which identity a Git URL has (`GET <registry>/identifiers?url=...`): a proxy answers
for repositories on its Git host (`https://github.com/apple/swift-argument-parser` → `apple.swift-argument-parser`),
a hosted registry for the `repositoryURLs` its releases were published with. Other URLs are still cloned with git.

### Signing in

Hosted repositories are not readable anonymously by default, and publishing always needs credentials. Sign in with
an API token (Account → API tokens); SwiftPM sends it as `Authorization: Bearer <token>`:

```sh
swift package-registry login https://registry.example.com/repository/swift-hosted/ --token <your-api-token> --no-confirm
# or with a password
swift package-registry login https://registry.example.com/repository/swift-hosted/ --username alice --password '...'
```

The login URL is the repository URL (or `<repository URL>login`); SwiftPM `POST`s to it and Waystation answers `200`
for valid credentials. On Linux the credentials go to `~/.netrc` (`--no-confirm` skips the question), on macOS to the
keychain; SwiftPM sends them to every registry on that host.

### Publishing and removing releases (hosted)

```sh
swift package-registry publish mona.LinkedList 1.0.0 --url https://registry.example.com/repository/swift-hosted/
# with release metadata (package-metadata.json in the package directory is picked up by default)
swift package-registry publish mona.LinkedList 1.0.1 --url https://registry.example.com/repository/swift-hosted/ \
  --metadata-path ./metadata.json
# signed (CMS, cms-1.0.0)
swift package-registry publish mona.LinkedList 1.0.2 --url https://registry.example.com/repository/swift-hosted/ \
  --private-key-path key.der --cert-chain-paths cert.der

# remove a release
curl -fS -X DELETE -H "Authorization: Bearer <your-api-token>" \
  https://registry.example.com/repository/swift-hosted/mona/LinkedList/1.0.0
```

Publishing needs the `write` permission, removing `delete`. Release metadata follows the registry specification's
appendix B (`description`, `repositoryURLs`, `licenseURL`, `readmeURL`, `author`, `originalPublicationTime`).
**Releases are immutable**: publishing a version that exists answers `409`, and a removed release keeps its version:
clients see it as gone (`410`, and a `problem` in the release list) and it cannot be published again.

The repository's **Set me up** tab shows these commands with your repository URL filled in.

## How it behaves

- **Wire format**: every answer carries `Content-Version: 1`; errors are problem details
  (`application/problem+json`). An `Accept` header asking for another API version is refused (`415`; `400` for a
  malformed one). Release lists come highest version first with a `latest-version` link (and `canonical`: the source
  repository); a release carries `latest-version`/`successor-version`/`predecessor-version` links, its
  `source-archive` checksum (and signature) and `publishedAt`. Manifests are served with `Link` alternates for every
  `Package@swift-<version>.swift` in the archive; `?swift-version=<v>` answers that file or `303` to `Package.swift`.
  Archives carry `Digest`, `Content-Disposition: attachment; filename="<name>-<version>.zip"` and, when signed,
  `X-Swift-Package-Signature-Format`/`X-Swift-Package-Signature`. Files are served by nginx (X-Accel), with ranges
  and conditional requests.
- **Proxy (SCM-backed)**: a package's tags come from the Git host's smart-HTTP ref advertisement
  (`<repo>.git/info/refs?service=git-upload-pack`), cached as metadata and revalidated after the metadata max age; tags
  that are versions (`1.2.3`, `v1.2.3`, `1.2`) are releases (`1.2.3` wins over `v1.2.3` when both exist). The first
  request for a release's information, manifest or archive downloads the tag's tarball (cached, at most 256 MiB, SSRF
  guard on the host and its redirects, e.g. GitHub's `codeload.github.com`) and repackages it into the zip clients
  download, in a server thread: the tarball's top folder becomes `<name>/`, entries sorted, fixed timestamps and
  permissions, fixed compression, so the same tag always gives the same bytes. Unsafe entries are refused (absolute
  paths, `..`, links leaving the package); at most 50,000 entries and 512 MiB unpacked. A tag without a `Package.swift`
  is not a release (`404`).
- **Pinned checksums**: the archive's checksum is recorded the first time it is built and never changes: clients'
  fingerprints (`~/.swiftpm/security/fingerprints`) stay valid even if the tag is later moved upstream. Release files
  are never removed by cleanup policies (only the cached upstream tarballs under `_scm/` are). If the files had to be
  rebuilt and the tag now gives different bytes, Waystation refuses (`502`) instead of serving them.
- **Hosted publishing**: the archive (at most 512 MiB; a hard quota is checked first) is checked before it is
  stored: a readable zip, one top-level folder, no encrypted entries, safe entry names, no duplicate names, no link
  leaving the package, bounded size and count, and a `Package.swift` whose first line declares a
  `swift-tools-version` (`422` otherwise, with the reason). The checksum is the sha256 of the bytes you uploaded. A
  signature is recorded (its signer's certificate subject is shown in Browse) and passed to clients, which verify it;
  Waystation does not verify signatures. Publishing and removals are audited (`content.upload`, `content.delete`).
- **Browse**: scopes → packages → releases (SemVer order, newest first) with the tools version, publish time and
  publisher, size, checksum, signature and, for a proxy, the tag and commit the archive was built from. A proxy lists
  the releases clients have asked for (each was pinned then). Global search finds packages by `scope.name`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `invalid registry URL ... must be HTTPS` | SwiftPM accepts only `https://` registries: serve Waystation over TLS. |
| `failed fetching ... 401` / resolve fails without credentials | The repository is not readable anonymously: `swift package-registry login <repository URL> --token ...`. Check `~/.netrc` (Linux) has an entry for the Waystation host. |
| `swift package-registry login` fails | A wrong token or password, or a token without access to that repository (`404`: it answers like a missing one). |
| Publishing answers 409 | The version exists or was removed: versions are immutable, publish a new version. |
| Publishing answers 422 | The archive or metadata was refused; the message says why (no `Package.swift` at the package root, no `swift-tools-version` line, an unsafe entry, invalid metadata JSON, a signature without `X-Swift-Package-Signature-Format: cms-1.0.0`). |
| A proxy package is not found | The scope is not served (`mapped_scopes_only`), the Git repository does not exist or is private (configure upstream credentials), or its name contains a `.`. |
| A proxy release answers 404 "not a Swift package" | The tag has no `Package.swift` at the repository root. |
| A proxy release answers 502 "does not match its pinned checksum" | Its files were lost and the tag now has different contents upstream; Waystation will not serve different bytes for a pinned release. Restore the blob store from a backup, or ask an administrator to remove the release record deliberately. |
| `checksum mismatch` / fingerprint errors in SwiftPM | The client has a fingerprint for the same identity from another registry. Remove the entry under `~/.swiftpm/security/fingerprints`, or use one registry per scope. |
| `--replace-scm-with-registry` still clones | The URL is not on the proxy's Git host (or not a published `repositoryURLs` entry of a hosted release), so the registry has no identity for it. |
