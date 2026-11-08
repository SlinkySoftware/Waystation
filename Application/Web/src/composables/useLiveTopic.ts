// SPDX-License-Identifier: GPL-3.0-or-later
// Subscribe a component to live topics for as long as it is mounted (UI_DESIGN §7 `useLiveStream(topic)`).
import { onScopeDispose, toValue, watch, type MaybeRefOrGetter } from 'vue'

import { useLiveStore } from '@/stores/live'

export function useLiveTopic(topics: MaybeRefOrGetter<string[]>): void {
  const live = useLiveStore()
  let offs: (() => void)[] = []
  const release = () => {
    for (const off of offs) off()
    offs = []
  }
  watch(
    () => toValue(topics).join(','),
    () => {
      const next = toValue(topics).map((topic) => live.subscribe(topic))
      release()
      offs = next
    },
    { immediate: true },
  )
  onScopeDispose(release)
}
