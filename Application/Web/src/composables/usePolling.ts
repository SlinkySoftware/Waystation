// SPDX-License-Identifier: GPL-3.0-or-later
// Poll while something is in progress (a running cleanup or GC job): calls `tick` every `ms` while `active` is
// true, stops when it turns false or the component goes away. Jobs have no live topic yet, so this is the
// SSE fallback UI_DESIGN §4.2 allows.
import { onScopeDispose, type WatchSource, watch } from 'vue'

export function usePolling(active: WatchSource<boolean>, tick: () => Promise<unknown> | void, ms = 2000): void {
  let timer: ReturnType<typeof setTimeout> | null = null
  let running = false

  const stop = () => {
    if (timer) clearTimeout(timer)
    timer = null
    running = false
  }

  const loop = () => {
    timer = setTimeout(async () => {
      if (!running) return
      try {
        await tick()
      } catch {
        // A failed poll is retried on the next tick; the view keeps its last data.
      }
      if (running) loop()
    }, ms)
  }

  watch(
    active,
    (on) => {
      if (on && !running) {
        running = true
        loop()
      } else if (!on) stop()
    },
    { immediate: true },
  )
  onScopeDispose(stop)
}
