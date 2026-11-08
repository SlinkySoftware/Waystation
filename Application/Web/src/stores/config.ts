// SPDX-License-Identifier: GPL-3.0-or-later
// Public instance configuration (name, base URL, docker routing) loaded once at start.
import { acceptHMRUpdate, defineStore } from 'pinia'
import { ref } from 'vue'

import { api, call } from '@/api/client'
import type { UiConfig } from '@/api/types'

export const useConfigStore = defineStore('config', () => {
  const config = ref<UiConfig | null>(null)
  let loading: Promise<void> | null = null

  function load(): Promise<void> {
    loading ??= call(api.GET('/config'))
      .then((value) => {
        config.value = value
      })
      .catch(() => {
        loading = null
      })
    return loading
  }

  return { config, load }
})

if (import.meta.hot) import.meta.hot.accept(acceptHMRUpdate(useConfigStore, import.meta.hot))
