// SPDX-License-Identifier: GPL-3.0-or-later
// "Set me up" snippets per format (FORMATS.md), as the server's format plugins will generate them
// (`setup_snippets(repo, user)`). {{TOKEN}} / {{USERNAME}} are placeholders the UI masks and can fill.
import type { AptOptions, DockerOptions, Repository, SetupClient, SetupGuide } from '@/api/types'

import { BASE_URL, REGISTRY_HOST } from './fixtures/repositories'

function docker(repo: Repository): SetupClient[] {
  const opts = (repo.format_options ?? {}) as DockerOptions
  const alias = opts.aliases?.[0]
  const sample = repo.proxy?.upstream_url.includes('docker.io') ? 'library/nginx:latest' : repo.name === 'ghcr' ? 'astral-sh/uv:latest' : 'org/image:tag'
  const host = REGISTRY_HOST
  const dockerSnippets: SetupClient['snippets'] = [
    { id: 'login', title: 'Sign in', description: 'Use an API token as the password. Anonymous pulls work without signing in if your administrator allows them.', language: 'shell', filename: null, os: null, content: `echo '{{TOKEN}}' | docker login ${host} --username {{USERNAME}} --password-stdin` },
    { id: 'pull-prefix', title: 'Pull by repository prefix', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${repo.name}/${sample}` },
  ]
  if (alias) dockerSnippets.push({ id: 'pull-alias', title: `Pull by upstream alias (${alias})`, description: 'Keep the upstream registry name in the image reference: only the host changes.', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${alias}/${sample}` })
  if (opts.default_repo) {
    dockerSnippets.push({ id: 'pull-default', title: 'Pull without a prefix (default repository)', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${sample}` })
    dockerSnippets.unshift({ id: 'mirror', title: 'Use as a Docker Hub mirror', description: 'Every docker pull of a Docker Hub image then goes through Waystation. Restart the daemon afterwards.', language: 'json', filename: '/etc/docker/daemon.json', os: 'linux', content: `{\n  "registry-mirrors": ["${BASE_URL}"]\n}` })
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

function apt(repo: Repository): SetupClient[] {
  const opts = (repo.format_options ?? {}) as AptOptions
  const upstream = repo.proxy?.upstream_url ?? ''
  const url = `${BASE_URL}/repository/${repo.name}/`
  const ubuntu = upstream.includes('ubuntu')
  const rpi = upstream.includes('raspberrypi')
  const security = opts.distribution?.endsWith('-security')
  const suite = opts.distribution || 'trixie'
  const suites = security ? suite : ubuntu ? `${suite} ${suite}-updates` : `${suite} ${suite}-updates`
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

function raw(repo: Repository): SetupClient[] {
  const url = `${BASE_URL}/repository/${repo.name}`
  const sample = repo.name === 'raw-isos' ? 'debian-cd/13.1.0/amd64/iso-cd/debian-13.1.0-amd64-netinst.iso' : 'cli/cli/releases/download/v2.83.0/gh_2.83.0_linux_amd64.tar.gz'
  return [
    { id: 'curl', label: 'curl', snippets: [
      { id: 'download', title: 'Download a file', description: 'The path after the repository name is the path on the upstream.', language: 'shell', filename: null, os: null, content: `curl -fLO ${url}/${sample}` },
      { id: 'auth', title: 'With credentials', language: 'shell', filename: null, os: null, content: `curl -fLO -u {{USERNAME}}:{{TOKEN}} ${url}/${sample}` },
    ] },
    { id: 'wget', label: 'wget', snippets: [{ id: 'download', title: 'Download a file', language: 'shell', filename: null, os: null, content: `wget ${url}/${sample}` }] },
  ]
}

export function setupGuide(repo: Repository): SetupGuide {
  const clients = repo.format === 'docker' ? docker(repo) : repo.format === 'npm' ? npm(repo) : repo.format === 'apt' ? apt(repo) : raw(repo)
  const notes = repo.format === 'docker' ? [`Docker clients address this repository as ${REGISTRY_HOST}/${repo.name}/… (path routing).`] : []
  return { repository: repo.name, format: repo.format, clients, notes }
}
