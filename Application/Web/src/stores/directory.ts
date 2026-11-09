// SPDX-License-Identifier: GPL-3.0-or-later
// Reference data for the administration pages: the grantable roles (names, permissions) and the sign-in providers
// (to name a user's source). Loaded once, refreshed after an edit.
import { acceptHMRUpdate, defineStore } from 'pinia'
import { computed, shallowRef } from 'vue'

import { api, call } from '@/api/client'
import type { Role } from '@/api/types'
import { parseSource } from '@/utils/rbac'

import { useAuthStore } from './auth'

export interface ProviderName {
  id: string
  kind: string
  name: string
}

export const useDirectoryStore = defineStore('directory', () => {
  const roles = shallowRef<Role[]>([])
  const providers = shallowRef<ProviderName[]>([])
  let rolesLoading: Promise<void> | null = null
  let providersLoading: Promise<void> | null = null

  const roleById = computed(() => new Map(roles.value.map((r) => [r.id, r])))
  const providerById = computed(() => new Map(providers.value.map((p) => [p.id, p])))

  function loadRoles(force = false): Promise<void> {
    if (rolesLoading && !force) return rolesLoading
    rolesLoading = call(api.GET('/roles'))
      .then((list) => {
        roles.value = list
      })
      .catch((error: unknown) => {
        rolesLoading = null
        throw error
      })
    return rolesLoading
  }

  /** Every configured provider for settings administrators; otherwise the enabled ones (public list). */
  function loadProviders(force = false): Promise<void> {
    if (providersLoading && !force) return providersLoading
    const full = useAuthStore().can('settings.admin')
    const request = full
      ? call(api.GET('/auth-providers')).then((list) => list.map((p) => ({ id: p.id, kind: p.kind, name: p.name })))
      : call(api.GET('/auth/providers')).then((list) => list.filter((p) => p.kind !== 'local').map((p) => ({ id: p.id, kind: p.kind, name: p.name })))
    providersLoading = request
      .then((list) => {
        providers.value = list
      })
      .catch(() => {
        providersLoading = null // names fall back to ids
      })
    return providersLoading
  }

  function roleName(id: string): string {
    return roleById.value.get(id)?.name ?? id
  }

  /** The provider's name for `ldap:<id>` / `oidc:<id>`, null for local accounts. */
  function providerName(source: string): string | null {
    const { provider } = parseSource(source)
    if (!provider) return null
    return providerById.value.get(provider)?.name ?? provider
  }

  return { roles, providers, roleById, loadRoles, loadProviders, roleName, providerName }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useDirectoryStore, import.meta.hot))
