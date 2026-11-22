// SPDX-License-Identifier: GPL-3.0-or-later
import { getResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'

import type { AuditPage } from '@/api/types'

import { handlers } from './handlers'
import { crc32, storedZip } from './supportBundle'

async function get(path: string): Promise<Response> {
  return (await getResponse(handlers, new Request(new URL(`/api/v1${path}`, location.origin))))!
}

/** The file names of a zip, read from its central directory. */
function zipNames(bytes: Uint8Array): string[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const end = bytes.length - 22
  expect(view.getUint32(end, true)).toBe(0x06054b50)
  const count = view.getUint16(end + 10, true)
  let at = view.getUint32(end + 16, true)
  const names: string[] = []
  for (let i = 0; i < count; i++) {
    expect(view.getUint32(at, true)).toBe(0x02014b50)
    const length = view.getUint16(at + 28, true)
    names.push(new TextDecoder().decode(bytes.subarray(at + 46, at + 46 + length)))
    at += 46 + length
  }
  return names
}

describe('mock support bundle', () => {
  afterEach(() => localStorage.clear())

  it('computes the standard CRC-32', () => {
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
  })

  it('writes a readable zip', () => {
    const zip = storedZip({ 'a.txt': 'hello', 'b.json': '{}' })
    expect(zipNames(zip)).toEqual(['a.txt', 'b.json'])
    expect(new TextDecoder().decode(zip.subarray(30 + 5, 30 + 5 + 5))).toBe('hello')
  })

  it('answers an administrator with the bundle as an attachment and audits it', async () => {
    localStorage.setItem('ws.mock.session', 'admin')
    const response = await get('/system/support-bundle')
    expect(response.status).toBe(200)
    expect(response.headers.get('Content-Type')).toBe('application/zip')
    expect(response.headers.get('Content-Disposition')).toMatch(/^attachment; filename="waystation-support-.+\.zip"$/)
    const names = zipNames(new Uint8Array(await response.arrayBuffer()))
    expect(names).toEqual(expect.arrayContaining(['README.txt', 'manifest.json', 'settings.json', 'errors.json']))
    const audit = (await (await get('/audit?action=system.support_bundle')).json()) as AuditPage
    expect(audit.items[0]?.action).toBe('system.support_bundle')
  })

  it('refuses users without the admin action', async () => {
    localStorage.setItem('ws.mock.session', 'reed')
    expect((await get('/system/support-bundle')).status).toBe(403)
    localStorage.removeItem('ws.mock.session')
    expect((await get('/system/support-bundle')).status).toBe(401)
  })
})
