// SPDX-License-Identifier: GPL-3.0-or-later
// Helm charts for any Helm repository created in the mock: real public chart names, plausible versions and sizes.
import type { HelmChart, HelmVersion } from '@/api/types'

import { hash, KiB } from '../rng'
import { BASE_URL } from './repositories'

const NOW = Date.now()
const DAY = 86_400_000

interface ChartSeed {
  name: string
  description: string
  app: string
  /** Minor versions of the latest major, newest first. */
  versions: string[]
  kib: number
  deprecated?: boolean
}

const CHARTS: ChartSeed[] = [
  { name: 'cert-manager', description: 'A Helm chart for cert-manager', app: 'v1.19.1', versions: ['v1.19.1', 'v1.19.0', 'v1.18.3', 'v1.18.2', 'v1.17.4'], kib: 102 },
  { name: 'ingress-nginx', description: 'Ingress controller for Kubernetes using NGINX as a reverse proxy and load balancer', app: '1.14.0', versions: ['4.14.0', '4.13.3', '4.13.2', '4.12.6'], kib: 58 },
  { name: 'kube-prometheus-stack', description: 'kube-prometheus-stack collects Kubernetes manifests, Grafana dashboards, and Prometheus rules', app: 'v0.86.2', versions: ['79.5.0', '79.4.1', '78.3.2', '77.14.0'], kib: 640 },
  { name: 'metrics-server', description: 'Metrics Server is a scalable, efficient source of container resource metrics for Kubernetes built-in autoscaling pipelines.', app: '0.8.0', versions: ['3.13.0', '3.12.2', '3.12.1', '3.11.0'], kib: 14 },
  { name: 'prometheus-node-exporter', description: 'A Helm chart for prometheus node-exporter', app: '1.10.2', versions: ['4.49.1', '4.48.0', '4.47.3'], kib: 18 },
  { name: 'kube-state-metrics', description: 'Install kube-state-metrics to generate and expose cluster-level metrics', app: '2.17.0', versions: ['6.4.1', '6.3.0'], kib: 16 },
]

function chart(seed: ChartSeed): HelmChart {
  return { name: seed.name, latest_version: seed.versions[0] ?? '', app_version: seed.app, description: seed.description, icon: null, deprecated: seed.deprecated ?? false, version_count: seed.versions.length }
}

export function helmChartList(): HelmChart[] {
  return CHARTS.map(chart)
}

export function helmVersionsFor(repo: string, name: string): { chart: HelmChart; versions: HelmVersion[] } | null {
  const seed = CHARTS.find((c) => c.name === name)
  if (!seed) return null
  const versions = seed.versions.map((version, i): HelmVersion => {
    const h = hash(`${repo}/${name}/${version}`)
    const path = `${name}-${version}.tgz`
    return {
      version,
      app_version: i === 0 ? seed.app : null,
      description: seed.description,
      created_at: new Date(NOW - (i * 23 + (h % 9)) * DAY).toISOString(),
      digest: h.toString(16).padStart(8, '0').repeat(8),
      size_bytes: Math.round(seed.kib * KiB * (0.9 + (h % 20) / 100)),
      cached: i < 2,
      deprecated: false,
      path,
      download_url: `${BASE_URL}/repository/${repo}/${path}`,
    }
  })
  return { chart: chart(seed), versions }
}
