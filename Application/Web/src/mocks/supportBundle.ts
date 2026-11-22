// SPDX-License-Identifier: GPL-3.0-or-later
// Mock support bundle (contract `GET /system/support-bundle`, T-O10): a small but real zip (stored, no compression)
// with the same file names as the server's, so the download can be tried in mock mode. Audited like the server.
import { http, HttpResponse } from 'msw'

import type { AuditActor } from '@/api/types'

import { recordAudit } from './audit'
import type { MockHelpers } from './tokens'

const API = '/api/v1'

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of data) crc = CRC_TABLE[(crc ^ byte) & 0xff]! ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

/** A zip archive of `files` (name → text), stored without compression. */
export function storedZip(files: Record<string, string>): Uint8Array<ArrayBuffer> {
  const encoder = new TextEncoder()
  const locals: Uint8Array[] = []
  const centrals: Uint8Array[] = []
  let offset = 0
  for (const [name, text] of Object.entries(files)) {
    const nameBytes = encoder.encode(name)
    const data = encoder.encode(text)
    const crc = crc32(data)
    const local = new Uint8Array(30 + nameBytes.length + data.length)
    const lv = new DataView(local.buffer)
    lv.setUint32(0, 0x04034b50, true)
    lv.setUint16(4, 20, true) // version needed
    lv.setUint16(8, 0, true) // stored
    lv.setUint32(14, crc, true)
    lv.setUint32(18, data.length, true)
    lv.setUint32(22, data.length, true)
    lv.setUint16(26, nameBytes.length, true)
    local.set(nameBytes, 30)
    local.set(data, 30 + nameBytes.length)
    const central = new Uint8Array(46 + nameBytes.length)
    const cv = new DataView(central.buffer)
    cv.setUint32(0, 0x02014b50, true)
    cv.setUint16(4, 20, true)
    cv.setUint16(6, 20, true)
    cv.setUint32(16, crc, true)
    cv.setUint32(20, data.length, true)
    cv.setUint32(24, data.length, true)
    cv.setUint16(28, nameBytes.length, true)
    cv.setUint32(42, offset, true)
    central.set(nameBytes, 46)
    locals.push(local)
    centrals.push(central)
    offset += local.length
  }
  const centralSize = centrals.reduce((sum, c) => sum + c.length, 0)
  const end = new Uint8Array(22)
  const ev = new DataView(end.buffer)
  ev.setUint32(0, 0x06054b50, true)
  ev.setUint16(8, centrals.length, true)
  ev.setUint16(10, centrals.length, true)
  ev.setUint32(12, centralSize, true)
  ev.setUint32(16, offset, true)
  const out = new Uint8Array(offset + centralSize + end.length)
  let at = 0
  for (const part of [...locals, ...centrals, end]) {
    out.set(part, at)
    at += part.length
  }
  return out
}

/** The mock bundle's files (the server's names; contents are illustrative). */
export function mockBundleFiles(now: Date, caller: string | null): Record<string, string> {
  const json = (value: unknown) => JSON.stringify(value, null, 2)
  return {
    'README.txt': 'Waystation support bundle (mock mode)\n\nREVIEW THESE FILES BEFORE YOU SHARE THEM.\n',
    'manifest.json': json({ generated_at: now.toISOString(), generator: 'Waystation mock', instance_name: 'Waystation', generated_by: caller }),
    'versions.json': json({ waystation: 'mock', python: '3.14', mongodb: { version: '8.0' }, valkey: { version: '8.1' }, workers: [] }),
    'settings.json': json({ environment: { variables: { WAYSTATION_BASE_URL: 'https://registry.example.com', WAYSTATION_MONGO_URI: 'mongodb://<redacted>@waystation-mongodb:27017/?replicaSet=rs0', WAYSTATION_SECRET_KEYS: '<redacted>' }, secrets_from_files: [] }, instance: [] }),
    'repositories.json': json({ count: 0, items: [] }),
    'auth_providers.json': json({ count: 0, items: [] }),
    'health.json': json({ components: [{ name: 'mongodb', status: 'ok' }, { name: 'valkey', status: 'ok' }] }),
    'jobs.json': json({ problem_runs: [], dead_letters: { total: 0, newest: [] } }),
    'errors.json': json({ count: 0, items: [] }),
  }
}

export function supportBundleHandlers(h: MockHelpers) {
  return [
    http.get(`${API}/system/support-bundle`, async ({ request }) => {
      const denied = h.guard(request)
      if (denied) return denied
      await h.latency()
      const now = new Date()
      const caller = h.currentUser()
      const zip = storedZip(mockBundleFiles(now, caller))
      const actor: AuditActor = { kind: 'user', id: null, name: caller ?? 'admin', auth: 'session', provider: 'local', token_id: null }
      recordAudit({ action: 'system.support_bundle', outcome: 'success', actor, target: { kind: 'system', id: 'support-bundle', name: 'Support bundle' }, details: { bytes: zip.length, files: 9 } })
      const stamp = now.toISOString().replace(/[-:]/g, '').slice(0, 15) + 'Z'
      return new HttpResponse(zip, {
        headers: {
          'Content-Type': 'application/zip',
          'Content-Disposition': `attachment; filename="waystation-support-waystation-${stamp}.zip"`,
          'Cache-Control': 'no-store',
        },
      })
    }),
  ]
}
