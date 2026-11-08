// SPDX-License-Identifier: GPL-3.0-or-later
// Theme before first render; a 401 mid-session sends the user to sign-in (keeping where they were).
import { defineBoot } from '#q-app'

import { setUnauthorizedHandler } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useConfigStore } from '@/stores/config'
import { useLiveStore } from '@/stores/live'
import { useThemeStore } from '@/stores/theme'

export default defineBoot(({ router }) => {
  useThemeStore().init()
  void useConfigStore().load()
  setUnauthorizedHandler(() => {
    const auth = useAuthStore()
    if (!auth.signedIn) return
    auth.expire()
    useLiveStore().reset()
    const current = router.currentRoute.value
    if (current.name !== 'sign-in') void router.replace({ name: 'sign-in', query: { next: current.fullPath, expired: '1' } })
  })
})
