# Hugging Face client guide

How to point huggingface_hub, and everything built on it (transformers, diffusers, datasets, sentence-transformers,
the `hf` CLI), at a Waystation **huggingface** repository: a pull-through cache of the Hugging Face Hub. The
repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples below use
`https://waystation.example.com` and a repository called `huggingface` (a proxy of `https://huggingface.co`).

## Overview

A huggingface repository caches models and datasets from the Hugging Face Hub (or a Hub-compatible server). Clients
use `https://waystation.example.com/repository/<repo>` as their Hub endpoint (`HF_ENDPOINT`); the Hub API paths
(model and dataset info, file resolution) are served below it.

Tested with huggingface_hub 2.4 and hf_xet: `hf_hub_download` (a regular file and an LFS file), `hf download`,
`snapshot_download`, the same downloads from Waystation's cache with the repository offline, and a Waystation API
token as `HF_TOKEN`.

## Repository types

- **Proxy** (default upstream `https://huggingface.co`):
  - Files are cached by **commit**: `main` is resolved to its commit (re-checked after the repository's metadata max
    age), and a file is cached once per commit and path. A branch that moves never serves the previous commit's
    bytes.
  - Every file is verified before it is kept: LFS files (weights) against their SHA-256 and size, other files against
    their git blob id and size.
  - **Download hosts** (`cdn_hosts`, default `huggingface.co`, `*.huggingface.co`, `*.hf.co`): the Hub redirects
    weight downloads to its CDN; Waystation follows redirects only to these hosts (and the upstream URL's host). Add
    your mirror's CDN host if you point the repository at another Hub-compatible server.
- **Hosted**: not supported.
- **Group**: not supported.

### Prefetch

Proxy repositories can warm models on a schedule (repository form → Prefetch), one item per line:

```text
julien-c/dummy-unknown                     # every file of main
openai-community/gpt2@main:config.json     # one file
datasets/stanfordnlp/imdb@refs/convert/parquet
```

A whole revision is limited to 2,000 files and 200 GiB; name files for bigger repositories.

## Client setup

### Environment variables

```shell
export HF_ENDPOINT=https://waystation.example.com/repository/huggingface   # no trailing slash
```

PowerShell: `$env:HF_ENDPOINT = "https://waystation.example.com/repository/huggingface"`.

huggingface_hub reads `HF_ENDPOINT` when it is imported; set it in the environment (or before importing in Python).

### The hf CLI

```shell
hf download julien-c/dummy-unknown                                   # the whole repository (main)
hf download julien-c/dummy-unknown config.json --revision main       # one file
hf download stanfordnlp/imdb --repo-type dataset                      # a dataset
```

Before huggingface_hub 1.0 the command was `huggingface-cli download ...` (same arguments).

### Python (huggingface_hub, transformers, diffusers)

```python
from huggingface_hub import hf_hub_download, snapshot_download

config = hf_hub_download('julien-c/dummy-unknown', 'config.json')
folder = snapshot_download('julien-c/dummy-unknown', revision='main', ignore_patterns=['*.h5'])
```

transformers, diffusers and sentence-transformers use huggingface_hub, so `HF_ENDPOINT` is all they need:
`AutoModel.from_pretrained('openai-community/gpt2')` downloads through Waystation.

## Authentication

- **Anonymous** reads work when the repository allows them (the default for proxies).
- Otherwise set `HF_TOKEN` to a **Waystation API token** (Profile → API tokens, scope `read`). huggingface_hub sends
  it as a bearer token to Waystation only; it never reaches the Hub.
- **Gated and private models**: put a Hugging Face access token (with access to those repositories) on the
  Waystation repository as its upstream credential (Authentication: bearer). Waystation sends it to the Hub only,
  never to the CDN. Everyone allowed to read the Waystation repository can then download those models: use RBAC to
  limit who can.

## Publishing

Not supported. Hugging Face repositories in Waystation are proxies only: you cannot upload models or datasets to
Waystation through the Hub API.

## Limitations

- Proxy only: no hosted or group repositories, no uploads.
- Revisions whose name contains `/` (other than `refs/pr/<n>` and `refs/convert/<name>`) cannot be addressed.
- Xet storage is not announced: through Waystation, files come over HTTP and hf_xet is not used.
- A prefetched whole revision is limited to 2,000 files and 200 GiB.
- The largest cacheable file is the server's `WAYSTATION_MAX_UPLOAD_GB` (64 GiB by default).

## Troubleshooting

- **`EntryNotFound` / 404 for a file the Hub has**: the file is not in the revision's file list Waystation cached. A
  new commit of a branch appears after the repository's metadata max age (lower it, or ask for the commit).
- **`502` on a weight file**: the Hub redirected to a host that is not in `cdn_hosts`, or the downloaded bytes did not
  match the SHA-256 the Hub lists (nothing is cached; the next request tries again). The server log names the host.
- **Downloads bypass Waystation (`cas-server.xethub.hf.co` in client logs)**: the client is not using
  `HF_ENDPOINT` (set it before huggingface_hub is imported).
- **`401` with a token**: `HF_TOKEN` must be a Waystation API token, not a Hugging Face token.
- **Very large files**: they stream to the client while they are cached; a second client asking for the same file
  shares the download.
