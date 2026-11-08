// SPDX-License-Identifier: GPL-3.0-or-later
// One shared clock for relative times ("5 min. ago"), ticking every 15 s: cells re-render together, not each
// on its own timer.
import { onScopeDispose, ref } from 'vue'

const now = ref(Date.now())
let users = 0
let timer: ReturnType<typeof setInterval> | null = null

export function useNow() {
  users++
  timer ??= setInterval(() => {
    now.value = Date.now()
  }, 15000)
  onScopeDispose(() => {
    users--
    if (users === 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  })
  return now
}
