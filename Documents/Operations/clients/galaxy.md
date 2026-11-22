# Ansible Galaxy client guide

How to point `ansible-galaxy` at a Waystation **Ansible Galaxy** repository and publish your own collections. The
repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples below use
`https://waystation.example.com` as the Waystation base URL, `galaxy` for a group or proxy repository and `mine` for a
hosted repository.

## Overview

- Collections are proxied, hosted and grouped; roles are proxied read-only (legacy v1 roles API).
- The repository URL is the Galaxy server URL: `https://waystation.example.com/repository/<repo>/`. `ansible-galaxy`
  finds the API at `<url>api/` by itself. Collections are served under `api/v3/collections/...`; Galaxy NG paths
  (`api/v3/plugin/ansible/content/<distribution>/collections/index/...` and `.../artifacts/<file>`) are accepted as
  aliases.
- Tested with ansible-core 2.22 (`tests/clients/galaxy/`); ansible-core 2.17 and later speak the same API.
- Protocol notes: [FORMATS.md](../../Architecture/FORMATS.md), section "Ansible Galaxy".

## Repository types

Create a repository in the UI (Repositories → Create → Ansible Galaxy) or through the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: the Galaxy server, e.g. `https://galaxy.ansible.com/` (preset), or another Galaxy NG / Pulp Ansible server. Collections path: where the upstream keeps `<namespace>/<name>/` documents, `api/v3/plugin/ansible/content/published/collections/index/` (galaxy.ansible.com and Galaxy NG, the default) or `api/v3/collections/` (servers with the plain v3 layout). Roles: proxy the legacy v1 roles API (default on). Collection documents and version lists are revalidated after the metadata max age. |
| Hosted | Stores the collections you publish with `ansible-galaxy collection publish`; a worker checks every upload before it is listed. Write policy: `allow_redeploy` or `disable_redeploy` (either way a published version cannot be replaced: delete it first), `read_only` (publishing and deleting answer `403`). |
| Group | Serves several Galaxy repositories under one server URL with merged version lists. Members in order (Galaxy proxy and hosted repositories), optionally a writable member (a hosted member that receives collections published to the group). |

### Proxy behaviour

- The collection document, every page of its version list and each version's document are cached as metadata and
  served with this repository's URLs: `href`s and pagination `links` are paths in the repository, and a version's
  `download_url` points at `collections/<ns>/<name>/<ns>-<name>-<version>.tar.gz` here. Large fields clients do not
  use (`files`, `manifest`, `docs_blob`, `metadata.contents`) are dropped.
- A collection artifact is verified against the version document's `artifact.sha256` and size while it downloads; a
  mismatch cuts the transfer and nothing is cached. Upstream documents must be JSON naming the collection asked for;
  a `download_url` must be http(s) (the SSRF guard applies to it and its redirects).
- Roles: the role search and version list are proxied, and each version's archive (usually a GitHub tag archive) is
  downloaded through the repository and cached.

### Group behaviour

Version lists are merged (a version several members have comes from the first member that has it); version
documents and artifacts come from the first member that has the version; roles from the first proxy member that has
them. Members that fail are skipped while another member answers.

### Browse

Namespaces → collections → versions (SemVer order, newest first), with `requires_ansible`, sha256, dependencies, the
publisher and whether the artifact is cached. A proxy lists the collections whose version lists it has cached.

## Client setup

### ansible-galaxy collection

```sh
ansible-galaxy collection install community.general -s https://waystation.example.com/repository/galaxy/
ansible-galaxy collection install 'community.general:>=9.0.0,<10' -s https://waystation.example.com/repository/galaxy/
ansible-galaxy collection install -r requirements.yml -s https://waystation.example.com/repository/galaxy/
```

To use the repository for every command, configure a server list in `ansible.cfg` (project directory,
`~/.ansible.cfg`, or the file `ANSIBLE_CONFIG` names):

```ini
[galaxy]
server_list = waystation

[galaxy_server.waystation]
url = https://waystation.example.com/repository/galaxy/
token = <your-api-token>
```

The same in CI, without a file:

```sh
export ANSIBLE_GALAXY_SERVER_LIST=waystation
export ANSIBLE_GALAXY_SERVER_WAYSTATION_URL=https://waystation.example.com/repository/galaxy/
export ANSIBLE_GALAXY_SERVER_WAYSTATION_TOKEN=<your-api-token>
```

A server can also be set per collection in `requirements.yml` (`source:`).

### ansible-galaxy role

```sh
ansible-galaxy role install geerlingguy.ntp -s https://waystation.example.com/repository/galaxy/
ansible-galaxy role install geerlingguy.ntp,4.0.1 -s https://waystation.example.com/repository/galaxy/
```

Roles are served by proxy repositories (and groups, from their first proxy member that has the role).

## Authentication

- Anonymous reads work where the deployment allows them (hosted repositories are not readable anonymously by
  default).
- Otherwise, and always to publish, use an API token (profile → API tokens, or a service account for CI).
  `ansible-galaxy` sends it as `Authorization: Token <token>`: set `token =` in `ansible.cfg`, the
  `ANSIBLE_GALAXY_SERVER_<NAME>_TOKEN` variable, or `--token <your-api-token>` on the command line.
- Without credentials, a repository you cannot see answers `401` (a missing one too); with a token that cannot read
  it, `404`.

## Publishing

Hosted repositories (and groups with a writable member) accept collections:

```sh
ansible-galaxy collection build                                     # my_namespace-my_collection-1.0.0.tar.gz
ansible-galaxy collection publish ./my_namespace-my_collection-1.0.0.tar.gz \
  -s https://waystation.example.com/repository/mine/ --token <your-api-token>

# delete one version, or every version of a collection
curl -fS -H "Authorization: Token <your-api-token>" -X DELETE \
  https://waystation.example.com/repository/mine/api/v3/collections/my_namespace/my_collection/versions/1.0.0/
curl -fS -H "Authorization: Token <your-api-token>" -X DELETE \
  https://waystation.example.com/repository/mine/api/v3/collections/my_namespace/my_collection/
```

- Publishing needs the `write` permission, deleting `delete`.
- `ansible-galaxy` waits for the import: the server stores the upload and answers with an import task, which a
  worker completes within seconds (`--no-wait` skips the wait; `--import-timeout` changes it). Publishing to a group
  with a writable member lands in that member; the task is polled through the group.
- An upload must be named `<namespace>-<name>-<version>.tar.gz` (lower-case names, a SemVer version), at most
  256 MiB, and match its `sha256` form field. The worker then reads it without extracting it: a gzip tar archive of
  regular files, directories and links inside the collection (no absolute paths, `..`, backslashes or control
  characters), `MANIFEST.json` naming the same namespace, name and version, `FILES.json` matching its checksum in the
  manifest, and every file in the archive listed in `FILES.json` with the right sha256. The version is listed only
  once the import completed.
- Versions are immutable: publishing an existing version answers `409`.

## Limitations

- Roles are proxy-only: they cannot be published to hosted repositories.
- The v1 roles API publishes no checksums, so role archives are cached unverified.
- A role `src:` pointing at a git URL bypasses Waystation entirely: Waystation cannot proxy git sources.
- A published collection version cannot be replaced; delete it first or publish a new version.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ansible-galaxy` answers "Galaxy server ... not found" or 404 | The repository is not readable anonymously: configure a token (see Authentication). Check the URL ends with `/repository/<name>/`. |
| Publishing answers 401 or 403 | No token, a token without the `write` scope, no write permission on the repository (or on the group's writable member), or a `read_only` repository. Proxies cannot be published to. |
| Publishing answers 409 | The version exists (versions are immutable: delete it or bump `version` in `galaxy.yml`), or an upload of it is still being imported. |
| Publishing answers 400 | The file name is not `<namespace>-<name>-<version>.tar.gz`, or the `sha256` does not match the upload; the message says which. |
| The import fails | The task's messages name the problem: `MANIFEST.json`/`FILES.json` missing or invalid, a file whose checksum differs from `FILES.json`, a file not listed, an unsafe path, or a manifest describing another version than the file name. Rebuild with `ansible-galaxy collection build`. |
| `ansible-galaxy` waits for the import and times out | No worker is running, or it is busy: check Tasks for `import.galaxy` jobs. The upload is kept; the import runs when a worker picks it up. |
| A download from a proxy is cut off | The upstream artifact does not match its published sha256; Waystation refuses it. Try again later or report it upstream. |
| A role installs straight from GitHub | The role's version list has no `download_url` for that version, or its `src:` is a git URL. |
