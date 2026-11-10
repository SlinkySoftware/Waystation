// SPDX-License-Identifier: GPL-3.0-or-later
// Mock declarative provisioning (T-O09): a configured file whose plan creates a role, updates a repository's
// description and a webhook's secret (redacted) and leaves the rest unchanged. Applying it records the outcome; the
// next plan is all "unchanged" until the page is reloaded.
import { http, HttpResponse } from 'msw'

import type { ProvisioningItem, ProvisioningLastApply, ProvisioningPlan, ProvisioningStatus, ProvisioningSummary } from '@/api/types'

import type { MockHelpers } from './tokens'

const API = '/api/v1'
const FILE = '/etc/waystation/provisioning.yaml'
const SHA = '6f1c0a9d3be24e8f5a7c91d0e4b2f3a8c6d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7'

const PENDING: ProvisioningItem[] = [
  {
    kind: 'role',
    name: 'docker-pull',
    action: 'create',
    changes: [
      { field: 'name', before: null, after: 'Docker pull', redacted: false },
      { field: 'permissions', before: null, after: [{ action: 'browse', scope: 'format:docker' }, { action: 'read', scope: 'format:docker' }], redacted: false },
      { field: 'managed_by', before: null, after: 'provisioning', redacted: false },
    ],
  },
  { kind: 'cleanup_policy', name: 'stale-proxy-cache', action: 'unchanged', changes: [] },
  {
    kind: 'repository',
    name: 'ghcr-proxy',
    action: 'update',
    changes: [{ field: 'description', before: 'GitHub Container Registry', after: 'GitHub Container Registry (public images)', redacted: false }],
  },
  { kind: 'repository', name: 'dockerhub-proxy', action: 'unchanged', changes: [] },
  { kind: 'repository', name: 'npm', action: 'unchanged', changes: [] },
  { kind: 'service_account', name: 'ci-builder', action: 'unchanged', changes: [] },
  {
    kind: 'webhook',
    name: 'chat-notifications',
    action: 'update',
    changes: [{ field: 'secret', before: '***', after: '***', redacted: true }],
  },
  { kind: 'settings', name: 'general', action: 'unchanged', changes: [] },
]

let applied = false
let lastApply: ProvisioningLastApply = {
  at: new Date(Date.now() - 3 * 3_600_000).toISOString(),
  by: 'init',
  sha256: SHA,
  outcome: 'applied',
  summary: { create: 0, update: 0, unchanged: 7, prune: 0, failed: 0 },
}

function summary(items: ProvisioningItem[]): ProvisioningSummary {
  const out: Required<ProvisioningSummary> = { create: 0, update: 0, unchanged: 0, prune: 0, failed: 0 }
  for (const item of items) {
    out[item.action] += 1
    if (item.outcome === 'failed') out.failed += 1
  }
  return out
}

function currentPlan(): ProvisioningPlan {
  const items = applied ? PENDING.map((i) => ({ ...i, action: 'unchanged' as const, changes: [] })) : PENDING.map((i) => ({ ...i }))
  return { file: FILE, sha256: SHA, valid: true, applied: false, errors: [], items, summary: summary(items) }
}

export function provisioningHandlers(h: MockHelpers) {
  return [
    http.get(`${API}/provisioning`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return HttpResponse.json<ProvisioningStatus>({ configured: true, file: FILE, last_apply: lastApply })
    }),
    http.post(`${API}/provisioning/plan`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      return HttpResponse.json<ProvisioningPlan>(currentPlan())
    }),
    http.post(`${API}/provisioning/apply`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const plan = currentPlan()
      const items = plan.items?.map((i) => (i.action === 'unchanged' ? i : { ...i, outcome: 'applied' as const })) ?? []
      applied = true
      lastApply = { at: new Date().toISOString(), by: h.currentUser() ?? 'admin', sha256: SHA, outcome: 'applied', summary: summary(items) }
      return HttpResponse.json<ProvisioningPlan>({ ...plan, applied: true, items, summary: summary(items) })
    }),
  ]
}
