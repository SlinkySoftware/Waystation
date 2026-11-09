// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Webhook } from '@/api/types'

import { compactEvents, deliveryMeta, deliveryResult, describeEvents, describeScope, expandEvents, generateSecret, headerProblem, prettyJson, rowsToHeaders, toWrite, urlProblem } from './webhooks'

describe('webhook events', () => {
  it('expands patterns and compacts selections', () => {
    expect([...expandEvents(['*'])]).toHaveLength(6)
    expect([...expandEvents(['content.*'])]).toEqual(['content.upload', 'content.delete', 'content.update'])
    expect([...expandEvents(['repository.delete', 'nonsense'])]).toEqual(['repository.delete'])
    expect(compactEvents(['content.upload', 'content.delete', 'content.update', 'repository.create'])).toEqual(['content.*', 'repository.create'])
    expect(compactEvents([...expandEvents(['*'])])).toEqual(['*'])
    expect(compactEvents([])).toEqual([])
  })

  it('describes a selection in words', () => {
    expect(describeEvents(['*'])).toBe('All events')
    expect(describeEvents(['content.*'])).toBe('All Content events')
    expect(describeEvents(['content.upload', 'repository.delete'])).toBe('Content: upload · Repository: delete')
    expect(describeScope([])).toBe('All repositories')
    expect(describeScope(['docker-*', 'npm'])).toBe('docker-*, npm')
  })
})

describe('webhook form helpers', () => {
  it('checks URLs and headers like the server', () => {
    expect(urlProblem('https://ci.example.com/hook')).toBeNull()
    expect(urlProblem('ftp://ci.example.com/')).not.toBeNull()
    expect(urlProblem('https://u:p@ci.example.com/')).not.toBeNull()
    expect(urlProblem('not a url')).not.toBeNull()
    expect(headerProblem('Authorization', 'Bearer x')).toBeNull()
    expect(headerProblem('Waystation-Event', 'x')).not.toBeNull()
    expect(headerProblem('Content-Type', 'x')).not.toBeNull()
    expect(headerProblem('Bad Name', 'x')).not.toBeNull()
    expect(headerProblem('X-A', 'a\nb')).not.toBeNull()
    expect(rowsToHeaders([{ name: ' X-A ', value: '1' }, { name: '', value: 'ignored' }])).toEqual({ 'X-A': '1' })
  })

  it('generates long random secrets', () => {
    const a = generateSecret()
    expect(a).toMatch(/^[0-9a-f]{64}$/)
    expect(generateSecret()).not.toBe(a)
  })

  it('keeps the stored secret and headers when toggling', () => {
    const hook = { id: 'w', name: 'n', description: '', url: 'https://x.example.com/', events: ['*'], repositories: [], enabled: true, secret_set: true, header_names: ['A'], allow_private: false, timeout_s: 10, max_attempts: 8, created_at: '', updated_at: '', last_delivery: null } satisfies Webhook
    expect(toWrite(hook, { enabled: false })).toMatchObject({ enabled: false, secret: null, headers: null, events: ['*'] })
  })
})

describe('delivery display', () => {
  it('labels statuses and results', () => {
    expect(deliveryMeta('succeeded').label).toBe('Delivered')
    expect(deliveryMeta('pending', 0).label).toBe('Queued')
    expect(deliveryMeta('pending', 2).label).toBe('Retrying')
    expect(deliveryMeta('dead').tone).toBe('tone-blocked')
    expect(deliveryResult({ attempts: 0, status_code: null, latency_ms: null, error: null })).toBe('Not attempted yet')
    expect(deliveryResult({ attempts: 1, status_code: 200, latency_ms: 84.4, error: null })).toBe('HTTP 200 · 84 ms')
    expect(deliveryResult({ attempts: 3, status_code: null, latency_ms: 10001, error: 'timed out after 10 s' })).toBe('timed out after 10 s')
    expect(prettyJson('{"a":1}')).toBe('{\n  "a": 1\n}')
    expect(prettyJson('not json')).toBe('not json')
  })
})
