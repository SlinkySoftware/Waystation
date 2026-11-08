// SPDX-License-Identifier: GPL-3.0-or-later
// Where live events come from: the SSE endpoint (SPEC §7), or the fake generator in mock mode.
import { API_BASE } from '@/api/client'
import type { LiveEvent } from '@/api/types'

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting'

export interface LiveHandlers {
  onEvent(event: LiveEvent, id: string | null): void
  onStatus(status: ConnectionStatus): void
}

export interface LiveSource {
  open(topics: string[], lastEventId: string | null, handlers: LiveHandlers): void
  close(): void
}

export const LIVE_EVENT_TYPES: readonly LiveEvent['type'][] = [
  'transfers.snapshot',
  'transfer.started',
  'transfer.progress',
  'transfer.finished',
  'transfer.failed',
  'stats.tick',
  'upstream.state',
  'activity',
]

const RETRY_MIN_MS = 1000
const RETRY_MAX_MS = 30000

/**
 * One EventSource for the whole tab. The browser reconnects by itself and sends Last-Event-ID; when it gives up
 * (CLOSED, e.g. after a 5xx), we reopen with backoff and pass the last id as `last_event_id`.
 */
export class EventSourceLiveSource implements LiveSource {
  private source: EventSource | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private retryMs = RETRY_MIN_MS
  private lastId: string | null = null

  open(topics: string[], lastEventId: string | null, handlers: LiveHandlers): void {
    this.close()
    this.lastId = lastEventId
    const params = new URLSearchParams({ topics: topics.join(',') })
    if (this.lastId) params.set('last_event_id', this.lastId)
    handlers.onStatus('connecting')
    const source = new EventSource(`${API_BASE}/events/stream?${params.toString()}`, { withCredentials: true })
    this.source = source
    source.onopen = () => {
      this.retryMs = RETRY_MIN_MS
      handlers.onStatus('open')
    }
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        handlers.onStatus('reconnecting')
        this.timer = setTimeout(() => this.open(topics, this.lastId, handlers), this.retryMs)
        this.retryMs = Math.min(RETRY_MAX_MS, this.retryMs * 2)
      } else {
        handlers.onStatus('reconnecting')
      }
    }
    for (const type of LIVE_EVENT_TYPES) {
      source.addEventListener(type, (message: MessageEvent<string>) => {
        try {
          const event = JSON.parse(message.data) as LiveEvent
          if (message.lastEventId) this.lastId = message.lastEventId
          handlers.onEvent(event, message.lastEventId || null)
        } catch (error) {
          console.warn('Bad live event', error)
        }
      })
    }
  }

  close(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.source?.close()
    this.source = null
  }
}

let factory: () => LiveSource = () => new EventSourceLiveSource()

/** Mock mode and tests swap the source (boot/mock.ts). */
export function setLiveSourceFactory(next: () => LiveSource): void {
  factory = next
}

export function createLiveSource(): LiveSource {
  return factory()
}
