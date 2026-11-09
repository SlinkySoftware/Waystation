// SPDX-License-Identifier: GPL-3.0-or-later
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { CleanupPolicy } from '@/api/types'

import {
  compatible,
  criteriaChips,
  cronError,
  describeSchedule,
  emptyCriteria,
  hasSelector,
  normaliseCriteria,
  progressRatio,
  runOutcome,
  runSeconds,
  SCHEDULE_PRESETS,
  scheduleMode,
  toWrite,
} from './cleanup'
import CriteriaChips from './CriteriaChips.vue'
import LastRun from './LastRun.vue'
import RunStatusPill from './RunStatusPill.vue'

const policy: CleanupPolicy = {
  id: 'cp_1',
  name: 'docker-keep-latest-5',
  description: 'Keep five',
  format: 'docker',
  criteria: { ...emptyCriteria(), keep_latest_n_versions: 5, last_downloaded_older_than_days: 60, name_exclude: '^library/postgres$' },
  schedule: '0 3 * * 0',
  enabled: true,
  repositories: ['dockerhub-proxy'],
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  next_run_at: null,
  last_run: null,
}

describe('criteria', () => {
  it('needs at least one selecting criterion; protections alone select nothing', () => {
    expect(hasSelector(emptyCriteria())).toBe(false)
    expect(hasSelector({ ...emptyCriteria(), name_exclude: '^keep', version_exclude: 'lts' })).toBe(false)
    expect(hasSelector({ ...emptyCriteria(), name_include: '   ' })).toBe(false)
    expect(hasSelector({ ...emptyCriteria(), keep_latest_n_versions: 3 })).toBe(true)
    expect(hasSelector({ ...emptyCriteria(), prerelease_only: true })).toBe(true)
    expect(hasSelector({ ...emptyCriteria(), untagged_older_than_days: 14 })).toBe(true)
    expect(hasSelector({ ...emptyCriteria(), version_include: '^nightly' })).toBe(true)
  })

  it('normalises blanks to null and drops the docker-only criterion for other formats', () => {
    const raw = { ...emptyCriteria(), name_include: '  ^@types/ ', version_exclude: '', untagged_older_than_days: 14, published_older_than_days: 30.4 }
    const npm = normaliseCriteria(raw, 'npm')
    expect(npm.name_include).toBe('^@types/')
    expect(npm.version_exclude).toBeNull()
    expect(npm.untagged_older_than_days).toBeNull()
    expect(npm.published_older_than_days).toBe(30)
    expect(normaliseCriteria(raw, 'docker').untagged_older_than_days).toBe(14)
    expect(normaliseCriteria(raw, '*').untagged_older_than_days).toBe(14)
  })

  it('summarises criteria as chips, selecting first, protections marked', () => {
    const chips = criteriaChips(policy.criteria)
    expect(chips.map((c) => c.label)).toEqual(['Unused 60 d', 'Keep newest 5', 'Protect name /^library/postgres$/'])
    expect(chips.filter((c) => c.protects).map((c) => c.key)).toEqual(['keep_latest_n_versions', 'name_exclude'])
    expect(criteriaChips({ ...emptyCriteria(), untagged_older_than_days: 14, prerelease_only: true }).map((c) => c.label)).toEqual(['Untagged > 14 d', 'Pre-releases'])
  })

  it('renders chips as text, never as HTML', () => {
    const w = mount(CriteriaChips, { props: { criteria: { ...emptyCriteria(), name_include: '<img src=x>' } } })
    expect(w.text()).toContain('<img src=x>')
    expect(w.find('img').exists()).toBe(false)
  })
})

describe('schedules', () => {
  it('maps presets to modes and back', () => {
    expect(scheduleMode(null)).toBe('manual')
    expect(scheduleMode(SCHEDULE_PRESETS.daily)).toBe('daily')
    expect(scheduleMode(SCHEDULE_PRESETS.weekly)).toBe('weekly')
    expect(scheduleMode('*/15 * * * *')).toBe('custom')
    expect(scheduleMode('@daily')).toBe('custom')
  })

  it('describes common schedules in words', () => {
    expect(describeSchedule(null)).toBe('Manual only')
    expect(describeSchedule('0 3 * * *')).toBe('Daily at 03:00 UTC')
    expect(describeSchedule('30 4 * * 0')).toBe('Weekly on Sun at 04:30 UTC')
    expect(describeSchedule('0 4 1 * *')).toBe('Monthly on day 1 at 04:00 UTC')
    expect(describeSchedule('@weekly')).toBe('Weekly on Sun at 00:00 UTC')
    expect(describeSchedule('*/15 * * * *')).toBe('Cron */15 * * * * (UTC)')
  })

  it('checks cron syntax', () => {
    expect(cronError('0 3 * * *')).toBeNull()
    expect(cronError('*/10 1-5 * * 1,3,5')).toBeNull()
    expect(cronError('@daily')).toBeNull()
    expect(cronError('')).toMatch(/Enter/)
    expect(cronError('@fortnightly')).toMatch(/macro/)
    expect(cronError('0 3 * *')).toMatch(/five fields/)
    expect(cronError('0 3 * * mon')).toMatch(/not valid/)
  })
})

describe('runs and policies', () => {
  it('computes progress and duration', () => {
    expect(progressRatio({ done: 5, total: 20 })).toBe(0.25)
    expect(progressRatio({ done: 5, total: null })).toBeNull()
    expect(progressRatio({ done: 30, total: 20 })).toBe(1)
    const started = '2026-11-09T10:00:00Z'
    expect(runSeconds({ started_at: started, finished_at: '2026-11-09T10:01:30Z' })).toBe(90)
    expect(runSeconds({ started_at: started, finished_at: null }, Date.parse(started) + 5000)).toBe(5)
  })

  it('attaches only to the same format or from an all-formats policy', () => {
    expect(compatible({ format: 'docker' }, 'docker')).toBe(true)
    expect(compatible({ format: '*' }, 'apt')).toBe(true)
    expect(compatible({ format: 'npm' }, 'docker')).toBe(false)
  })

  it('builds the write body for a toggle without losing criteria', () => {
    const body = toWrite(policy, { enabled: false })
    expect(body).toMatchObject({ name: policy.name, format: 'docker', schedule: '0 3 * * 0', enabled: false })
    expect(body.criteria.keep_latest_n_versions).toBe(5)
    expect('repositories' in body).toBe(false)
  })

  it('says what a run removed in words', () => {
    expect(runOutcome({ removed: 0, bytes: 0 })).toBe('Nothing removed')
    expect(runOutcome({ removed: 1, bytes: 2048 })).toBe('1 item · 2.00 KiB removed')
    expect(runOutcome({ removed: 12, bytes: 3 * 1024 * 1024 })).toMatch(/^12 items · 3(\.0+)? MiB removed$/)
  })

  it('shows a run that removed nothing without a bare "0 B · 0"', () => {
    const run = { id: 'r1', status: 'succeeded', trigger: 'manual', started_at: '2026-11-09T10:00:00Z', finished_at: '2026-11-09T10:00:01Z', removed: 0, bytes: 0 }
    const text = mount(LastRun, { props: { run: run as never } }).text()
    expect(text).toContain('Nothing removed')
    expect(text).not.toContain('0 B')
  })

  it('shows run status with an icon and a label', () => {
    const w = mount(RunStatusPill, { props: { status: 'failed', tooltip: false } })
    expect(w.text()).toBe('Failed')
    expect(w.classes()).toContain('tone-blocked')
    expect(w.find('.q-icon').exists()).toBe(true)
    expect(mount(RunStatusPill, { props: { status: 'running', tooltip: false } }).classes()).toContain('run-pill--running')
  })
})
