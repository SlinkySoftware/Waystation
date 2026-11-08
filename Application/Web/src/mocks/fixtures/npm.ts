// SPDX-License-Identifier: GPL-3.0-or-later
// npm packages in the npm proxy: real package names, plausible versions and tarball sizes.
import type { NpmPackage, NpmVersion } from '@/api/types'

import { hash, KiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000

interface PackageSeed {
  name: string
  description: string
  latest: string
  /** Typical tarball size, KiB. */
  kib: number
  license: string
  versions: number
}

export const NPM_PACKAGES: PackageSeed[] = [
  { name: 'lodash', description: 'Lodash modular utilities.', latest: '4.17.21', kib: 312, license: 'MIT', versions: 114 },
  { name: 'react', description: 'React is a JavaScript library for building user interfaces.', latest: '19.2.0', kib: 66, license: 'MIT', versions: 2400 },
  { name: 'react-dom', description: 'React package for working with the DOM.', latest: '19.2.0', kib: 1580, license: 'MIT', versions: 2350 },
  { name: 'vue', description: 'The progressive JavaScript framework for building modern web UI.', latest: '3.5.24', kib: 640, license: 'MIT', versions: 520 },
  { name: 'typescript', description: 'TypeScript is a language for application scale JavaScript development', latest: '5.9.3', kib: 4430, license: 'Apache-2.0', versions: 3300 },
  { name: 'vite', description: 'Native-ESM powered web dev build tool', latest: '7.2.2', kib: 820, license: 'MIT', versions: 640 },
  { name: 'esbuild', description: 'An extremely fast JavaScript and CSS bundler and minifier.', latest: '0.25.12', kib: 42, license: 'MIT', versions: 790 },
  { name: '@esbuild/linux-x64', description: 'The Linux 64-bit binary for esbuild.', latest: '0.25.12', kib: 4560, license: 'MIT', versions: 330 },
  { name: '@babel/core', description: 'Babel compiler core.', latest: '7.28.5', kib: 220, license: 'MIT', versions: 190 },
  { name: '@types/node', description: 'TypeScript definitions for node', latest: '24.10.0', kib: 520, license: 'MIT', versions: 2100 },
  { name: 'express', description: 'Fast, unopinionated, minimalist web framework', latest: '5.1.0', kib: 21, license: 'MIT', versions: 280 },
  { name: 'axios', description: 'Promise based HTTP client for the browser and node.js', latest: '1.13.2', kib: 590, license: 'MIT', versions: 110 },
  { name: 'chalk', description: 'Terminal string styling done right', latest: '5.6.2', kib: 14, license: 'MIT', versions: 40 },
  { name: 'commander', description: 'the complete solution for node.js command-line programs', latest: '14.0.2', kib: 48, license: 'MIT', versions: 170 },
  { name: 'zod', description: 'TypeScript-first schema declaration and validation library with static type inference', latest: '4.1.12', kib: 1280, license: 'MIT', versions: 600 },
  { name: 'eslint', description: 'An AST-based pattern checker for JavaScript.', latest: '9.39.1', kib: 760, license: 'MIT', versions: 380 },
  { name: 'prettier', description: 'Prettier is an opinionated code formatter', latest: '3.6.2', kib: 2080, license: 'MIT', versions: 160 },
  { name: 'next', description: 'The React Framework', latest: '16.0.1', kib: 31200, license: 'MIT', versions: 3100 },
  { name: 'tslib', description: 'Runtime library for TypeScript helper functions', latest: '2.8.1', kib: 19, license: '0BSD', versions: 70 },
  { name: 'semver', description: 'The semantic version parser used by npm.', latest: '7.7.3', kib: 26, license: 'ISC', versions: 90 },
  { name: 'debug', description: 'Lightweight debugging utility for Node.js and the browser', latest: '4.4.3', kib: 13, license: 'MIT', versions: 80 },
  { name: 'ws', description: 'Simple to use, blazing fast and thoroughly tested websocket client and server for Node.js', latest: '8.18.3', kib: 36, license: 'MIT', versions: 170 },
  { name: 'uuid', description: 'RFC9562 UUIDs', latest: '13.0.0', kib: 64, license: 'MIT', versions: 60 },
  { name: 'date-fns', description: 'Modern JavaScript date utility library', latest: '4.1.0', kib: 4380, license: 'MIT', versions: 190 },
  { name: 'rxjs', description: 'Reactive Extensions for modern JavaScript', latest: '7.8.2', kib: 1350, license: 'Apache-2.0', versions: 250 },
  { name: 'rollup', description: 'Next-generation ES module bundler', latest: '4.53.1', kib: 660, license: 'MIT', versions: 860 },
  { name: 'postcss', description: 'Tool for transforming styles with JS plugins', latest: '8.5.6', kib: 82, license: 'MIT', versions: 270 },
  { name: 'tailwindcss', description: 'A utility-first CSS framework for rapidly building custom user interfaces.', latest: '4.1.17', kib: 690, license: 'MIT', versions: 3900 },
  { name: 'vitest', description: 'Next generation testing framework powered by Vite', latest: '4.0.8', kib: 480, license: 'MIT', versions: 420 },
  { name: 'quasar', description: 'Build high-performance VueJS user interfaces (SPA, PWA, SSR, Mobile and Desktop) in record time', latest: '2.18.6', kib: 3850, license: 'MIT', versions: 650 },
  { name: 'pinia', description: 'Intuitive, type safe and flexible Store for Vue', latest: '3.0.4', kib: 46, license: 'MIT', versions: 140 },
  { name: '@vue/compiler-sfc', description: '@vue/compiler-sfc', latest: '3.5.24', kib: 650, license: 'MIT', versions: 380 },
  { name: 'undici', description: 'An HTTP/1.1 client, written from scratch for Node.js', latest: '7.16.0', kib: 410, license: 'MIT', versions: 250 },
  { name: 'request', description: 'Simplified HTTP request client.', latest: '2.88.2', kib: 54, license: 'Apache-2.0', versions: 126 },
]

/** Version strings counting down from `latest`. */
function versionList(seed: PackageSeed, count: number): string[] {
  const [maj = 1, min = 0, pat = 0] = seed.latest.split('.').map((p) => Number.parseInt(p, 10))
  const out: string[] = []
  let [a, b, c] = [maj, min, pat]
  const next = rng(hash(`ver:${seed.name}`))
  while (out.length < count && a >= 0) {
    out.push(`${a}.${b}.${c}`)
    if (c > 0) c--
    else if (b > 0) {
      b--
      c = Math.floor(next() * 6)
    } else {
      a--
      b = Math.floor(next() * 8)
      c = Math.floor(next() * 4)
    }
  }
  return out
}

export function npmPackageList(): NpmPackage[] {
  return NPM_PACKAGES.map((seed) => {
    const next = rng(hash(`pkg:${seed.name}`))
    const cached = 1 + Math.floor(next() * 9)
    return {
      name: seed.name,
      description: seed.description,
      latest: seed.latest,
      version_count: seed.versions,
      cached_versions: cached,
      size_bytes: Math.round(seed.kib * KiB * cached * (0.8 + next() * 0.3)),
      license: seed.license,
      last_downloaded_at: new Date(NOW - next() * 2 * DAY).toISOString(),
      downloads: Math.round(40 + next() * 9000),
    }
  })
}

export function npmVersionsFor(name: string): { pkg: NpmPackage; versions: NpmVersion[]; distTags: Record<string, string> } | null {
  const seed = NPM_PACKAGES.find((p) => p.name === name)
  const pkg = npmPackageList().find((p) => p.name === name)
  if (!seed || !pkg) return null
  const next = rng(hash(`vers:${name}`))
  const list = versionList(seed, Math.min(40, seed.versions))
  const cachedIdx = new Set<number>([0])
  while (cachedIdx.size < Math.min(pkg.cached_versions, list.length)) cachedIdx.add(Math.floor(next() * Math.min(list.length, 14)))
  let t = NOW - next() * 12 * DAY
  const versions = list.map((version, i): NpmVersion => {
    t -= (1 + next() * 20) * DAY
    const cached = cachedIdx.has(i)
    const size = Math.round(seed.kib * KiB * (1 - i * 0.004) * (0.95 + next() * 0.1))
    return {
      version,
      published_at: new Date(t).toISOString(),
      size_bytes: size,
      unpacked_size: Math.round(size * (3.2 + next() * 1.5)),
      cached,
      downloads: cached ? Math.round(3 + next() * 2500 / (i + 1)) : 0,
      deprecated: name === 'request' ? 'request has been deprecated, see https://github.com/request/request/issues/3142' : null,
      integrity: `sha512-${btoa(String.fromCharCode(...Array.from({ length: 48 }, () => Math.floor(next() * 256)))).slice(0, 86)}==`,
      dist_tags: i === 0 ? ['latest'] : [],
      last_downloaded_at: cached ? new Date(NOW - next() * 5 * DAY).toISOString() : null,
    }
  })
  const distTags: Record<string, string> = { latest: seed.latest }
  if (['react', 'react-dom', 'next', 'typescript', 'vite'].includes(name)) {
    const canary = `${Number(seed.latest.split('.')[0]) + (name === 'typescript' ? 1 : 0)}.${name === 'typescript' ? '0' : String(Number(seed.latest.split('.')[1]) + 1)}.0-${name === 'typescript' ? 'dev.20261107' : 'canary.4'}`
    distTags[name === 'typescript' ? 'next' : 'canary'] = canary
  }
  return { pkg, versions, distTags }
}

/** Tarball file name as npm requests it: `@scope/name/-/name-1.2.3.tgz`. */
export function tarballPath(name: string, version: string): string {
  const base = name.includes('/') ? name.split('/')[1] : name
  return `${name}/-/${base}-${version}.tgz`
}
