// SPDX-License-Identifier: GPL-3.0-or-later
// Playwright suites on the mock build (T-U09): E2E flows, axe accessibility on every page, the visual-regression
// baseline (2 themes x 3 widths) and the UI performance checks (200 live transfers, a 100,000-entry folder). Run with
// `make e2e`; `make e2e-update` rewrites the screenshot baselines (e2e/__screenshots__). Runs in the official
// Playwright image pinned in the Makefile, so fonts and rendering match the committed baselines.
import { defineConfig, devices } from '@playwright/test'

const PORT = Number(process.env.E2E_PORT ?? 4173)

export default defineConfig({
  testDir: 'e2e',
  outputDir: 'test-results',
  snapshotPathTemplate: 'e2e/__screenshots__/{testFileName}/{arg}{ext}',
  fullyParallel: true,
  workers: Number(process.env.E2E_WORKERS ?? 4),
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  expect: {
    timeout: 10_000,
    toHaveScreenshot: { maxDiffPixelRatio: 0.01, animations: 'disabled', caret: 'hide', scale: 'css' },
  },
  use: {
    baseURL: `http://127.0.0.1:${PORT}/ui/`,
    ...devices['Desktop Chrome'],
    viewport: { width: 1440, height: 900 },
    locale: 'en-GB',
    timezoneId: 'UTC',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'ui', testIgnore: /perf\.spec\.ts/ },
    { name: 'perf', testMatch: /perf\.spec\.ts/, dependencies: ['ui'], fullyParallel: false },
  ],
  webServer: {
    command: `PORT=${PORT} node e2e/serve.mjs`,
    url: `http://127.0.0.1:${PORT}/ui/`,
    reuseExistingServer: true,
    timeout: 30_000,
  },
})
