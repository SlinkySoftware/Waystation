# Helm client guide

How to point helm at Waystation **Helm** repositories (classic chart repositories: `index.yaml` plus chart
packages) and how to upload charts. The repository's **Set me up** tab shows the same commands with your real URL
filled in. Examples below use `https://waystation.example.com` as the Waystation base URL, a repository called `charts`
(a proxy or group) and a hosted repository called `mine`.

## Overview

Waystation serves classic Helm chart repositories: an `index.yaml` and chart packages (`.tgz`) with optional
provenance files (`.tgz.prov`). Hosted repositories accept uploads with curl, the ChartMuseum API or
`helm cm-push`; a worker rebuilds their index. Proxy, hosted and group repositories are supported. Protocol notes:
[FORMATS.md](../../Architecture/FORMATS.md) ("Helm").

Every repository answers at `https://waystation.example.com/repository/<name>/` (the URL for `helm repo add`).

OCI charts (`helm push oci://...`, `helm install oci://...`) are not classic repositories: they live in **docker**
repositories (proxy, hosted or group) like any other OCI artifact; see the [Docker client guide](docker.md).

Tested with helm 3.19 and helm 4.0.

## Repository types

Create a repository in the UI (Repositories → Create → Helm) or through the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: the chart repository URL you would give `helm repo add`, e.g. `https://prometheus-community.github.io/helm-charts` (presets: Prometheus community, Jetstack, ingress-nginx). Its `index.yaml` is revalidated after the metadata max age. |
| Hosted | Write policy: `allow_redeploy` (default: uploading an existing chart version replaces it), `disable_redeploy` (a version is uploaded once; again answers `409`), `read_only` (uploads and deletes answer `403`). |
| Group | Members in order (Helm proxy and hosted repositories), optionally a writable member (a hosted member that receives uploads sent to the group). |

### How each type behaves

- **Proxy index**: chart URLs in the upstream `index.yaml` are rewritten to the repository. A chart under the
  upstream URL keeps its relative path (`charts/foo-1.0.0.tgz`); a chart hosted elsewhere (GitHub releases, a CDN)
  is served at `_ext/<scheme>/<host>/<path>` in the repository. Only charts the cached index lists can be fetched.
- **Proxy validation**: a chart is verified against the index's `digest` (sha256) while it downloads; a mismatch cuts
  the transfer and nothing is cached. Charts without a digest are stored as received.
- **Groups**: the merged index lists each chart version once, from the first member that has it. Chart downloads
  come from the first member that has the file.
- **Served indexes are JSON** (valid YAML). Helm 3.11+ parses JSON indexes directly; older Helm, Flux and Argo CD
  read them as YAML.

## Client setup

### helm

```sh
helm repo add charts https://waystation.example.com/repository/charts/
helm repo update charts
helm search repo charts/ --versions
helm install my-release charts/kube-prometheus-stack --version 79.5.0
helm pull charts/cert-manager --version v1.19.1
```

### helm-push plugin (cm-push)

Used only for uploads to hosted repositories; see "Publishing".

## Authentication

Proxy repositories are usually readable anonymously; hosted repositories are not readable anonymously by default.
Sign in with your user name and an API token (avatar menu → Profile → API tokens) as the password. Helm keeps the
credentials in its repository config and sends them only to this host (chart URLs in the served index are relative
to the repository):

```sh
helm repo add mine https://waystation.example.com/repository/mine/ --username <username> --password <your-api-token>
```

curl and the ChartMuseum API take the same credentials with HTTP basic auth (`-u <username>:<your-api-token>`). Without
credentials, a repository you cannot see answers the `401` Basic challenge (a missing one too); with credentials that
cannot read it, `404`.

## Publishing

Hosted repositories (and groups with a writable member) accept uploads:

```sh
helm package ./mychart                                  # mychart-0.1.0.tgz

# curl (PUT): the chart is stored as <name>-<version>.tgz from its Chart.yaml, whatever the URL's file name
curl -fS -u <username>:<your-api-token> --upload-file mychart-0.1.0.tgz https://waystation.example.com/repository/mine/

# ChartMuseum API (POST multipart, fields "chart" and optional "prov"), what helm cm-push sends
curl -fS -u <username>:<your-api-token> -F chart=@mychart-0.1.0.tgz https://waystation.example.com/repository/mine/api/charts

# the helm-push plugin: point its context path at the repository
helm plugin install https://github.com/chartmuseum/helm-push
helm cm-push mychart-0.1.0.tgz mine --context-path=/repository/mine

# provenance file next to an uploaded chart (helm package --sign)
curl -fS -u <username>:<your-api-token> --upload-file mychart-0.1.0.tgz.prov \
  https://waystation.example.com/repository/mine/mychart-0.1.0.tgz.prov

# delete a version (its provenance file goes too)
curl -fS -u <username>:<your-api-token> -X DELETE https://waystation.example.com/repository/mine/mychart-0.1.0.tgz
curl -fS -u <username>:<your-api-token> -X DELETE https://waystation.example.com/repository/mine/api/charts/mychart/0.1.0
```

Uploads need the `write` permission, deletes `delete`. The index is rebuilt by a worker within a few seconds of an
upload or delete; run `helm repo update` after that. Uploads to a group with a writable member land in that member.

Uploads are checked before they are stored: a gzip tar archive with one top-level directory, no links, absolute
paths or `..`, a `Chart.yaml` of at most 1 MiB with `apiVersion` v1/v2, a valid name and a SemVer version; chart
packages up to 64 MiB.

## Limitations

- OCI charts are served by docker repositories, not Helm repositories.
- A proxy serves only charts its cached upstream index lists.
- Proxy charts without a `digest` in the upstream index are stored unverified.
- A hosted repository's index is rebuilt asynchronously, so a new chart appears a few seconds after the upload.
- Chart packages are limited to 64 MiB and `Chart.yaml` to 1 MiB.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `helm repo add` fails with 401 or 404 | The repository is not readable anonymously: add `--username` and `--password` (an API token). |
| A just-uploaded chart is missing from `helm search` | The index is rebuilt asynchronously: wait a few seconds and run `helm repo update <name>`. If it never appears, check Tasks for failed `index.helm` runs. |
| Upload answers 400 | The file is not a valid chart package (see "Publishing"); the message names the problem. |
| Upload answers 409 | The repository has `disable_redeploy` and the chart version exists; bump the chart version. |
| `helm pull` from a proxy answers 404 | The chart version is not in the upstream index (the index is fetched again once before answering 404). |
| `helm pull` from a proxy is cut off | The upstream file does not match the index digest; Waystation refuses it. Try again later or report it upstream. |
