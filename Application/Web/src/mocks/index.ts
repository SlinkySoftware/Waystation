// SPDX-License-Identifier: GPL-3.0-or-later
// Mock mode entry (WAYSTATION_MOCK=1): MSW service worker for /api/v1 + the world as the live source.
import { setupWorker } from 'msw/browser'

import { setLiveSourceFactory } from '@/stores/liveSource'

import { handlers } from './handlers'
import { WorldLiveSource } from './liveSource'

export async function startMocks(): Promise<void> {
  setLiveSourceFactory(() => new WorldLiveSource())
  const worker = setupWorker(...handlers)
  const base = import.meta.env.BASE_URL ?? '/'
  await worker.start({
    serviceWorker: { url: `${base}mockServiceWorker.js`, options: { scope: base } },
    // Only /api is mocked; app modules, fonts and assets go to the dev server untouched.
    onUnhandledFrame: 'bypass',
    quiet: true,
  })
}
