// SPDX-License-Identifier: GPL-3.0-or-later
// Presentation helpers of the Provisioning page (T-O09): which plan items to show, how to print a changed value.
import type { ProvisioningChange, ProvisioningItem, ProvisioningPlan } from '@/api/types'
import { t } from '@/i18n'

/** Items with something to do first (in plan order), unchanged ones only when asked for. */
export function visibleItems(items: ProvisioningItem[], showUnchanged: boolean): ProvisioningItem[] {
  return items.filter((item) => showUnchanged || item.action !== 'unchanged')
}

/** How many changes applying the plan would make (create + update + prune). */
export function changeCount(plan: ProvisioningPlan | null | undefined): number {
  const s = plan?.summary
  return s ? (s.create ?? 0) + (s.update ?? 0) + (s.prune ?? 0) : 0
}

/** A before/after value as one short line (secrets are never sent: a redacted change says so). */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return t('provisioning.notSet')
  let text: string
  if (typeof value === 'string') text = value === '' ? '""' : value
  else if (typeof value === 'number' || typeof value === 'boolean') text = String(value)
  else text = JSON.stringify(value)
  return text.length > 160 ? `${text.slice(0, 159)}…` : text
}

export function describeChange(change: ProvisioningChange): string {
  if (change.redacted) return `${change.field}: ${t('provisioning.secretChanged')}`
  return `${change.field}: ${formatValue(change.before)} → ${formatValue(change.after)}`
}

export const ACTION_TONE: Record<ProvisioningItem['action'], string> = {
  create: 'tone-ok',
  update: 'tone-hit',
  unchanged: 'tone-offline',
  prune: 'tone-blocked',
}
