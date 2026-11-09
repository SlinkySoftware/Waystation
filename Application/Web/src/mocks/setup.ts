// SPDX-License-Identifier: GPL-3.0-or-later
// "Set me up" snippets per format (FORMATS.md), as the server's format plugins will generate them
// (`setup_snippets(repo, user)`). {{TOKEN}} / {{USERNAME}} are placeholders the UI masks and can fill.
import type { AptOptions, DockerOptions, Repository, SetupClient, SetupGuide } from '@/api/types'

import { BASE_URL, REGISTRY_HOST } from './fixtures/repositories'

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
  if (opts.default_repo) {
    dockerSnippets.splice(1, 0, { id: 'pull-default', title: 'Pull without a prefix (default repository)', description: 'Docker Hub names work as they are; single-segment names get library/ like on Docker Hub.', language: 'shell', filename: null, os: null, content: `docker pull ${host}/${hub ? 'nginx:latest' : sample}` })
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

/** yum / dnf: one .repo file per distribution repo id, keeping $releasever/$basearch and the distribution's GPG check. */
function yum(repo: Repository): SetupClient[] {
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
      title: 'Send the credentials up front (.mvn/maven.config, or on the command line)',
      description: 'Needed when anonymous users cannot read this repository: Waystation answers 404, not a challenge, to anonymous requests for it, and Maven only sends credentials after a challenge.',
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
      { id: 'gradle-publishing', title: 'Publish (build.gradle.kts)', language: 'kotlin', filename: 'build.gradle.kts', os: null, content: `plugins {\n    \`maven-publish\`\n}\n\npublishing {\n    repositories {\n        maven {\n            url = uri("${url}")\n            credentials {\n                username = providers.gradleProperty("wsUser").get()\n                password = providers.gradleProperty("wsToken").get()\n            }\n            authentication { create<BasicAuthentication>("basic") }  // send them up front\n        }\n    }\n}\n` },
      { id: 'gradle-properties', title: 'Credentials (~/.gradle/gradle.properties)', language: 'ini', filename: 'gradle.properties', os: null, content: 'wsUser={{USERNAME}}\nwsToken={{TOKEN}}\n' },
    )
  }
  return [
    { id: 'maven', label: 'Maven', snippets: mavenSnippets },
    { id: 'gradle', label: 'Gradle', snippets: gradle },
    { id: 'sbt', label: 'sbt', snippets: [{ id: 'sbt-resolver', title: 'Resolve (build.sbt)', language: 'scala', filename: 'build.sbt', os: null, content: `resolvers += "${repo.name}" at "${url}"\n` }] },
  ]
}

export function setupGuide(repo: Repository): SetupGuide {
  const clients =
    repo.format === 'docker' ? docker(repo) : repo.format === 'npm' ? npm(repo) : repo.format === 'apt' ? apt(repo) : repo.format === 'yum' ? yum(repo) : repo.format === 'maven' ? maven(repo) : raw(repo)
  const opts = (repo.format_options ?? {}) as DockerOptions
  const notes: string[] = []
  if (repo.format === 'docker') {
    notes.push(`Docker clients address this repository as ${REGISTRY_HOST}/${repo.name}/… (path routing).`)
    if (opts.aliases?.length) notes.push(`${opts.aliases.map((a) => `${REGISTRY_HOST}/${a}/…`).join(', ')} also routes here.`)
    if (opts.default_repo) notes.push('This is the default docker repository: unprefixed names and registry-mirrors pulls land here.')
  }
  return { repository: repo.name, format: repo.format, clients, notes }
}
