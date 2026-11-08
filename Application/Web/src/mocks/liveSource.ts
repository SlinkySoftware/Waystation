// SPDX-License-Identifier: GPL-3.0-or-later
// Mock-mode LiveSource: feeds the live store from the in-browser world instead of the SSE endpoint, with the same
// semantics (snapshot on open, then events; topic `repo:<name>` filters to one repository).
import type { LiveEvent } from '@/api/types'
import type { LiveHandlers, LiveSource } from '@/stores/liveSource'

import { world } from './world'

function repoOf(event: LiveEvent): string | null {
  switch (event.type) {
    case 'transfer.started':
      return event.transfer.repository
    case 'upstream.state':
      return event.health.repository
    case 'activity':
      return event.event.repository ?? null
    default:
      return null
  }
}

export class WorldLiveSource implements LiveSource {
  private off: (() => void) | null = null

  open(topics: string[], _lastEventId: string | null, handlers: LiveHandlers): void {
    this.close()
    handlers.onStatus('connecting')
    const wants = new Set(topics)
    const repos = new Set(topics.filter((t) => t.startsWith('repo:')).map((t) => t.slice(5)))
    const global = wants.has('transfers') || wants.has('stats') || wants.has('activity')
    const known = new Set<string>()
    setTimeout(() => {
      handlers.onStatus('open')
      const snapshot = world.transfers().filter((t) => global || repos.has(t.repository))
      for (const t of snapshot) known.add(t.id)
      handlers.onEvent({ type: 'transfers.snapshot', ts: new Date().toISOString(), transfers: snapshot }, null)
      this.off = world.subscribe((event, id) => {
        const repo = repoOf(event)
        if (event.type === 'transfer.started') {
          if (!wants.has('transfers') && !(repo && repos.has(repo))) return
          known.add(event.transfer.id)
        } else if (event.type.startsWith('transfer.')) {
          if (!('id' in event) || !known.has(event.id)) return
          if (event.type !== 'transfer.progress') known.delete(event.id)
        } else if (event.type === 'stats.tick' && !wants.has('stats')) return
        else if (event.type === 'activity' && !wants.has('activity') && !(repo && repos.has(repo))) return
        handlers.onEvent(event, id)
      })
    }, 120)
  }

  close(): void {
    this.off?.()
    this.off = null
  }
}
