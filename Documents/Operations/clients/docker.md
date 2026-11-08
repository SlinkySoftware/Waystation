# Docker / OCI: client guide

A docker proxy repository pulls images from an upstream registry (Docker Hub, GHCR, Quay, any OCI registry) and
caches them. Tested clients: the Docker Engine daemon (`registry-mirrors`, prefix and alias pulls) and skopeo
(`tests/clients/docker/`). Protocol notes: FORMATS.md "Docker / OCI".

Examples use `registry.example.com` as the Waystation host (base URL `https://registry.example.com`), a Docker Hub
repository named `docker-hub` and a GHCR repository named `ghcr`.

## Create the repositories

In the UI (Repositories → Create → Docker / OCI → Proxy) or the API:

| Setting | Docker Hub | GHCR / other OCI registries |
|---|---|---|
| Upstream URL | `https://registry-1.docker.io` | `https://ghcr.io`, `https://quay.io`, ... |
| Docker Hub (`docker_hub`) | on: token service `auth.docker.io`, `library/` for one-segment names, rate-limit tracking | off: the upstream's `WWW-Authenticate` challenge decides |
| Aliases (`aliases`) | `docker.io` | `ghcr.io` |
| Default repository (`default_repo`) | on: receives mirror-mode pulls and names without a repository prefix | off |
| Upstream credentials | optional Docker Hub username + access token (higher pull limits) | optional, e.g. a GHCR username + PAT with `read:packages` for private images |

Only one repository can be the default. Aliases are unique across repositories and cannot equal a repository name.

## How names are routed

`/v2/<first segment>/...`: when the first segment is a repository name it selects that repository; else when it is
an alias (`docker.io`, `ghcr.io`) it selects the repository that declares it; else the whole name goes to the default
repository. The same API is also served under `/repository/<repo>/v2/...` (Nexus-style paths).

| Client asks for | Repository | Upstream image |
|---|---|---|
| `registry.example.com/docker-hub/nginx:1.27` | `docker-hub` | `library/nginx` |
| `registry.example.com/docker.io/library/nginx:1.27` | `docker-hub` (alias) | `library/nginx` |
| `registry.example.com/library/nginx:1.27` (and mirror mode) | default repository | `library/nginx` |
| `registry.example.com/ghcr/owner/app:1.0` | `ghcr` | `owner/app` |
| `registry.example.com/ghcr.io/owner/app:1.0` | `ghcr` (alias) | `owner/app` |

## Docker Engine

### Mirror mode (Docker Hub images, no image name changes)

`/etc/docker/daemon.json`, then restart the daemon (`systemctl restart docker`):

```json
{
  "registry-mirrors": ["https://registry.example.com"]
}
```

`docker pull nginx` now asks `https://registry.example.com/v2/library/nginx/...`, which the default repository
serves. The daemon only uses mirrors for Docker Hub images, and silently falls back to Docker Hub when the mirror
fails: check `docker info` (Registry Mirrors) and the repository's live transfers to see that pulls go through
Waystation.

### Prefix and alias pulls (any upstream)

```sh
docker pull registry.example.com/docker-hub/nginx:1.27
docker pull registry.example.com/docker.io/library/nginx:1.27
docker pull registry.example.com/ghcr.io/owner/app:1.0
```

The repository's **Set me up** tab shows these with your host filled in.

A Waystation reached over plain HTTP (test setups only) must be listed in `"insecure-registries":
["registry.example.com:8080"]`; the mirror URL is then `http://registry.example.com:8080`.

## containerd / nerdctl / Kubernetes nodes

Mirror a registry with `hosts.toml` (`/etc/containerd/certs.d/<upstream host>/hosts.toml`; containerd's
`config_path` must point at `/etc/containerd/certs.d`):

```toml
# /etc/containerd/certs.d/docker.io/hosts.toml
server = "https://registry-1.docker.io"

[host."https://registry.example.com/v2/docker-hub"]
  capabilities = ["pull", "resolve"]
  override_path = true
```

`override_path = true` makes containerd append `/<image>/manifests/...` to that path, so each upstream host maps to
its own repository (use a `ghcr.io` directory and `/v2/ghcr` for GHCR). Without `override_path`, use
`[host."https://registry.example.com"]` for the default repository only. Not yet covered by the client harness.

## skopeo, crane, podman

```sh
skopeo inspect docker://registry.example.com/docker-hub/library/nginx:1.27
skopeo copy docker://registry.example.com/ghcr.io/owner/app:1.0 oci:/tmp/app:1.0
podman pull registry.example.com/docker-hub/library/nginx:1.27
crane digest registry.example.com/docker-hub/library/nginx:1.27
```

podman can also use a mirror for `docker.io` in `/etc/containers/registries.conf`
(`[[registry]] location = "docker.io"` with `[[registry.mirror]] location = "registry.example.com/docker-hub"`).
skopeo is tested; podman and crane are not yet covered by the client harness.

## Authentication

M0: proxy repositories are readable anonymously. Clients get an anonymous pull token from Waystation's token
service (`/auth/token`, announced in the `WWW-Authenticate` challenge of `/v2/`) automatically; no `docker login`
is needed. When an administrator removes anonymous access, sign in with an API token once they land (T-S04):

```sh
echo "<your-api-token>" | docker login registry.example.com --username alice --password-stdin
```

Upstream credentials (Docker Hub account, GHCR PAT) are set on the repository, never on the clients. They are sent
only to the upstream registry and its token service, never to blob CDNs the upstream redirects to.

## What Waystation does with the upstream

- Tags are metadata: a tag is re-checked with the upstream after the repository's metadata max age, first with a
  `HEAD` (same `Docker-Content-Digest` = unchanged; Docker Hub does not count `HEAD`s against the pull limit).
- Manifests by digest and blobs are content: verified against their digest before they are cached, kept forever
  (subject to cleanup policies), shared between images of the same repository.
- Manifests are never converted: the client's `Accept` list goes upstream, and the bytes and digest are served as
  the upstream sent them.
- `docker pull` answers carry `Docker-Content-Digest`, the manifest media type and
  `Docker-Distribution-API-Version: registry/2.0`; `X-Ws-Cache` tells `FETCHING`, `HIT` or `REVALIDATED`.
- Docker Hub's `ratelimit-limit` / `ratelimit-remaining` headers appear on the repository's upstream health card.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `manifest unknown` | The tag does not exist upstream (or not for your platform), or a recent 404 is still in the negative cache: wait for the negative cache TTL. |
| `name unknown` / `repository not found upstream, or the upstream denied access` | Docker Hub answers "unauthorized" for names that do not exist and for private ones. Check the name (`library/` is added only for one-segment names on a Docker Hub repository) and the repository's upstream credentials. |
| `toomanyrequests` | Docker Hub's anonymous pull limit was reached. Add Docker Hub credentials to the repository; already cached images keep working. |
| `502` / `unknown` with "digest" in the message | The upstream sent bytes that do not match the requested digest; nothing was cached. Retry; if it persists the upstream (or something between) is corrupting content. |
| Mirror-mode pulls never show up in Waystation | The daemon fell back to Docker Hub: check `daemon.json` (valid JSON, restart done), that `https://registry.example.com/v2/` answers, and that a default docker repository exists. |
| `http: server gave HTTP response to HTTPS client` | Waystation is reached over plain HTTP: add it to `insecure-registries`, or put it behind TLS. |
| `x509: certificate signed by unknown authority` | Waystation's TLS certificate is from a private CA: install the CA on the client (`/etc/docker/certs.d/registry.example.com/ca.crt` for Docker). |
| `unsupported` / `405` on push | Proxy repositories are read-only; push to a hosted docker repository (not available yet). |
| `docker search`, `_catalog`, referrers | Not available for proxy repositories yet; clients fall back to the tag schema for signatures/SBOMs. |
