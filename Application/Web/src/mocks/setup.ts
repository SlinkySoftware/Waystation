// SPDX-License-Identifier: GPL-3.0-or-later
// "Set me up" snippets per format (FORMATS.md), as the server's format plugins will generate them
// (`setup_snippets(repo, user)`). {{TOKEN}} / {{USERNAME}} are placeholders the UI masks and can fill.
import type { ApkOptions, AptOptions, DockerOptions, Repository, SetupClient, SetupGuide, SetupSnippet } from '@/api/types'

import { BASE_URL, REGISTRY_HOST } from './fixtures/repositories'

/** `base` as it reads on `os` (the server's `on()`): same id and title, the given fields replaced. */
function on(os: 'linux' | 'macos' | 'windows', base: SetupSnippet, patch: Partial<SetupSnippet> = {}): SetupSnippet {
  return { ...base, ...patch, os }
}

const psEnv = (lines: [string, string][]) => lines.map(([k, v]) => `$env:${k} = "${v}"`).join('\n')

/** `<base URL host>:<connector port>` (the base URL's own port dropped), or null without a connector port (the server's `connector_host`). */
function connectorHost(repo: Repository): string | null {
  const port = (repo.format_options as DockerOptions | undefined)?.connector_port
  if (typeof port !== 'number') return null
  const host = new URL(BASE_URL).hostname
  return `${host}:${port}`
}

/** The dedicated port connector snippet (SPEC §4.4), when the repository has one (the server's `_connector`). */
function dockerConnector(repo: Repository, image: string): SetupSnippet[] {
  const chost = connectorHost(repo)
  if (chost === null) return []
  const port = (repo.format_options as DockerOptions).connector_port
  const base = { id: 'docker-connector', language: 'shell', filename: null, os: null } as const
  if (repo.type === 'hosted') {
    return [{ ...base, title: `Push and pull through the dedicated port ${port}`, content: `docker login ${chost}\ndocker tag myapp:1.0 ${chost}/myapp:1.0\ndocker push ${chost}/myapp:1.0`, description: 'No repository prefix on this port; it needs its own docker login.' }]
  }
  const description = repo.type === 'proxy' ? `No repository prefix on this port. Sign in with docker login ${chost} when anonymous pulls are not allowed.` : 'No repository prefix on this port.'
  return [{ ...base, title: `Pull through the dedicated port ${port}`, content: `docker pull ${chost}/${image}`, description }]
}

function dockerHosted(repo: Repository): SetupClient[] {
  const image = `${REGISTRY_HOST}/${repo.name}/myapp`
  const snippet = (id: string, title: string, content: string): SetupClient['snippets'][number] => ({ id, title, language: 'shell', filename: null, os: null, content })
  return [
    {
      id: 'docker',
      label: 'Docker',
      snippets: [
        { ...snippet('docker-login', 'Sign in (an API token as the password)', `echo "{{TOKEN}}" | docker login ${REGISTRY_HOST} --username {{USERNAME}} --password-stdin`), description: 'Pushing needs write permission on this repository; use an API token with the write scope.' },
        snippet('docker-push', 'Tag and push an image', `docker tag myapp:1.0 ${image}:1.0\ndocker push ${image}:1.0`),
        ...dockerConnector(repo, repo.type === 'hosted' ? 'myapp:1.0' : 'team/app:1.0'),
        snippet('docker-buildx', 'Build and push a multi-platform image', `docker buildx build --platform linux/amd64,linux/arm64 -t ${image}:1.0 --push .`),
        snippet('docker-pull', 'Pull', `docker pull ${image}:1.0`),
      ],
    },
    { id: 'podman', label: 'Podman', snippets: [snippet('podman-push', 'Push and pull with podman', `podman login ${REGISTRY_HOST}\npodman push myapp:1.0 ${image}:1.0\npodman pull ${image}:1.0`)] },
    {
      id: 'oci-tools',
      label: 'crane, skopeo, oras, cosign',
      snippets: [
        snippet('crane-copy', 'Copy an image from another registry (crane, skopeo)', `crane copy registry.example.com/team/app:1.0 ${image}:1.0\nskopeo copy --all docker://registry.example.com/team/app:1.0 docker://${image}:1.0`),
        snippet('oras-push', 'Push and pull an OCI artifact (oras)', `oras push ${image}-docs:1.0 README.md:text/markdown\noras pull ${image}-docs:1.0`),
        snippet('cosign-sign', 'Sign and verify an image (cosign; signatures are stored next to the image)', `cosign sign --key cosign.key ${image}@sha256:<digest>\ncosign verify --key cosign.pub ${image}:1.0`),
      ],
    },
  ]
}

function docker(repo: Repository): SetupClient[] {
  if (repo.type === 'hosted' || repo.type === 'group') return dockerHosted(repo)
  const opts = (repo.format_options ?? {}) as DockerOptions
  const alias = opts.aliases?.[0]
  const hub = Boolean(repo.proxy?.upstream_url.includes('docker.io'))
  const sample = hub ? 'library/nginx:latest' : repo.proxy?.upstream_url.includes('ghcr.io') ? 'home-assistant/home-assistant:stable' : 'org/image:tag'
  const host = REGISTRY_HOST
  const dockerSnippets: SetupClient['snippets'] = [
    { id: 'login', title: 'Sign in', description: 'Use an API token as the password. Anonymous pulls work without signing in if your administrator allows them.', language: 'shell', filename: null, os: null, content: `echo '{{TOKEN}}' | docker login ${host} --username {{USERNAME}} --password-stdin` },
  ]
  const prefix: SetupClient['snippets'][number] = { id: 'pull-prefix', title: 'Pull by repository prefix', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${repo.name}/${sample}` }
  // A non-default repository with an alias is usually addressed by the upstream's name (ghcr.io/<owner>/<image>).
  if (alias && !opts.default_repo) {
    dockerSnippets.push({ id: 'pull-alias', title: `Pull by upstream name (${alias}/…)`, description: `Keep ${alias} in the image reference: only the host changes.`, language: 'shell', filename: null, os: null, content: `docker pull ${host}/${alias}/${sample}` }, prefix)
  } else {
    dockerSnippets.push(prefix)
    if (alias) dockerSnippets.push({ id: 'pull-alias', title: `Pull by upstream alias (${alias})`, description: 'Keep the upstream registry name in the image reference: only the host changes.', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${alias}/${sample}` })
  }
  dockerSnippets.push(...dockerConnector(repo, opts.docker_hub ? 'library/nginx:latest' : 'org/image:tag'))
  if (opts.default_repo) {
    dockerSnippets.splice(1, 0, { id: 'pull-default', title: 'Pull without a prefix (default repository)', description: 'Docker Hub names work as they are; single-segment names get library/ like on Docker Hub.', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${hub ? 'nginx:latest' : sample}` })
    const mirror: SetupSnippet = { id: 'docker-mirror', title: 'Use as a Docker Hub mirror', description: 'Every docker pull of a Docker Hub image then goes through Waystation. Restart the daemon afterwards.', language: 'json', filename: '/etc/docker/daemon.json', os: null, content: `{\n  "registry-mirrors": ["${BASE_URL}"]\n}` }
    const desktop = { filename: 'Docker Desktop: Settings > Docker Engine', description: 'Add the key to the JSON there, then Apply & restart. Mirror mode only ever asks for Docker Hub images.' }
    dockerSnippets.unshift(mirror, on('macos', mirror, desktop), on('windows', mirror, desktop))
    dockerSnippets.splice(1, 0, { id: 'restart', title: 'Apply the mirror setting', language: 'shell', filename: null, os: 'linux', content: 'sudo systemctl restart docker' })
  }
  const upstreamHost = alias ?? new URL(repo.proxy?.upstream_url ?? BASE_URL).host
  return [
    { id: 'docker', label: 'Docker', snippets: dockerSnippets },
    {
      id: 'containerd',
      label: 'containerd',
      snippets: [
        { id: 'hosts', title: `Mirror ${upstreamHost} through Waystation`, description: 'containerd 1.7+ and 2.x; also used by nerdctl and Kubernetes nodes.', language: 'toml', filename: `/etc/containerd/certs.d/${upstreamHost}/hosts.toml`, os: 'linux', content: `server = "https://${upstreamHost === 'docker.io' ? 'registry-1.docker.io' : upstreamHost}"\n\n[host."${BASE_URL}/v2/${repo.name}"]\n  capabilities = ["pull", "resolve"]\n  override_path = true` },
      ],
    },
    {
      id: 'podman',
      label: 'Podman',
      snippets: [
        { id: 'registries', title: `Mirror ${upstreamHost}`, language: 'toml', filename: '/etc/containers/registries.conf.d/waystation.conf', os: 'linux', content: `[[registry]]\nprefix = "${upstreamHost}"\nlocation = "${upstreamHost}"\n\n[[registry.mirror]]\nlocation = "${host}/${repo.name}"` },
        { id: 'login', title: 'Sign in', language: 'shell', filename: null, os: null, content: `podman login ${host} --username {{USERNAME}} --password '{{TOKEN}}'` },
      ],
    },
  ]
}

function npm(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const authLine = `//${REGISTRY_HOST}/repository/${repo.name}/:_authToken={{TOKEN}}`
  return [
    {
      id: 'npm',
      label: 'npm',
      snippets: [
        { id: 'npmrc', title: 'Project or user .npmrc', description: 'Put it in the project root (shared) or ~/.npmrc (just you). Leave out the token line for anonymous reads.', language: 'ini', filename: '.npmrc', os: null, content: `registry=${url}\n${authLine}\nalways-auth=true` },
        { id: 'set', title: 'Or set it from the command line', language: 'shell', filename: null, os: null, content: `npm config set registry ${url}` },
        { id: 'login', title: 'Sign in with your username and password', description: 'Stores a fresh API token in ~/.npmrc.', language: 'shell', filename: null, os: null, content: `npm login --auth-type=legacy --registry=${url}` },
      ],
    },
    { id: 'pnpm', label: 'pnpm', snippets: [{ id: 'npmrc', title: '.npmrc (pnpm reads the same file)', language: 'ini', filename: '.npmrc', os: null, content: `registry=${url}\n${authLine}` }] },
    { id: 'yarn', label: 'Yarn', snippets: [
      { id: 'berry', title: 'Yarn 2+ (.yarnrc.yml)', language: 'yaml', filename: '.yarnrc.yml', os: null, content: `npmRegistryServer: "${url}"\nnpmAlwaysAuth: true\nnpmAuthToken: "{{TOKEN}}"` },
      { id: 'classic', title: 'Yarn 1 (.yarnrc)', language: 'ini', filename: '.yarnrc', os: null, content: `registry "${url}"` },
    ] },
    { id: 'bun', label: 'Bun', snippets: [{ id: 'bunfig', title: 'bunfig.toml', language: 'toml', filename: 'bunfig.toml', os: null, content: `[install]\nregistry = { url = "${url}", token = "{{TOKEN}}" }` }] },
  ]
}

function helm(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const snippet = (id: string, title: string, content: string, description?: string): SetupSnippet => ({ id, title, description, language: 'shell', filename: null, os: null, content })
  const snippets = [
    snippet('helm-repo-add', 'Add the repository', `helm repo add ${repo.name} ${url}\nhelm repo update ${repo.name}`),
    snippet('helm-repo-add-auth', 'With credentials (when anonymous reads are not allowed)', `helm repo add ${repo.name} ${url} --username {{USERNAME}} --password {{TOKEN}}`, 'An API token is the password. Helm sends it only to this host.'),
    snippet('helm-search', 'List the charts', `helm search repo ${repo.name}/ --versions`),
    snippet('helm-install', 'Install or download a chart', `helm install my-release ${repo.name}/<chart> --version <version>\nhelm pull ${repo.name}/<chart> --version <version>`),
  ]
  if (repo.type === 'hosted' || (repo.type === 'group' && repo.group?.writable_member)) {
    snippets.push(
      snippet('helm-package-push', 'Package and upload a chart', `helm package ./mychart\ncurl -fS -u {{USERNAME}}:{{TOKEN}} --upload-file mychart-0.1.0.tgz ${url}`, `Uploads need the write permission. The index is rebuilt within seconds; then run helm repo update ${repo.name}.`),
      snippet('helm-cm-push', 'Or with the helm-push plugin (ChartMuseum API)', `helm plugin install https://github.com/chartmuseum/helm-push\nhelm cm-push mychart-0.1.0.tgz ${repo.name} --context-path=/repository/${repo.name}`, 'The repository must have been added with --username and --password.'),
    )
  }
  return [{ id: 'helm', label: 'Helm', snippets }]
}

function galaxy(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const snippet = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupSnippet => ({ id, title, description, language, filename, os: null, content })
  const snippets = [
    snippet('ansible-cfg', 'Use this repository for every ansible-galaxy command (ansible.cfg)', `[galaxy]\nserver_list = waystation\n\n[galaxy_server.waystation]\nurl = ${url}\n# only when anonymous reads are not allowed (and to publish): an API token\ntoken = {{TOKEN}}\n`, 'ini', 'ansible.cfg', 'In the project directory, ~/.ansible.cfg or the file ANSIBLE_CONFIG names. ansible-galaxy sends the token as "Authorization: Token ...".'),
    snippet('galaxy-install', 'Install a collection', `ansible-galaxy collection install community.general -s ${url}\n# a version range, or from a requirements file\nansible-galaxy collection install 'community.general:>=9.0.0,<10' -s ${url}\nansible-galaxy collection install -r requirements.yml -s ${url}`),
    snippet('galaxy-env', 'Or with environment variables (CI)', `export ANSIBLE_GALAXY_SERVER_LIST=waystation\nexport ANSIBLE_GALAXY_SERVER_WAYSTATION_URL=${url}\nexport ANSIBLE_GALAXY_SERVER_WAYSTATION_TOKEN={{TOKEN}}`),
    snippet('galaxy-requirements', 'Per project (requirements.yml)', `collections:\n  - name: community.general\n    version: ">=9.0.0"\n    source: ${url}\n`, 'yaml', 'requirements.yml'),
  ]
  if (repo.type === 'hosted' || (repo.type === 'group' && repo.group?.writable_member)) {
    const into = repo.type === 'group' ? ` (stored in ${repo.group?.writable_member})` : ''
    snippets.push(snippet('galaxy-publish', `Build and publish a collection${into}`, `ansible-galaxy collection build\nansible-galaxy collection publish ./my_namespace-my_collection-1.0.0.tar.gz \\\n  -s ${url} --token {{TOKEN}}`, 'shell', null, 'Publishing needs the write permission. The import is checked within seconds (MANIFEST.json, FILES.json and every file checksum); versions cannot be replaced.'))
  }
  if (repo.type === 'hosted') {
    snippets.push(snippet('galaxy-delete', 'Delete a collection version', `curl -fS -H "Authorization: Token {{TOKEN}}" -X DELETE ${url}api/v3/collections/my_namespace/my_collection/versions/1.0.0/`))
  } else {
    snippets.push(snippet('galaxy-role-install', 'Install a role (legacy roles API, read-only)', `ansible-galaxy role install geerlingguy.docker -s ${url}`, 'shell', null, 'Role archives are fetched from the source the upstream names (usually GitHub tag archives) and cached unverified: the roles API publishes no checksums.'))
  }
  return [{ id: 'ansible-galaxy', label: 'ansible-galaxy', snippets }]
}

function swift(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const snippet = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupSnippet => ({ id, title, description, language, filename, os: null, content })
  const snippets = [
    snippet('swift-set', 'Use this registry (per project, or --global for every project)', `swift package-registry set ${url}\n# or only for one scope\nswift package-registry set --scope mona ${url}\n# for every package on this machine\nswift package-registry set --global ${url}`, 'shell', null, 'Writes .swiftpm/configuration/registries.json (with --global: ~/.swiftpm/configuration/). Swift Package Manager talks to registries over HTTPS only.'),
    snippet('swift-login', 'Sign in with an API token (needed to publish, and to read when anonymous access is off)', `swift package-registry login ${url} --token {{TOKEN}} --no-confirm`, 'shell', null, 'Swift sends the token as "Authorization: Bearer ...". On Linux it is stored in ~/.netrc; --username and --password sign in with a password instead.'),
    snippet('swift-dependency', 'Depend on a package by its registry identity (Package.swift)', 'dependencies: [\n    .package(id: "apple.swift-argument-parser", from: "1.5.0"),\n],', 'swift', 'Package.swift', repo.type === 'proxy' ? 'The identity is <scope>.<name>: for this proxy, the owner and the repository name on the Git host.' : 'The identity is <scope>.<name>, as published.'),
    snippet('swift-resolve', 'Resolve dependencies through the registry', 'swift package resolve\n# keep url: dependencies and let the registry serve them where it knows the URL\nswift package resolve --replace-scm-with-registry', 'shell', null, '--replace-scm-with-registry asks the registry which identity a Git URL has (/identifiers) and downloads that package from the registry instead of cloning it.'),
  ]
  if (repo.type === 'hosted') {
    snippets.push(
      snippet('swift-publish', 'Publish a release', `swift package-registry publish mona.LinkedList 1.0.0 --url ${url}\n# with release metadata (package-metadata.json in the package directory is picked up by default)\nswift package-registry publish mona.LinkedList 1.0.1 --url ${url} --metadata-path ./metadata.json`, 'shell', null, 'Publishing needs the write permission. Releases are immutable: a version cannot be published twice, nor again after it was removed.'),
      snippet('swift-unpublish', 'Remove a release', `curl -fS -X DELETE -H "Authorization: Bearer {{TOKEN}}" ${url}mona/LinkedList/1.0.0`, 'shell', null, 'Needs the delete permission. Clients then see the release as gone (410); the version stays reserved.'),
    )
  }
  return [{ id: 'swiftpm', label: 'Swift Package Manager', snippets }]
}

/** Cargo (sparse index): replace crates.io or use a named registry, credentials; hosted repositories and groups with a writable member also cargo publish and yank. */
function cargo(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const registry = `[registries.waystation]\nindex = "sparse+${url}index/"`
  const snippet = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupSnippet => ({ id, title, description, language, filename, os: null, content })
  const clients: SetupClient[] = [
    { id: 'cargo', label: 'Cargo', snippets: [
      snippet('cargo-replace', 'Use this repository instead of crates.io', `${registry}\n\n[source.crates-io]\nreplace-with = "waystation"`, 'toml', '~/.cargo/config.toml', 'Every crates.io dependency then resolves through this repository; Cargo.toml stays unchanged.'),
      snippet('cargo-registry', 'Use it as a named registry', `${registry}\n\n# in Cargo.toml: my-crate = { version = "1", registry = "waystation" }`, 'toml', '.cargo/config.toml'),
      snippet('cargo-credentials', 'Let cargo send tokens (once per machine)', '[registry]\nglobal-credential-providers = ["cargo:token"]', 'toml', '~/.cargo/config.toml', 'Cargo talks to registries that ask for credentials only with a credential provider configured; cargo:token keeps tokens in ~/.cargo/credentials.toml (cargo:libsecret, cargo:macos-keychain and cargo:wincred use the system keyring).'),
      snippet('cargo-login', 'Credentials (private repositories and publishing)', 'cargo login --registry waystation\n# paste {{TOKEN}} when asked, or set it for one shell only:\nexport CARGO_REGISTRIES_WAYSTATION_TOKEN={{TOKEN}}', 'shell', null, 'The token is a Waystation API token. Cargo stores it in ~/.cargo/credentials.toml.'),
    ] },
  ]
  if (repo.type === 'hosted' || (repo.type === 'group' && repo.group?.writable_member)) {
    clients.push({ id: 'cargo-publish', label: 'cargo publish', snippets: [
      snippet('cargo-publish', 'Publish a crate', 'cargo publish --registry waystation', 'shell', null, 'Needs the write permission. A version cannot be published twice; yank it instead.'),
      snippet('cargo-yank', 'Yank a version (or undo it with --undo)', 'cargo yank --registry waystation --version 1.0.0 my-crate', 'shell', null, 'A yanked version stays downloadable for lock files that already pin it.'),
    ] })
  }
  return clients
}

/** Hosted apt: the repository's signing key, a deb822 source with Signed-By, credentials, and curl uploads. */
function aptHosted(repo: Repository): SetupClient[] {
  const opts = (repo.format_options ?? {}) as AptOptions
  const url = `${BASE_URL}/repository/${repo.name}/`
  const keyring = `/etc/apt/keyrings/${repo.name}.asc`
  const components = opts.components ?? ['main']
  const sample = 'mypackage_1.0-1_amd64.deb'
  return [
    {
      id: 'apt',
      label: 'apt',
      snippets: [
        { id: 'apt-key', title: "Install the repository's signing key", description: `Waystation signs InRelease and Release.gpg with this repository's own key. Compare the fingerprint with the one shown in Waystation; a binary keyring is at ${url}public.gpg.`, language: 'shell', filename: null, os: 'linux', content: `sudo install -d -m 0755 /etc/apt/keyrings\ncurl -fsSL -u {{USERNAME}}:{{TOKEN}} ${url}public.asc | sudo tee ${keyring} > /dev/null\ngpg --show-keys ${keyring}` },
        { id: 'apt-sources', title: 'deb822 source (Debian 12+, Ubuntu 24.04+)', language: 'text', filename: `/etc/apt/sources.list.d/${repo.name}.sources`, os: 'linux', content: `Types: deb\nURIs: ${url}\nSuites: ${opts.distribution}\nComponents: ${components.join(' ')}\nSigned-By: ${keyring}\n` },
        { id: 'apt-auth', title: 'Credentials (an API token as the password)', description: 'Only when anonymous reads are off. Keep the file chmod 600; apt sends it over https only (write the machine line with http:// for a plain-http URL).', language: 'text', filename: `/etc/apt/auth.conf.d/${repo.name}.conf`, os: 'linux', content: `machine ${url.replace(/^https?:\/\//, '')}\nlogin {{USERNAME}}\npassword {{TOKEN}}\n` },
        { id: 'upload', title: 'Upload a package', description: `Uploads need the write permission. The package goes to component ${components[0]} (add ?component=<name> to the URL for another of: ${components.join(' ')}); the indexes are rebuilt and signed within seconds.`, language: 'shell', filename: null, os: 'linux', content: `curl -fS -u {{USERNAME}}:{{TOKEN}} --upload-file ${sample} ${url}` },
        { id: 'delete', title: 'Delete a package', description: 'The path is the one the upload answered (Location); deletes need the delete permission.', language: 'shell', filename: null, os: 'linux', content: `curl -fS -u {{USERNAME}}:{{TOKEN}} -X DELETE ${url}pool/${components[0]}/m/mypackage/${sample}` },
        { id: 'apt-update', title: 'Then', language: 'shell', filename: null, os: 'linux', content: 'sudo apt update && sudo apt install mypackage' },
      ],
    },
  ]
}

function apt(repo: Repository): SetupClient[] {
  if (repo.type === 'hosted') return aptHosted(repo)
  const opts = (repo.format_options ?? {}) as AptOptions
  const upstream = repo.proxy?.upstream_url ?? ''
  const url = `${BASE_URL}/repository/${repo.name}/`
  const ubuntu = upstream.includes('ubuntu')
  const rpi = upstream.includes('raspberrypi')
  const security = opts.distribution?.endsWith('-security')
  const suite = opts.distribution || 'trixie'
  const suites = security || rpi ? suite : `${suite} ${suite}-updates ${suite}-backports`
  const components = ubuntu ? 'main restricted universe multiverse' : rpi ? 'main' : 'main contrib non-free non-free-firmware'
  const keyring = ubuntu ? '/usr/share/keyrings/ubuntu-archive-keyring.gpg' : rpi ? '/usr/share/keyrings/raspberrypi-archive-keyring.gpg' : '/usr/share/keyrings/debian-archive-keyring.gpg'
  return [
    {
      id: 'apt',
      label: 'apt',
      snippets: [
        { id: 'deb822', title: 'Source file (deb822)', description: 'Replace the distribution\'s default source. Packages stay signed by the distribution: apt verifies them with its own keyring.', language: 'text', filename: `/etc/apt/sources.list.d/${repo.name}.sources`, os: 'linux', content: `Types: deb\nURIs: ${url}\nSuites: ${suites}\nComponents: ${components}\nSigned-By: ${keyring}` },
        { id: 'oneline', title: 'One-line format (older systems)', language: 'text', filename: `/etc/apt/sources.list.d/${repo.name}.list`, os: 'linux', content: `deb [signed-by=${keyring}] ${url} ${suites.split(' ')[0]} ${components}` },
        { id: 'auth', title: 'Credentials (only if anonymous reads are off)', language: 'text', filename: '/etc/apt/auth.conf.d/waystation.conf', os: 'linux', content: `machine ${REGISTRY_HOST}/repository/${repo.name}/\nlogin {{USERNAME}}\npassword {{TOKEN}}` },
        { id: 'update', title: 'Refresh', language: 'shell', filename: null, os: 'linux', content: 'sudo apt update' },
      ],
    },
  ]
}

/** Hosted yum: a .repo file with Waystation's signing key for repomd.xml, credentials, and curl uploads. */
function yumHosted(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const content = `[${repo.name}]\nname=${repo.name} (Waystation)\nbaseurl=${url}\nenabled=1\nrepo_gpgcheck=1\ngpgcheck=0\ngpgkey=${url}RPM-GPG-KEY\nusername={{USERNAME}}\npassword={{TOKEN}}`
  return [
    {
      id: 'dnf',
      label: 'dnf / yum',
      snippets: [
        { id: 'repo', title: 'Repository file', description: "Waystation signs repomd.xml with this repository's own key (repo_gpgcheck=1; dnf imports the key from gpgkey on first use). Set gpgcheck=1 when your packages are signed and add your signing key to gpgkey. An API token is the password; keep the file chmod 600.", language: 'ini', filename: `/etc/yum.repos.d/${repo.name}.repo`, os: 'linux', content },
        { id: 'upload', title: 'Upload a package', description: `Uploads need the write permission. The repodata is rebuilt and signed within seconds; then run dnf makecache --refresh --repo=${repo.name}.`, language: 'shell', filename: null, os: 'linux', content: `curl -fS -u {{USERNAME}}:{{TOKEN}} --upload-file mypackage-1.0-1.el10.x86_64.rpm ${url}` },
        { id: 'key', title: 'Fingerprint of the signing key', description: 'Compare it with the key dnf shows before importing it.', language: 'shell', filename: null, os: 'linux', content: `curl -fsS ${url}RPM-GPG-KEY | gpg --show-keys` },
        { id: 'delete', title: 'Delete a package', language: 'shell', filename: null, os: 'linux', content: `curl -fS -u {{USERNAME}}:{{TOKEN}} -X DELETE ${url}mypackage-1.0-1.el10.x86_64.rpm` },
        { id: 'refresh', title: 'Refresh and install', language: 'shell', filename: null, os: 'linux', content: `sudo dnf makecache --refresh --repo=${repo.name} && sudo dnf install mypackage` },
      ],
    },
  ]
}

/** yum / dnf: one .repo file per distribution repo id, keeping $releasever/$basearch and the distribution's GPG check. */
function yum(repo: Repository): SetupClient[] {
  if (repo.type === 'hosted') return yumHosted(repo)
  const upstream = repo.proxy?.upstream_url ?? ''
  const url = `${BASE_URL}/repository/${repo.name}`
  const oracle = upstream.includes('oracle')
  const epel = upstream.includes('epel')
  const alma = upstream.includes('almalinux')
  const name = oracle ? 'Oracle Linux' : epel ? 'EPEL' : alma ? 'AlmaLinux' : upstream.includes('rocky') ? 'Rocky Linux' : repo.name
  const key = oracle ? 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-oracle' : epel ? 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-$releasever' : alma ? 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-$releasever' : 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-$releasever'
  const sections: [string, string, string][] = oracle
    ? [['ol9_baseos_latest', 'BaseOS Latest', 'OL9/baseos/latest'], ['ol9_appstream', 'AppStream', 'OL9/appstream'], ['ol9_UEKR8', 'UEK Release 8', 'OL9/UEKR8']]
    : epel
      ? [['epel', 'Everything', '$releasever/Everything']]
      : [['baseos', 'BaseOS', '$releasever/BaseOS'], ['appstream', 'AppStream', '$releasever/AppStream']]
  const baseurl = (path: string) => (oracle ? `${url}/repo/OracleLinux/${path}/$basearch/` : epel ? `${url}/${path}/$basearch/` : `${url}/${path}/$basearch/os/`)
  const content = sections.map(([id, label, path]) => `[${repo.name}-${id}]\nname=${name} ${oracle ? '9' : '$releasever'} - ${label} (Waystation)\nbaseurl=${baseurl(path)}\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=0\ngpgkey=${key}\n#username={{USERNAME}}\n#password={{TOKEN}}`).join('\n\n')
  const disable = oracle ? 'sudo dnf config-manager --disable ol9_baseos_latest ol9_appstream ol9_UEKR8' : epel ? 'sudo dnf config-manager --disable epel' : 'sudo dnf config-manager --disable baseos appstream'
  return [
    {
      id: 'dnf',
      label: 'dnf',
      snippets: [
        { id: 'repo', title: 'Repository file', description: 'Packages stay signed by the distribution: dnf verifies them with its own key (gpgcheck stays on). Uncomment the credentials only if anonymous reads are off.', language: 'ini', filename: `/etc/yum.repos.d/${repo.name}.repo`, os: 'linux', content },
        { id: 'disable', title: 'Turn off the distribution\'s own mirrors', description: 'So dnf does not fall back to the internet (mirrorlist/metalink upstreams are not used).', language: 'shell', filename: null, os: 'linux', content: disable },
        { id: 'refresh', title: 'Refresh and install', language: 'shell', filename: null, os: 'linux', content: 'sudo dnf makecache && sudo dnf upgrade' },
      ],
    },
  ]
}

function rawHosted(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}`
  const file = 'releases/1.0/app-1.0.tar.gz'
  const s = (id: string, title: string, content: string): SetupClient['snippets'][number] => ({ id, title, language: 'shell', filename: null, os: null, content })
  const writable = repo.type === 'hosted' || Boolean(repo.group?.writable_member)
  return [
    { id: 'curl', label: 'curl', snippets: [
      s('curl-get', 'Download a file', `curl -fSLO ${url}/${file}`),
      s('curl-auth', 'With credentials (when anonymous reads are not allowed)', `curl -fSLO -u {{USERNAME}}:{{TOKEN}} ${url}/${file}`),
      on('windows', s('curl-get', 'Download a file', `curl.exe -fSLO ${url}/${file}`), { language: 'powershell' }),
      on('windows', s('curl-auth', 'With credentials (when anonymous reads are not allowed)', `curl.exe -fSLO -u {{USERNAME}}:{{TOKEN}} ${url}/${file}`), { language: 'powershell' }),
      ...(writable ? [
        { ...s('curl-put', 'Upload a file', `curl -fS -u {{USERNAME}}:{{TOKEN}} --upload-file app-1.0.tar.gz ${url}/${file}`), description: 'Uploads need the write permission; an API token is the password.' },
        s('curl-put-checksum', 'Upload with a checksum the server verifies', `curl -fS -u {{USERNAME}}:{{TOKEN}} \\\n  -H "X-Checksum-Sha256: $(sha256sum app-1.0.tar.gz | cut -d' ' -f1)" \\\n  --upload-file app-1.0.tar.gz ${url}/${file}`),
      ] : []),
      ...(repo.type === 'hosted' ? [s('curl-delete', 'Delete a file', `curl -fS -u {{USERNAME}}:{{TOKEN}} -X DELETE ${url}/${file}`)] : []),
    ] },
    { id: 'wget', label: 'wget', snippets: [s('wget-get', 'Download a file', `wget ${url}/${file}`)] },
  ]
}

function raw(repo: Repository): SetupClient[] {
  if (repo.type === 'hosted' || repo.type === 'group') return rawHosted(repo)
  const url = `${BASE_URL}/repository/${repo.name}`
  const sample = 'cli/cli/releases/download/v2.83.0/gh_2.83.0_linux_amd64.tar.gz'
  return [
    { id: 'curl', label: 'curl', snippets: [
      { id: 'download', title: 'Download a file', description: 'The path after the repository name is the path on the upstream.', language: 'shell', filename: null, os: null, content: `curl -fLO ${url}/${sample}` },
      { id: 'auth', title: 'With credentials', language: 'shell', filename: null, os: null, content: `curl -fLO -u {{USERNAME}}:{{TOKEN}} ${url}/${sample}` },
    ] },
    { id: 'wget', label: 'wget', snippets: [{ id: 'download', title: 'Download a file', language: 'shell', filename: null, os: null, content: `wget ${url}/${sample}` }] },
  ]
}

function maven(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const server = `<servers>\n  <server>\n    <id>${repo.name}</id>\n    <username>{{USERNAME}}</username>\n    <password>{{TOKEN}}</password>\n  </server>\n</servers>\n`
  const policy = (repo.format_options as { version_policy?: string } | undefined)?.version_policy ?? 'release'
  const snapshots = repo.type === 'group' || policy !== 'release'
  const profile = snapshots
    ? '  <!-- Maven Central (which the mirror replaces) has SNAPSHOTs turned off: turn them on -->\n  <profiles>\n    <profile>\n      <id>waystation</id>\n      <repositories>\n        <repository>\n          <id>central</id>\n          <url>http://central</url>\n          <snapshots><enabled>true</enabled></snapshots>\n        </repository>\n      </repositories>\n    </profile>\n  </profiles>\n  <activeProfiles><activeProfile>waystation</activeProfile></activeProfiles>\n'
    : ''
  const settings = `<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">\n  <mirrors>\n    <mirror>\n      <id>${repo.name}</id>\n      <mirrorOf>*</mirrorOf>\n      <url>${url}</url>\n    </mirror>\n  </mirrors>\n  <!-- only when anonymous reads are not allowed: your username and an API token -->\n${server.replace(/^/gm, '  ').trimEnd()}\n${profile}</settings>\n`
  const tags = policy === 'snapshot' ? ['snapshotRepository'] : policy === 'mixed' ? ['repository', 'snapshotRepository'] : ['repository']
  const distribution = `<distributionManagement>\n${tags.map((t) => `  <${t}>\n    <id>${repo.name}</id>\n    <url>${url}</url>\n  </${t}>\n`).join('')}</distributionManagement>\n`
  const hosted = repo.type === 'hosted'
  const mavenSnippets: SetupClient['snippets'] = [
    { id: 'settings-mirror', title: 'Resolve everything through this repository (~/.m2/settings.xml)', language: 'xml', filename: 'settings.xml', os: null, content: settings },
    {
      id: 'maven-config',
      title: 'Optional: send the credentials up front (.mvn/maven.config, or on the command line)',
      description: 'Not required: Maven sends the settings.xml credentials after the 401 challenge from Waystation. Sending them up front saves that extra round trip per request.',
      language: 'shell',
      filename: '.mvn/maven.config',
      os: null,
      content: '-Daether.connector.http.preemptiveAuth=true\n',
    },
  ]
  if (hosted) {
    mavenSnippets.push(
      { id: 'pom-distribution', title: 'Deploy with mvn deploy (pom.xml)', description: 'Add the matching <server> (your username and an API token) to settings.xml.', language: 'xml', filename: 'pom.xml', os: null, content: distribution },
      { id: 'settings-server', title: 'Deploy credentials (~/.m2/settings.xml)', language: 'xml', filename: 'settings.xml', os: null, content: server },
      { id: 'mvn-deploy-alt', title: 'Deploy to this repository without a distributionManagement section', language: 'shell', filename: null, os: null, content: `mvn deploy -DaltDeploymentRepository=${repo.name}::${url}` },
    )
  }
  const gradle: SetupClient['snippets'] = [
    { id: 'gradle-repositories', title: 'Resolve (build.gradle.kts)', language: 'kotlin', filename: 'build.gradle.kts', os: null, content: `repositories {\n    maven {\n        url = uri("${url}")\n    }\n}\n` },
  ]
  if (hosted) {
    gradle.push(
      { id: 'gradle-publishing', title: 'Publish (build.gradle.kts)', language: 'kotlin', filename: 'build.gradle.kts', os: null, content: `plugins {\n    \`maven-publish\`\n}\n\npublishing {\n    repositories {\n        maven {\n            url = uri("${url}")\n            credentials {\n                username = providers.gradleProperty("wsUser").get()\n                password = providers.gradleProperty("wsToken").get()\n            }\n            authentication { create<BasicAuthentication>("basic") }  // optional: send them up front\n        }\n    }\n}\n` },
      { id: 'gradle-properties', title: 'Credentials (~/.gradle/gradle.properties)', language: 'ini', filename: 'gradle.properties', os: null, content: 'wsUser={{USERNAME}}\nwsToken={{TOKEN}}\n' },
    )
  }
  return [
    { id: 'maven', label: 'Maven', snippets: mavenSnippets },
    { id: 'gradle', label: 'Gradle', snippets: gradle },
    { id: 'sbt', label: 'sbt', snippets: [{ id: 'sbt-resolver', title: 'Resolve (build.sbt)', language: 'scala', filename: 'build.sbt', os: null, content: `resolvers += "${repo.name}" at "${url}"\n` }] },
  ]
}

/** PyPI: pip, uv, Poetry, PDM; hosted repositories and groups with a writable member also twine and uv publish. */
function pypi(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const index = `${url}simple/`
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null): SetupClient['snippets'][number] => ({ id, title, language, filename, os: null, content })
  const clients: SetupClient[] = [
    { id: 'pip', label: 'pip', snippets: [
      s('pip-install', 'Install a package', `pip install --index-url ${index} requests`),
      s('pip-conf', 'Use this index for every pip command (pip.conf)', `[global]\nindex-url = ${index}`, 'ini', '~/.config/pip/pip.conf'),
      on('macos', s('pip-conf', 'Use this index for every pip command (pip.conf)', `[global]\nindex-url = ${index}`, 'ini', '~/Library/Application Support/pip/pip.conf')),
      on('windows', s('pip-conf', 'Use this index for every pip command (pip.conf)', `[global]\nindex-url = ${index}`, 'ini', '%APPDATA%\\pip\\pip.ini')),
    ] },
    { id: 'uv', label: 'uv', snippets: [
      s('uv-pip', 'uv pip', `uv pip install --index-url ${index} requests`),
      s('uv-project', 'Per project (pyproject.toml)', `[[tool.uv.index]]\nname = "waystation"\nurl = "${index}"\ndefault = true`, 'toml', 'pyproject.toml'),
      s('uv-auth', 'Credentials (when anonymous reads are not allowed)', 'export UV_INDEX_WAYSTATION_USERNAME={{USERNAME}}\nexport UV_INDEX_WAYSTATION_PASSWORD={{TOKEN}}'),
      on('windows', s('uv-auth', 'Credentials (when anonymous reads are not allowed)', psEnv([['UV_INDEX_WAYSTATION_USERNAME', '{{USERNAME}}'], ['UV_INDEX_WAYSTATION_PASSWORD', '{{TOKEN}}']]), 'powershell')),
    ] },
    { id: 'poetry', label: 'Poetry', snippets: [s('poetry-source', 'Add this index as the primary source', `poetry source add --priority=primary waystation ${index}\npoetry config http-basic.waystation {{USERNAME}} {{TOKEN}}`)] },
    { id: 'pdm', label: 'PDM', snippets: [s('pdm-config', 'Use this index', `pdm config pypi.url ${index}\npdm config pypi.username {{USERNAME}}\npdm config pypi.password {{TOKEN}}`)] },
  ]
  if (repo.type === 'hosted' || (repo.type === 'group' && repo.group?.writable_member)) {
    clients.push(
      { id: 'twine', label: 'twine', snippets: [
        s('pypirc', 'Configure twine (~/.pypirc)', `[distutils]\nindex-servers = waystation\n\n[waystation]\nrepository = ${url}\nusername = {{USERNAME}}\npassword = {{TOKEN}}`, 'ini', '.pypirc'),
        s('twine-upload', 'Upload', 'twine upload --repository waystation dist/*'),
      ] },
      { id: 'uv-publish', label: 'uv publish', snippets: [s('uv-publish', 'Publish with uv', `uv publish --publish-url ${url} --username {{USERNAME}} --password {{TOKEN}} dist/*`)] },
    )
  }
  return clients
}

/** NuGet: the v3 service index for dotnet and NuGet.exe; push, delete (unlist) for hosted repositories and writable groups. */
function nuget(repo: Repository): SetupClient[] {
  const index = `${BASE_URL}/repository/${repo.name}/index.json`
  const writable = repo.type === 'hosted' || (repo.type === 'group' && Boolean(repo.group?.writable_member))
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupClient['snippets'][number] => ({ id, title, description, language, filename, os: null, content })
  const config = `<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="waystation" value="${index}" protocolVersion="3" />
  </packageSources>
  <packageSourceCredentials>
    <waystation>
      <add key="Username" value="{{USERNAME}}" />
      <add key="ClearTextPassword" value="{{TOKEN}}" />
    </waystation>
  </packageSourceCredentials>
</configuration>`
  const dotnet: SetupClient['snippets'] = [
    s('dotnet-source', 'Add the source', `dotnet nuget add source ${index} --name waystation`),
    s('dotnet-source-auth', 'With credentials (when anonymous reads are not allowed)', `dotnet nuget add source ${index} --name waystation --username {{USERNAME}} --password {{TOKEN}} --store-password-in-clear-text`, 'shell', null, 'An API token is the password. --store-password-in-clear-text is needed on Linux and macOS, where dotnet cannot encrypt it.'),
    s('nuget-config', 'Or per solution (nuget.config)', config, 'xml', 'nuget.config', '<clear /> makes this repository the only source: restores never reach nuget.org directly. Leave out packageSourceCredentials when anonymous reads are allowed.'),
    s('dotnet-add', 'Add a package', 'dotnet add package Newtonsoft.Json\ndotnet restore'),
  ]
  if (writable) {
    dotnet.push(
      s('dotnet-push', 'Push a package', `dotnet pack -c Release\ndotnet nuget push bin/Release/MyLib.1.0.0.nupkg --source ${index} --api-key {{TOKEN}}`, 'shell', null, 'The API key is an API token with write permission on this repository.'),
      s('dotnet-delete', 'Delete or unlist a version', `dotnet nuget delete MyLib 1.0.0 --source ${index} --api-key {{TOKEN}} --non-interactive`, 'shell', null, 'The repository\'s delete setting decides: unlist (hidden from search and version resolution, still restorable) or delete.'),
    )
  }
  const nugetExe: SetupClient['snippets'] = [
    s('nuget-exe-source', 'Add the source', `nuget sources add -Name waystation -Source ${index}`),
    s('nuget-exe-source-auth', 'With credentials (when anonymous reads are not allowed)', `nuget sources add -Name waystation -Source ${index} -Username {{USERNAME}} -Password {{TOKEN}}`),
    s('nuget-exe-install', 'Install a package', 'nuget install Newtonsoft.Json -Source waystation'),
  ]
  if (writable) nugetExe.push(s('nuget-exe-push', 'Push a package', `nuget push MyLib.1.0.0.nupkg -Source ${index} -ApiKey {{TOKEN}}`))
  return [
    { id: 'dotnet', label: 'dotnet CLI', snippets: dotnet },
    { id: 'nuget-exe', label: 'NuGet.exe', snippets: nugetExe },
  ]
}

/** Composer: the repository in a project (config or composer.json), auth.json credentials; upload and delete for hosted repositories. */
function composer(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const host = new URL(BASE_URL).host
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupClient['snippets'][number] => ({ id, title, description, language, filename, os: null, content })
  const members = repo.type === 'group' ? `One repository for the members, in order: ${(repo.group?.members ?? []).join(', ')}. ` : ''
  const json = `{\n  "repositories": [\n    {"type": "composer", "url": "${url}"},\n    {"packagist.org": false}\n  ]\n}`
  const snippets: SetupClient['snippets'] = [
    s('composer-repo', 'Use this repository in a project', `composer config repositories.waystation composer ${url}\ncomposer config repositories.packagist.org false`, 'shell', null, `${members}The second line stops Composer asking packagist.org directly.`),
  ]
  if (BASE_URL.startsWith('http:')) snippets.push(s('composer-secure-http', 'Allow plain HTTP', 'composer config secure-http false'))
  snippets.push(
    s('composer-auth', 'Credentials (auth.json)', `composer config http-basic.${host} {{USERNAME}} {{TOKEN}}`, 'shell', null, 'An API token is the password; leave it out when anonymous reads are allowed.'),
    s('composer-json', 'Or in composer.json', json, 'json', 'composer.json'),
    s('composer-require', 'Add a package', 'composer require monolog/monolog'),
  )
  if (repo.type === 'hosted') {
    snippets.push(
      s('composer-upload', 'Upload a package', `cd my-package && zip -r ../acme-demo-1.0.0.zip . -x '.git/*' 'vendor/*'\ncurl -u {{USERNAME}}:{{TOKEN}} --upload-file ../acme-demo-1.0.0.zip ${url}packages/upload/acme/demo/1.0.0`, 'shell', null, 'The zip holds composer.json at its root (or in one top-level folder); its name must be acme/demo. Needs the write permission.'),
      s('composer-delete', 'Delete a version', `curl -u {{USERNAME}}:{{TOKEN}} -X DELETE ${url}packages/upload/acme/demo/1.0.0`, 'shell', null, 'Needs the delete permission.'),
    )
  }
  return [{ id: 'composer', label: 'Composer', snippets }]
}

/** Alpine apk: /etc/apk/repositories lines for the configured branch and repositories; hosted: the index signing
 *  key and uploads. */
function apk(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const o = (repo.format_options ?? {}) as ApkOptions
  const branch = o.branch ?? 'latest-stable'
  const repos = o.repositories ?? ['main', 'community']
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupClient['snippets'][number] => ({ id, title, description, language, filename, os: 'linux', content })
  const lines = repos.map((r) => `${url}${branch}/${r}`).join('\n')
  const snippets: SetupClient['snippets'] = [
    s('repositories', 'Repository list', lines, 'text', '/etc/apk/repositories', repo.type === 'hosted' ? 'Add these lines; the index is signed with this repository\'s own key (next step).' : 'Replace the dl-cdn lines. Packages stay signed by Alpine: apk verifies them with the keys it already has.'),
    s('auth', 'Credentials (when anonymous reads are off)', repos.map((r) => `${url.replace('://', '://{{USERNAME}}:{{TOKEN}}@')}${branch}/${r}`).join('\n'), 'text', '/etc/apk/repositories', 'An API token is the password; keep the file readable by root only.'),
  ]
  if (repo.type === 'hosted') {
    const keyName = `${repo.name}-7k2m9q4x.rsa.pub` // the server: <name>-<last 8 characters of the repository id>
    snippets.push(s('key', 'Trust the signing key', `wget -O /etc/apk/keys/${keyName} ${url}keys/${keyName}`, 'shell', null, 'apk checks the APKINDEX signature with this public key (published after the first upload).'))
    snippets.push(s('upload', 'Upload a package', `curl -fS -u {{USERNAME}}:{{TOKEN}} --upload-file mypackage-1.0-r0.apk ${url}${branch}/${repos[0] ?? 'main'}/x86_64/mypackage-1.0-r0.apk`, 'shell', null, 'Needs the write permission. The index is rebuilt and signed within seconds.'))
    snippets.push(s('delete', 'Delete a package', `curl -fS -u {{USERNAME}}:{{TOKEN}} -X DELETE ${url}${branch}/${repos[0] ?? 'main'}/x86_64/mypackage-1.0-r0.apk`))
  }
  snippets.push(s('install', 'Update and install', repo.type === 'hosted' ? 'apk update && apk add mypackage' : 'apk update && apk add curl'))
  return [{ id: 'apk', label: 'apk', snippets }]
}

/** RubyGems: Bundler (Gemfile source, credentials), the gem CLI; gem push for hosted repositories. */
function rubygems(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupClient['snippets'][number] => ({ id, title, description, language, filename, os: null, content })
  const members = repo.type === 'group' ? `One source for the members, in order: ${(repo.group?.members ?? []).join(', ')}. ` : ''
  const bundler: SetupClient['snippets'] = [
    s('bundler-source', 'Use this repository in a Gemfile', `source "${url}"\n\ngem "rake", "~> 13.2"`, 'ruby', 'Gemfile', `${members}Replace the rubygems.org source line.`),
    s('bundler-auth', 'Credentials', `bundle config set --global ${url} {{USERNAME}}:{{TOKEN}}`, 'shell', null, 'An API token is the password; leave it out when anonymous reads are allowed.'),
    s('bundler-mirror', 'Or mirror rubygems.org for every project', `bundle config set --global mirror.https://rubygems.org ${url}`),
  ]
  const gem: SetupClient['snippets'] = [
    s('gem-sources', 'Use this repository as the only source', `gem sources --remove https://rubygems.org/\ngem sources --add ${url}`),
    s('gem-install', 'Install a gem', `gem install rake --source ${url}`),
  ]
  if (repo.type === 'hosted') {
    gem.push(s('gem-push', 'Push a gem', `gem build example-demo.gemspec\nGEM_HOST_API_KEY={{TOKEN}} gem push example-demo-1.0.0.gem --host ${url}`, 'shell', null, 'The API token goes in GEM_HOST_API_KEY (or ~/.gem/credentials). Needs the write permission.'))
  }
  return [
    { id: 'bundler', label: 'Bundler', snippets: bundler },
    { id: 'gem', label: 'gem CLI', snippets: gem },
  ]
}

/** Go: GOPROXY (the checksum database through the repository), private modules, .netrc credentials. */
function go(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}`
  const host = new URL(BASE_URL).host
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupClient['snippets'][number] => ({ id, title, description, language, filename, os: null, content })
  return [
    { id: 'go', label: 'go', snippets: [
      s('go-env', 'Use this repository for every module download', `go env -w GOPROXY=${url}\n# checksums are still verified against sum.golang.org, through this repository (sumdb/ proxy)\ngo mod download`, 'shell', null, 'Without ",direct" in GOPROXY every module comes through Waystation. The checksum database is reached through it too: the go command asks the proxy for sumdb/sum.golang.org/supported first.'),
      s('go-once', 'One command', `GOPROXY=${url} go get example.com/some/module@latest`),
      s('go-private', 'Private modules (not in the public checksum database)', '# fetched through Waystation, not looked up in the checksum database:\ngo env -w GONOSUMDB=example.com/private/*\n# GOPRIVATE=... would also bypass the proxy (GONOPROXY): use it only for modules fetched directly', 'shell', null, 'Add the same patterns to the repository\'s "nosumdb" option so Waystation does not look them up either.'),
    ] },
    { id: 'go-auth', label: 'Authentication', snippets: [
      s('netrc', 'Credentials (when anonymous reads are not allowed)', `machine ${host}\nlogin {{USERNAME}}\npassword {{TOKEN}}`, 'text', '~/.netrc', 'The go command sends .netrc credentials over HTTPS only (GOAUTH=netrc, the default). Use an API token as the password; NETRC points to another file.'),
      on('windows', s('netrc', 'Credentials (when anonymous reads are not allowed)', `machine ${host}\nlogin {{USERNAME}}\npassword {{TOKEN}}`, 'text', '%USERPROFILE%\\_netrc', 'The go command sends .netrc credentials over HTTPS only (GOAUTH=netrc, the default). Use an API token as the password; NETRC points to another file.')),
    ] },
  ]
}

/** Terraform/OpenTofu proxy: the provider_installation network mirror, module lookups through a host block and
 * credentials blocks (bearer API token); the hosted repository: credentials, sources and publishing. */
function terraform(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const host = new URL(BASE_URL).host
  const s = (id: string, title: string, content: string, language = 'shell', filename: string | null = null, description?: string): SetupClient['snippets'][number] => ({ id, title, description, language, filename, os: null, content })
  if (repo.type === 'hosted') return terraformHosted(repo, host, s)
  const registry = repo.proxy?.upstream_url ? new URL(repo.proxy.upstream_url).host : 'registry.terraform.io'
  const config =
    `provider_installation {\n  network_mirror {\n    url = "${url}"\n  }\n}\n\n` +
    `# modules from ${registry} through Waystation (module sources stay "<namespace>/<name>/<system>")\n` +
    `host "${registry}" {\n  services = {\n    "modules.v1" = "${url}v1/modules/"\n  }\n}\n` +
    `credentials "${registry}" {\n  token = "{{TOKEN}}"\n}\n\ncredentials "${host}" {\n  token = "{{TOKEN}}"\n}\n`
  const note = `Every provider is installed through Waystation (network mirror). The host block sends module lookups for ${registry} to this repository: archives from GitHub tags and allowed hosts are cached here, other module sources are fetched by the client itself. The credentials blocks are needed only when anonymous reads are not allowed: the API token is sent as a bearer token to Waystation only.`
  return [
    { id: 'terraform', label: 'Terraform', snippets: [
      s('terraform-cli-config', 'Terraform CLI configuration', config, 'hcl', '~/.terraformrc', `${note} TF_CLI_CONFIG_FILE names another file.`),
      on('windows', s('terraform-cli-config', 'Terraform CLI configuration', config, 'hcl', '%APPDATA%\\terraform.rc', `${note} TF_CLI_CONFIG_FILE names another file.`)),
      s('terraform-init', 'Install providers and modules', 'terraform init\n# record the hashes of every platform your team uses in .terraform.lock.hcl:\nterraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=windows_amd64'),
      s('terraform-token-env', 'Or pass the token in the environment', `export TF_TOKEN_${host.replace(/-/g, '__').replace(/\./g, '_')}={{TOKEN}}`, 'shell', null, 'Instead of the credentials block (host names without a port only).'),
    ] },
    { id: 'tofu', label: 'OpenTofu', snippets: [
      s('tofu-cli-config', 'OpenTofu CLI configuration', config, 'hcl', '~/.tofurc', `${note} TF_CLI_CONFIG_FILE names another file.`),
      on('windows', s('tofu-cli-config', 'OpenTofu CLI configuration', config, 'hcl', '%APPDATA%\\tofu.rc', `${note} TF_CLI_CONFIG_FILE names another file.`)),
      s('tofu-init', 'Install providers and modules', 'tofu init'),
    ] },
  ]
}

type Snip = (id: string, title: string, content: string, language?: string, filename?: string | null, description?: string) => SetupClient['snippets'][number]

function terraformHosted(repo: Repository, host: string, s: Snip): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}/`
  const config = `credentials "${host}" {\n  token = "{{TOKEN}}"\n}\n`
  const note = `Module and provider addresses start with this host name, which Terraform finds through https://${host}/.well-known/terraform.json (it must contain a dot, and Waystation must be served over https). The credentials block is needed only when anonymous reads are not allowed; file downloads get short-lived signed URLs, so no other configuration is needed.`
  const usage = s(
    'terraform-sources',
    'Use private providers and modules',
    `terraform {\n  required_providers {\n    example = {\n      source  = "${host}/my-namespace/example"\n      version = "1.0.0"\n    }\n  }\n}\n\nmodule "network" {\n  source  = "${host}/my-namespace/network/aws"\n  version = "1.0.0"\n}\n`,
    'hcl',
    'main.tf',
    'Module sources are <host>/<namespace>/<name>/<system>, provider sources <host>/<namespace>/<type>.',
  )
  const publishModule = s(
    'terraform-publish-module',
    'Publish a module',
    `tar -czf network.tar.gz -C ./network .\ncurl --fail -H "Authorization: Bearer {{TOKEN}}" --upload-file network.tar.gz \\\n  ${url}v1/modules/my-namespace/network/aws/1.0.0`,
    'shell',
    null,
    "The archive holds the module directory's contents (its .tf files at the root). Publishing needs the write permission; versions cannot be replaced (delete one with curl -X DELETE on the same URL).",
  )
  const publishProvider = s(
    'terraform-publish-provider',
    'Publish a provider release',
    'cd dist  # terraform-provider-example_1.0.0_<os>_<arch>.zip for each platform\n' +
      'sha256sum terraform-provider-example_1.0.0_*.zip > terraform-provider-example_1.0.0_SHA256SUMS\n' +
      'gpg --detach-sign terraform-provider-example_1.0.0_SHA256SUMS\n' +
      'gpg --armor --export my-signing-key > signing-key.asc\n' +
      'curl --fail -H "Authorization: Bearer {{TOKEN}}" \\\n' +
      '  $(for f in terraform-provider-example_1.0.0_*.zip; do printf -- "-F archive=@%s " "$f"; done) \\\n' +
      '  -F shasums=@terraform-provider-example_1.0.0_SHA256SUMS \\\n' +
      '  -F signature=@terraform-provider-example_1.0.0_SHA256SUMS.sig \\\n' +
      '  -F signing_key=@signing-key.asc -F protocols=5.0 \\\n' +
      `  ${url}v1/providers/my-namespace/example/1.0.0`,
    'shell',
    null,
    'SHA256SUMS must list every archive and verify with the signing key, as Terraform and OpenTofu check them when they install the provider. Releases cannot be replaced.',
  )
  return [
    { id: 'terraform', label: 'Terraform', snippets: [
      s('terraform-cli-config', 'Terraform CLI configuration', config, 'hcl', '~/.terraformrc', `${note} TF_CLI_CONFIG_FILE names another file; terraform login ${host} works too.`),
      on('windows', s('terraform-cli-config', 'Terraform CLI configuration', config, 'hcl', '%APPDATA%\\terraform.rc', `${note} TF_CLI_CONFIG_FILE names another file; terraform login ${host} works too.`)),
      s('terraform-token-env', 'Or pass the token in the environment', `export TF_TOKEN_${host.replace(/-/g, '__').replace(/\./g, '_')}={{TOKEN}}`, 'shell', null, 'Instead of the credentials block (host names without a port only).'),
      usage,
      s('terraform-init', 'Install', 'terraform init'),
      publishModule,
      publishProvider,
    ] },
    { id: 'tofu', label: 'OpenTofu', snippets: [
      s('tofu-cli-config', 'OpenTofu CLI configuration', config, 'hcl', '~/.tofurc', `${note} TF_CLI_CONFIG_FILE names another file; tofu login ${host} works too.`),
      on('windows', s('tofu-cli-config', 'OpenTofu CLI configuration', config, 'hcl', '%APPDATA%\\tofu.rc', `${note} TF_CLI_CONFIG_FILE names another file; tofu login ${host} works too.`)),
      usage,
      s('tofu-init', 'Install', 'tofu init'),
    ] },
  ]
}

export function setupGuide(repo: Repository): SetupGuide {
  const clients =
    repo.format === 'docker' ? docker(repo) : repo.format === 'npm' ? npm(repo) : repo.format === 'apt' ? apt(repo) : repo.format === 'yum' ? yum(repo) : repo.format === 'maven' ? maven(repo) : repo.format === 'pypi' ? pypi(repo) : repo.format === 'go' ? go(repo) : repo.format === 'helm' ? helm(repo) : repo.format === 'galaxy' ? galaxy(repo) : repo.format === 'terraform' ? terraform(repo) : repo.format === 'swift' ? swift(repo) : repo.format === 'cargo' ? cargo(repo) : repo.format === 'nuget' ? nuget(repo) : repo.format === 'composer' ? composer(repo) : repo.format === 'rubygems' ? rubygems(repo) : repo.format === 'apk' ? apk(repo) : raw(repo)
  const opts = (repo.format_options ?? {}) as DockerOptions
  const notes: string[] = []
  if (repo.format === 'docker') {
    notes.push(`Docker clients address this repository as ${REGISTRY_HOST}/${repo.name}/… (path routing).`)
    if (opts.aliases?.length) notes.push(`${opts.aliases.map((a) => `${REGISTRY_HOST}/${a}/…`).join(', ')} also routes here.`)
    const chost = connectorHost(repo)
    if (chost) notes.push(`${chost}/… (dedicated port) also routes here.`)
    if (opts.default_repo) notes.push('This is the default docker repository: unprefixed names and registry-mirrors pulls land here.')
  }
  return { repository: repo.name, format: repo.format, clients, notes }
}
