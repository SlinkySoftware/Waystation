// SPDX-License-Identifier: GPL-3.0-or-later
// Shared helpers for the Playwright suites on the mock build: a signed-in mock session without the form, the mock
// knobs (no live generator for still screenshots; a load of live transfers for the frame-rate check), a seeded
// Math.random and a fixed clock so pages render the same on every run.
import { expect, type Page } from '@playwright/test'

/** 2026-11-20 09:30 UTC: every relative time ("3 min ago") is computed from here in still mode. */
export const FIXED_NOW = Date.UTC(2026, 10, 20, 9, 30, 0)

export interface Prepare {
  /** Mock user (any name is an administrator, except nina/reed/gus/grace: SPEC §4.10 smoke users). Null = signed out. */
  user?: string | null
  /** No live generator, seeded randomness, fixed clock: deterministic screenshots. */
  still?: boolean
  /** Keep about this many live transfers in flight. */
  load?: number
  density?: 'comfortable' | 'compact'
}

export async function prepare(page: Page, opts: Prepare = {}): Promise<void> {
  const { user = 'admin', still = false, load = 0, density } = opts
  if (still) await page.clock.setFixedTime(FIXED_NOW)
  await page.addInitScript(
    ({ user, still, load, density }) => {
      if (user) localStorage.setItem('ws.mock.session', user)
      else localStorage.removeItem('ws.mock.session')
      if (still) localStorage.setItem('ws.mock.still', '1')
      if (load) localStorage.setItem('ws.mock.load', String(load))
      if (density) localStorage.setItem('ws.density', density)
      if (still) {
        // mulberry32: the mock fixtures and charts draw the same numbers on every run.
        let seed = 0x5eed
        Math.random = () => {
          seed = (seed + 0x6d2b79f5) | 0
          let t = seed
          t = Math.imul(t ^ (t >>> 15), t | 1)
          t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
          return ((t ^ (t >>> 14)) >>> 0) / 4294967296
        }
      }
    },
    { user, still, load, density },
  )
}

/** Open an app path (relative to /ui/) and wait until the page has drawn its data. */
export async function open(page: Page, path: string): Promise<void> {
  await page.goto(path, { waitUntil: 'networkidle' })
  await expect(page.locator('.q-skeleton')).toHaveCount(0, { timeout: 15_000 })
  await page.evaluate(() => document.fonts.ready)
}

/** Fail the test on console errors and page errors (registered before navigation). */
export function watchConsole(page: Page): string[] {
  const problems: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') problems.push(`console error: ${msg.text()}`)
  })
  page.on('pageerror', (error) => problems.push(`page error: ${error.message}`))
  return problems
}

/** Every page an administrator can open on the mock build (path relative to /ui/). */
export const PAGES: { name: string; path: string }[] = [
  { name: 'dashboard', path: '' },
  { name: 'repositories', path: 'repositories' },
  { name: 'repositories-grid', path: 'repositories?view=grid' },
  { name: 'repo-overview', path: 'repositories/dockerhub-proxy' },
  { name: 'repo-packages', path: 'repositories/dockerhub-proxy/packages?image=library%2Fnginx' },
  { name: 'repo-manifest', path: 'repositories/dockerhub-proxy/packages?image=library%2Fnginx&ref=1.29' },
  { name: 'repo-npm-package', path: 'repositories/npm/packages?package=vue' },
  { name: 'repo-browse', path: 'repositories/raw-releases/browse?path=cli%2Fcli%2Freleases%2Fdownload%2Fv2.83.0' },
  { name: 'repo-activity', path: 'repositories/debian/activity' },
  { name: 'repo-cleanup', path: 'repositories/dockerhub-proxy/cleanup' },
  { name: 'repo-members', path: 'repositories/npm/members' },
  { name: 'repo-settings', path: 'repositories/npm/settings' },
  { name: 'browse', path: 'browse' },
  { name: 'search', path: 'search?q=nginx' },
  { name: 'activity', path: 'activity' },
  { name: 'storage', path: 'storage' },
  { name: 'cleanup', path: 'cleanup' },
  { name: 'tasks', path: 'tasks' },
  { name: 'admin-users', path: 'admin/users' },
  { name: 'admin-groups', path: 'admin/groups' },
  { name: 'admin-roles', path: 'admin/roles' },
  { name: 'admin-auth', path: 'admin/auth-providers' },
  { name: 'admin-tokens', path: 'admin/tokens' },
  { name: 'admin-audit', path: 'admin/audit' },
  { name: 'admin-webhooks', path: 'admin/webhooks' },
  { name: 'admin-settings', path: 'admin/settings' },
  { name: 'admin-provisioning', path: 'admin/provisioning' },
  { name: 'admin-system', path: 'admin/system' },
  { name: 'import-nexus', path: 'repositories/import/nexus' },
  { name: 'profile', path: 'profile' },
  { name: 'showcase', path: 'dev/showcase' },
  { name: 'not-found', path: 'no/such/page' },
]
