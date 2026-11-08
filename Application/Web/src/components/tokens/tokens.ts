// SPDX-License-Identifier: GPL-3.0-or-later
// API token presentation helpers: status badges, scope labels, expiry presets.
import type { ApiToken, TokenScope } from '@/api/types'
import { t } from '@/i18n'
import { iconBlocked, iconOk, iconTimer } from '@/utils/icons'

export const SCOPES: TokenScope[] = ['read', 'write', 'delete', 'manage', 'stats-read', 'metrics', 'admin']

export function scopeLabel(scope: TokenScope): string {
  return t(`tokens.scope.${scope}`)
}

export function scopeHelp(scope: TokenScope): string {
  return t(`tokens.scopeHelp.${scope}`)
}

export function tokenStatusMeta(status: ApiToken['status']): { label: string; icon: string; tone: string } {
  switch (status) {
    case 'active':
      return { label: t('tokens.status.active'), icon: iconOk, tone: 'tone-ok' }
    case 'expired':
      return { label: t('tokens.status.expired'), icon: iconTimer, tone: 'tone-offline' }
    case 'revoked':
      return { label: t('tokens.status.revoked'), icon: iconBlocked, tone: 'tone-blocked' }
  }
}

export type ExpiryChoice = '7' | '30' | '90' | '365' | 'custom' | 'never'

export const EXPIRY_CHOICES: { value: ExpiryChoice; label: string }[] = [
  { value: '7', label: t('tokens.form.expiryDays', { count: 7 }) },
  { value: '30', label: t('tokens.form.expiryDays', { count: 30 }) },
  { value: '90', label: t('tokens.form.expiryDays', { count: 90 }) },
  { value: '365', label: t('tokens.form.expiryYear') },
  { value: 'custom', label: t('tokens.form.expiryCustom') },
  { value: 'never', label: t('tokens.form.expiryNever') },
]

/** `YYYY/MM/DD` (q-date model) for a date `days` from `now`. */
export function dateModel(days: number, now: Date = new Date()): string {
  const d = new Date(now.getTime() + days * 86_400_000)
  return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`
}

/** The `expires_at` to send: preset days from now, the end of a custom day (local time), or null for never. */
export function expiresAt(choice: ExpiryChoice, customDate: string | null, now: Date = new Date()): string | null {
  if (choice === 'never') return null
  if (choice === 'custom') {
    if (!customDate) return null
    const [y, m, d] = customDate.split('/').map(Number)
    return new Date(y!, m! - 1, d!, 23, 59, 59).toISOString()
  }
  return new Date(now.getTime() + Number(choice) * 86_400_000).toISOString()
}

/** Custom dates must be after today. */
export function isFutureDate(customDate: string, now: Date = new Date()): boolean {
  return customDate > dateModel(0, now)
}
