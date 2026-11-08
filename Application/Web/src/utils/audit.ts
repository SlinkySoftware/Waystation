// SPDX-License-Identifier: GPL-3.0-or-later
// Audit vocabulary (SPEC §4.11): action catalog by category, labels and icons. Actions are `<category>.<verb>`;
// unknown actions (a newer server) fall back to their raw name.
import type { AuditActor, AuditRecord } from '@/api/types'
import { type MessageKey, t } from '@/i18n'
import {
  iconAuthProviders,
  iconCleanup,
  iconGroups,
  iconPackage,
  iconPerson,
  iconProfile,
  iconRepositories,
  iconRoles,
  iconSettings,
  iconSystem,
  iconToken,
} from '@/utils/icons'

export const AUDIT_CATEGORIES = ['auth', 'session', 'token', 'service_account', 'user', 'group', 'role', 'auth_provider', 'repository', 'content', 'cleanup', 'settings'] as const
export type AuditCategory = (typeof AUDIT_CATEGORIES)[number]

/** Every action the server records (or will record: see the server's audit catalog). */
export const AUDIT_ACTIONS: Record<AuditCategory, readonly string[]> = {
  auth: ['login', 'logout', 'lockout', 'password_change'],
  session: ['revoke'],
  token: ['create', 'revoke', 'use_refused'],
  service_account: ['create', 'update', 'delete'],
  user: ['create', 'link', 'groups_sync', 'update', 'delete'],
  group: ['create', 'update', 'delete'],
  role: ['create', 'update', 'delete'],
  auth_provider: ['create', 'update', 'delete', 'test'],
  repository: ['create', 'update', 'delete', 'test_upstream'],
  content: ['upload', 'delete'],
  cleanup: ['run'],
  settings: ['update'],
}

export const ALL_AUDIT_ACTIONS: string[] = AUDIT_CATEGORIES.flatMap((c) => AUDIT_ACTIONS[c].map((v) => `${c}.${v}`))

const CATEGORY_ICONS: Record<AuditCategory, string> = {
  auth: iconProfile,
  session: iconProfile,
  token: iconToken,
  service_account: iconSystem,
  user: iconPerson,
  group: iconGroups,
  role: iconRoles,
  auth_provider: iconAuthProviders,
  repository: iconRepositories,
  content: iconPackage,
  cleanup: iconCleanup,
  settings: iconSettings,
}

function isCategory(value: string): value is AuditCategory {
  return (AUDIT_CATEGORIES as readonly string[]).includes(value)
}

/** Translate a dynamic key, or return `fallback` when the catalogue has no entry. */
function tOr(key: string, fallback: string): string {
  const text = t(key as MessageKey)
  return text === key ? fallback : text
}

export function auditCategory(action: string): string {
  return action.split('.', 1)[0] ?? action
}

export function auditCategoryLabel(category: string): string {
  return tOr(`audit.category.${category}`, category)
}

export function auditCategoryIcon(action: string): string {
  const category = auditCategory(action)
  return isCategory(category) ? CATEGORY_ICONS[category] : iconSettings
}

/** Human label; failures use their own wording where it differs (`auth.login` → "Sign-in failed"). */
export function auditActionLabel(action: string, outcome?: AuditRecord['outcome']): string {
  const base = tOr(`audit.action.${action}`, action)
  return outcome === 'failure' ? tOr(`audit.failed.${action}`, base) : base
}

export function auditReasonLabel(reason: string | null): string {
  return reason ? tOr(`audit.reason.${reason}`, reason) : ''
}

export function auditTargetKindLabel(kind: string): string {
  return tOr(`audit.targetKind.${kind}`, kind)
}

export function auditAuthLabel(auth: AuditActor['auth']): string {
  return tOr(`audit.auth.${auth}`, auth)
}

/** `ldap:01JC…` → `LDAP`, `local` → `Local`. */
export function providerLabel(provider: string | null): string | null {
  if (!provider) return null
  const kind = provider.split(':', 1)[0] ?? provider
  return tOr(`audit.provider.${kind}`, kind)
}

/** A plain JSON value for display: strings as-is, everything else JSON-encoded; null/undefined as a dash. */
export function auditValue(value: unknown): string {
  if (value === null || value === undefined) return '–'
  if (typeof value === 'string') return value
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : JSON.stringify(value)
}

export function auditTargetText(record: AuditRecord): string {
  const target = record.target
  if (!target) return ''
  return target.name ?? target.id ?? ''
}
