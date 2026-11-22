# Client guides

One guide per package format: how to point each client tool at Waystation, how to authenticate, how to publish, and
what to check when something fails. Every guide has the same sections: Overview, Repository types, Client setup,
Authentication, Publishing, Limitations and Troubleshooting. Each repository's **Set me up** tab in the UI shows the
same snippets with your real URL and repository name filled in.

## Conventions

- **Base URL.** The examples use `https://waystation.example.com` (and `registry.example.com` for Docker). Use the
  external URL of your deployment: the address clients reach, normally through a TLS-terminating reverse proxy. See
  [DEPLOYMENT.md](../DEPLOYMENT.md) and [REVERSE_PROXIES.md](../REVERSE_PROXIES.md).
- **Repository URLs.** Every repository answers at `https://waystation.example.com/repository/<repo>/...`, the same
  layout Nexus Repository uses, so migrated clients keep their URLs. A few formats also have protocol-specific entry
  points (Docker's `/v2/` and dedicated ports, Terraform's service discovery); their guides describe them.
- **Repository types.** A **proxy** caches an upstream (Docker Hub, npmjs, PyPI, ...). A **hosted** repository
  stores your own uploads. A **group** serves several repositories of the same format under one URL; uploads to a
  group go to its writable member where the format supports it.
- **Authentication.** Package clients authenticate with an **API token**. (`docker login` and `npm login` also
  accept a local or LDAP account password; use a token for CI.) Create one on your profile page (avatar menu, Profile, API
  tokens) or, for CI, on a service account. Give it only the scopes it needs (`read`, `write`, `delete`) and,
  ideally, restrict it to the repositories it needs. Most clients send the token with HTTP basic auth (any
  username, the token as the password) or as a bearer token; each guide shows how.
- **Anonymous access.** Proxies are readable anonymously when the deployment allows anonymous reads. A repository
  you may not see answers `404`; a request without credentials to a repository that anonymous users cannot read
  gets a `401` challenge, so clients know to send credentials.

## Formats

| Format | Clients | Proxy | Hosted | Group | Guide |
|---|---|---|---|---|---|
| Docker / OCI | docker, containerd / nerdctl, podman, skopeo, crane, oras, cosign | yes | yes | yes | [docker.md](docker.md) |
| npm | npm, pnpm, Yarn, Bun | yes | yes | yes | [npm.md](npm.md) |
| PyPI | pip, uv, Poetry, PDM, twine | yes | yes | yes | [pypi.md](pypi.md) |
| Maven | Maven, Gradle, sbt | yes | yes | yes | [maven.md](maven.md) |
| apt | apt (Debian, Ubuntu) | yes | yes | no | [apt.md](apt.md) |
| yum / dnf | dnf, yum | yes | yes | no | [yum.md](yum.md) |
| Alpine apk | apk | yes | yes | no | [apk.md](apk.md) |
| Raw | curl, wget, any HTTP client | yes | yes | yes | [raw.md](raw.md) |
| Go modules | go | yes | no | yes | [go.md](go.md) |
| Helm | helm, helm cm-push, curl | yes | yes | yes | [helm.md](helm.md) |
| Terraform / OpenTofu | terraform, tofu | yes | yes | no | [terraform.md](terraform.md) |
| Ansible Galaxy | ansible-galaxy (collections; roles through proxies only) | yes | yes | yes | [galaxy.md](galaxy.md) |
| Swift | SwiftPM (`swift package-registry`) | yes | yes | no | [swift.md](swift.md) |
| NuGet | dotnet CLI, NuGet.exe | yes | yes | yes | [nuget.md](nuget.md) |
| Cargo | cargo | yes | yes | yes | [cargo.md](cargo.md) |
| RubyGems | gem, Bundler | yes | yes | yes | [rubygems.md](rubygems.md) |
| Composer | Composer 2 | yes | yes | yes | [composer.md](composer.md) |
| Conda | conda, mamba, micromamba, pixi | yes | yes | no | [conda.md](conda.md) |
| Hugging Face | hf CLI, huggingface_hub, transformers, diffusers, datasets | yes | no | no | [huggingface.md](huggingface.md) |

Per-format protocol details (what is cached, how metadata is revalidated, what is verified) are in
[FORMATS.md](../../Architecture/FORMATS.md). Moving from Nexus Repository: see
[MIGRATING_FROM_NEXUS.md](../MIGRATING_FROM_NEXUS.md).
