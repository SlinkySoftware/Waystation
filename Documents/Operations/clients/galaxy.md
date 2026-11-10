# Ansible Galaxy: client guide

A Galaxy proxy repository caches collections (and, read-only, roles) from galaxy.ansible.com or another Galaxy NG /
Pulp Ansible server. A Galaxy hosted repository stores the collections you publish with `ansible-galaxy collection
publish`; a worker checks every upload before it is listed. A Galaxy group serves several Galaxy repositories under
one server URL with merged version lists. Tested with ansible-core 2.22 (`tests/clients/galaxy/`); ansible-core 2.17
and later speak the same API. Protocol notes: FORMATS.md "Ansible Galaxy".

Examples use `https://registry.example.com` as the Waystation base URL.

## Create the repository

In the UI (Repositories → Create → Ansible Galaxy) or the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: the Galaxy server, e.g. `https://galaxy.ansible.com/` (preset). Collections path: where the upstream keeps `<namespace>/<name>/` documents, `api/v3/plugin/ansible/content/published/collections/index/` (galaxy.ansible.com and Galaxy NG, the default) or `api/v3/collections/` (servers with the plain v3 layout). Roles: proxy the legacy v1 roles API (default on). Collection documents and version lists are revalidated after the metadata max age. |
| Hosted | Write policy: `allow_redeploy` or `disable_redeploy` (either way a published version cannot be replaced: delete it first), `read_only` (publishing and deleting answer `403`). |
| Group | Members in order (Galaxy proxy and hosted repositories), optionally a writable member (a hosted member that receives collections published to the group). |

## Client configuration

The repository URL is the Galaxy server URL (`ansible-galaxy` finds the API at `<url>api/` by itself):

```sh
ansible-galaxy collection install community.general -s https://registry.example.com/repository/galaxy/
ansible-galaxy collection install 'community.general:>=9.0.0,<10' -s https://registry.example.com/repository/galaxy/
ansible-galaxy collection install -r requirements.yml -s https://registry.example.com/repository/galaxy/
```

To use it for every command, configure a server list in `ansible.cfg` (project directory, `~/.ansible.cfg`, or the
file `ANSIBLE_CONFIG` names). When anonymous reads are not allowed (hosted repositories are not readable anonymously
by default), and to publish, add an API token; `ansible-galaxy` sends it as `Authorization: Token <token>`:

```ini
[galaxy]
server_list = waystation

[galaxy_server.waystation]
url = https://registry.example.com/repository/galaxy/
token = <your-api-token>
```

The same in CI, without a file:

```sh
export ANSIBLE_GALAXY_SERVER_LIST=waystation
export ANSIBLE_GALAXY_SERVER_WAYSTATION_URL=https://registry.example.com/repository/galaxy/
export ANSIBLE_GALAXY_SERVER_WAYSTATION_TOKEN=<your-api-token>
```

Or per collection in `requirements.yml` (`source:`), or `--token <your-api-token>` on the command line. The
repository's **Set me up** tab shows these snippets with your repository URL filled in.

### Publishing and deleting collections (hosted)

```sh
ansible-galaxy collection build                                     # my_namespace-my_collection-1.0.0.tar.gz
ansible-galaxy collection publish ./my_namespace-my_collection-1.0.0.tar.gz \
  -s https://registry.example.com/repository/mine/ --token <your-api-token>

# delete one version, or every version of a collection
curl -fS -H "Authorization: Token <your-api-token>" -X DELETE \
  https://registry.example.com/repository/mine/api/v3/collections/my_namespace/my_collection/versions/1.0.0/
curl -fS -H "Authorization: Token <your-api-token>" -X DELETE \
  https://registry.example.com/repository/mine/api/v3/collections/my_namespace/my_collection/
```

Publishing needs the `write` permission, deleting `delete`. `ansible-galaxy` waits for the import: the server
stores the upload and answers with an import task, which a worker completes within seconds (`--no-wait` skips the
wait; `--import-timeout` changes it). Publishing to a group with a writable member lands in that member; the task is
polled through the group.

### Roles (proxy, read-only)

```sh
ansible-galaxy role install geerlingguy.ntp -s https://registry.example.com/repository/galaxy/
ansible-galaxy role install geerlingguy.ntp,4.0.1 -s https://registry.example.com/repository/galaxy/
```

Roles come from the legacy v1 API: the role search and version list are proxied, and each version's archive (usually
a GitHub tag archive) is downloaded through the repository and cached. **Limitations**: the v1 API publishes no
checksums, so role archives are cached unverified; roles cannot be published to hosted repositories; a role
`src:` pointing at a git URL bypasses Waystation entirely.

## How it behaves

- **Proxy documents**: the collection document, every page of its version list and each version's document are
  cached as metadata and served with this repository's URLs: `href`s and pagination `links` are paths in the
  repository, a version's `download_url` points at `collections/<ns>/<name>/<ns>-<name>-<version>.tar.gz` here.
  Large fields clients do not use (`files`, `manifest`, `docs_blob`, `metadata.contents`) are dropped.
  Galaxy NG paths (`api/v3/plugin/ansible/content/<distribution>/collections/index/...` and `.../artifacts/<file>`)
  are accepted as aliases of `api/v3/collections/...`.
- **Validation**: a collection artifact is verified against the version document's `artifact.sha256` and size while
  it downloads; a mismatch cuts the transfer and nothing is cached. Upstream documents must be JSON naming the
  collection asked for; a `download_url` must be http(s) (the SSRF guard applies to it and its redirects).
- **Hosted imports**: an upload must be named `<namespace>-<name>-<version>.tar.gz` (lower-case names, a SemVer
  version), at most 256 MiB, and match its `sha256` form field. The worker then reads it without extracting it: a
  gzip tar archive of regular files, directories and links inside the collection (no absolute paths, `..`,
  backslashes or control characters), `MANIFEST.json` naming the same namespace, name and version, `FILES.json`
  matching its checksum in the manifest, and every file in the archive listed in `FILES.json` with the right sha256.
  The version is listed only once the import completed. Versions are immutable: publishing an existing version
  answers `409`.
- **Groups**: version lists are merged (a version several members have comes from the first member that has it);
  version documents and artifacts come from the first member that has the version; roles from the first proxy
  member that has them. Members that fail are skipped while another member answers.
- **Browse**: namespaces → collections → versions (SemVer order, newest first), with `requires_ansible`, sha256,
  dependencies, the publisher and whether the artifact is cached. A proxy lists the collections whose version lists
  it has cached.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ansible-galaxy` answers "Galaxy server ... not found" or 404 | The repository is not readable anonymously: configure a token (see above). A hidden repository answers 404. Check the URL ends with `/repository/<name>/`. |
| Publishing answers 401 or 403 | No token, a token without the `write` scope, no write permission on the repository (or on the group's writable member), or a `read_only` repository. Proxies cannot be published to. |
| Publishing answers 409 | The version exists (versions are immutable: delete it or bump `version` in `galaxy.yml`), or an upload of it is still being imported. |
| Publishing answers 400 | The file name is not `<namespace>-<name>-<version>.tar.gz`, or the `sha256` does not match the upload; the message says which. |
| The import fails | The task's messages name the problem: `MANIFEST.json`/`FILES.json` missing or invalid, a file whose checksum differs from `FILES.json`, a file not listed, an unsafe path, or a manifest describing another version than the file name. Rebuild with `ansible-galaxy collection build`. |
| `ansible-galaxy` waits for the import and times out | No worker is running, or it is busy: check Tasks for `import.galaxy` jobs. The upload is kept; the import runs when a worker picks it up. |
| A download from a proxy is cut off | The upstream artifact does not match its published sha256; Waystation refuses it. Try again later or report it upstream. |
| A role installs straight from GitHub | The role's version list has no `download_url` for that version, or its `src:` is a git URL: Waystation cannot proxy git sources. |
