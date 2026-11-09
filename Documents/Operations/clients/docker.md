# Docker / OCI: client guide

A docker proxy repository pulls images from an upstream registry (Docker Hub, GHCR, Quay, any OCI registry) and
caches them. A docker hosted repository stores the images and OCI artifacts you push (see "Hosted repositories").
Tested clients (`tests/clients/docker/`): the Docker Engine daemon (`registry-mirrors`, prefix and alias pulls,
`docker push`, `docker buildx --push` multi-platform), skopeo, crane, oras and cosign, plus the OCI
distribution-spec conformance suite against a hosted repository. Protocol notes: FORMATS.md "Docker / OCI".

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
skopeo and crane are tested; podman is not yet covered by the client harness.

## Hosted repositories

Create one in the UI (Repositories → Create → Docker / OCI → Hosted) or the API:

```json
{"name": "docker-hosted", "format": "docker", "type": "hosted", "online": true,
 "hosted": {"write_policy": "allow_redeploy", "immutable_tag_patterns": ["v[0-9]+(\\.[0-9]+)*"]}}
```

| Setting | Meaning |
|---|---|
| Write policy (`write_policy`) | `allow_redeploy` (default): tags may move and be deleted; `disable_redeploy`: no tag ever moves or is deleted by a client; `read_only`: no pushes or deletes at all |
| Immutable tags (`immutable_tag_patterns`) | Regular expressions matched against the whole tag: a matching tag never moves or goes away once pushed (`v1.2.3` stays, `latest` moves) |
| Default repository (`default_repo`) | Names without a repository prefix are pushed and pulled here (only one docker repository can be the default) |

Images are addressed by repository prefix, like proxies: `registry.example.com/docker-hosted/team/app:1.0`. Pushing
needs `write` on the repository (the `deployer` role, or a custom grant); deleting needs `delete`. Sign in with an
API token that has the `write` (and `delete`) scope:

```sh
echo "$WAYSTATION_TOKEN" | docker login registry.example.com --username ci --password-stdin
docker tag app:1.0 registry.example.com/docker-hosted/team/app:1.0
docker push registry.example.com/docker-hosted/team/app:1.0
docker buildx build --platform linux/amd64,linux/arm64 -t registry.example.com/docker-hosted/team/app:1.1 --push .
skopeo copy --all docker://registry.example.com/team/app:1.0 docker://registry.example.com/docker-hosted/team/app:1.0
crane copy docker.io/library/alpine:3.22 registry.example.com/docker-hosted/base/alpine:3.22
oras push registry.example.com/docker-hosted/docs:1.0 README.md:text/markdown
oras attach --artifact-type application/spdx+json registry.example.com/docker-hosted/team/app@sha256:<digest> sbom.json
cosign sign --key cosign.key registry.example.com/docker-hosted/team/app@sha256:<digest>
cosign verify --key cosign.pub registry.example.com/docker-hosted/team/app:1.0
```

What happens on a push:

- Blobs are streamed to disk and verified against their sha256 before they are stored; uploads may be monolithic,
  chunked or resumed after an interruption (the client asks for the upload status and continues). Identical content
  is stored once across every repository. A layer the client may already read in another docker repository (a proxy
  cache included) is mounted instead of uploaded again (`crane copy` and `docker push` do this).
- A manifest is accepted only when every blob it names is in the repository with the size it states, and every
  manifest an index names was pushed first (clients always push in that order). Media types are checked, the
  manifest is limited to 4 MiB and is stored byte for byte (its digest never changes).
- A manifest with a `subject` (cosign 3 signatures, `oras attach`, SBOMs, attestations) is listed by the referrers
  API (`GET /v2/<name>/referrers/<digest>`, with the `artifactType` filter), so clients find it without the
  `sha256-<digest>` tag workaround.
- Every pushed manifest and every delete is in the audit log (`content.upload` / `content.delete`) and the activity
  feed; layer uploads show as live upload transfers on the dashboard. The Browse tab shows pushed images, tags and
  platforms like a proxy's.
- Deletes: a tag (`DELETE /v2/<name>/manifests/<tag>`), a manifest by digest (with its tags; refused while an
  immutable tag points at it) and a blob no manifest of the repository uses. Disk space comes back with blob GC.
- `GET /v2/_catalog` lists `<repository>/<image>` for every tagged image of the docker repositories you can see.

## Group repositories

A group puts several docker repositories behind one name: `registry.example.com/docker-all/team/app:1.0` is looked
up in each member in order. Create one in the UI (Repositories → Create → Docker / OCI → Group) or the API:

```json
{"name": "docker-all", "format": "docker", "type": "group", "online": true,
 "group": {"members": ["docker-hosted", "docker-hub", "ghcr"], "writable_member": "docker-hosted"}}
```

- Put hosted repositories first: a name missing there is then asked of the proxies' upstreams in order.
- Blobs are found in any member's stored or cached copy before an upstream is asked; checking whether a layer exists
  (`HEAD`, as `docker push` does) never goes upstream.
- `tags/list` is the union of the members' tags (a proxy contributes its upstream's first page).
- `docker push` to the group stores the image in the writable member; you need `write` on the group and on that
  member. Without a writable member the group is read-only (`unsupported`). Deletes through the group act on the
  writable member only.
- Reading through the group needs `read` on the group; members' content is served through it.
- A repository that is a group member cannot be deleted until it is removed from the group.
- Answers name the member that served them in the `X-Ws-Group-Member` header.

## Authentication

Proxy repositories are readable anonymously by default. Clients get an anonymous pull token from Waystation's token
service (`/auth/token`, announced in the `WWW-Authenticate` challenge of `/v2/`) automatically; no `docker login`
is needed. When an administrator removes anonymous access, sign in with an API token as the password (any
username). Create an API token on your profile page (avatar menu → Profile → API tokens; shown once, copy it then) or, for
CI, on a service account. Give it only the `read` scope and, ideally, restrict it to the repositories the client
needs: a token only ever narrows what its owner may do. A revoked or expired token is refused at once (401); it
never falls back to anonymous access.

```sh
echo "$WAYSTATION_TOKEN" | docker login registry.example.com --username alice --password-stdin
```

The registry tokens Docker then fetches carry the API token's id and the API token is re-checked on every request,
so revoking it stops pulls at once, not when the 5-minute registry token expires. `docker login` with your account
password also works (local accounts); prefer a token for CI.

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
| `unsupported` / `405` on push | Proxy repositories are read-only; push to a hosted docker repository. |
| `denied: tag ... is immutable in this repository` | The tag matches an immutable pattern (or the repository disables redeploy): push a new tag. |
| `denied: this repository is read-only` | The hosted repository's write policy is `read_only`. |
| `unsupported: this group has no writable member` | Push to a hosted repository, or give the group a writable member. |
| `denied: write permission required on <member>` when pushing to a group | You may write to the group but not to its writable member: ask for `write` on the member. |
| `unauthorized` / `insufficient_scope` on push | Your token or account has no `write` on the repository: use an API token with the `write` scope and a role that may write there. |
| `manifest blob unknown` on push | The manifest names a blob the repository does not hold (an interrupted push): push again; the client re-uploads what is missing. |
| `docker search`, referrers on proxies | Not available; proxies answer the referrers API with 404 and clients fall back to the tag schema. |
