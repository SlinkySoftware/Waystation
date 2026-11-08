// SPDX-License-Identifier: GPL-3.0-or-later
// A route query parameter as a ref: browse state lives in the URL (shareable, back button works).
import { computed, type WritableComputedRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'

export function useQueryParam(key: string): WritableComputedRef<string | null> {
  const route = useRoute()
  const router = useRouter()
  return computed({
    get: () => {
      const v = route.query[key]
      return typeof v === 'string' && v !== '' ? v : null
    },
    set: (value) => {
      const query = { ...route.query }
      if (value === null || value === '') delete query[key]
      else query[key] = value
      void router.push({ query })
    },
  })
}

/** Set several query parameters in one navigation (null removes). */
export function useQueryNavigate() {
  const route = useRoute()
  const router = useRouter()
  return (changes: Record<string, string | null>, replace = false) => {
    const query = { ...route.query }
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === '') delete query[k]
      else query[k] = v
    }
    void (replace ? router.replace({ query }) : router.push({ query }))
  }
}
