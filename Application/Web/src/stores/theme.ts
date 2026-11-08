// SPDX-License-Identifier: GPL-3.0-or-later
// Light / dark / system theme (UI_DESIGN §2). localStorage gives the first paint (public/theme-init.js reads the
// same key); the signed-in user's server-side preference wins once loaded.
import { acceptHMRUpdate, defineStore } from 'pinia'
import { Dark } from 'quasar'
import { ref } from 'vue'

import { api, call } from '@/api/client'
import type { Preferences } from '@/api/types'
import { readString, writeString } from '@/utils/storage'

export type ThemeMode = Preferences['theme']
export type Density = Preferences['density']

export const THEME_KEY = 'ws.theme'
export const DENSITY_KEY = 'ws.density'

function parseMode(value: string | null): ThemeMode {
  return value === 'light' || value === 'dark' ? value : 'system'
}

export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(parseMode(readString(THEME_KEY)))
  const density = ref<Density>(readString(DENSITY_KEY) === 'compact' ? 'compact' : 'comfortable')

  function apply(next: ThemeMode): void {
    mode.value = next
    writeString(THEME_KEY, next)
    Dark.set(next === 'system' ? 'auto' : next === 'dark')
    document.documentElement.style.colorScheme = Dark.isActive ? 'dark' : 'light'
  }

  function init(): void {
    apply(mode.value)
  }

  /** Adopt the preferences that came with the session. */
  function adopt(prefs: Preferences): void {
    apply(prefs.theme)
    density.value = prefs.density
    writeString(DENSITY_KEY, prefs.density)
  }

  async function save(next: Partial<Preferences>): Promise<void> {
    if (next.theme) apply(next.theme)
    if (next.density) {
      density.value = next.density
      writeString(DENSITY_KEY, next.density)
    }
    try {
      await call(api.PUT('/auth/me/preferences', { body: { theme: mode.value, density: density.value } }))
    } catch {
      // Local preference still applies; the server copy catches up next time.
    }
  }

  /** Cycle light → dark → system (top-bar toggle). */
  function cycle(): void {
    void save({ theme: mode.value === 'light' ? 'dark' : mode.value === 'dark' ? 'system' : 'light' })
  }

  return { mode, density, init, adopt, save, cycle }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useThemeStore, import.meta.hot))
