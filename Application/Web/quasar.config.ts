// SPDX-License-Identifier: GPL-3.0-or-later
// https://v2.quasar.dev/quasar-cli-vite/quasar-config-file

import { rmSync } from 'node:fs'
import { join } from 'node:path'

import { defineConfig } from '#q-app'

// WAYSTATION_MOCK=1 runs the whole UI on MSW handlers and the fake live-event generator (npm run dev:mock).
const mock = process.env.WAYSTATION_MOCK === '1'
// The API is same-origin under /api/v1: the web tier proxies it in production, the dev server here.
const apiTarget = process.env.WAYSTATION_DEV_API_TARGET || 'http://localhost:8000'

export default defineConfig(() => ({
  boot: ['fonts', 'mock', 'session'],
  css: ['app.css'],
  extras: [],

  build: {
    target: { browser: ['es2022', 'firefox115', 'chrome115', 'safari15'] },
    vueRouterMode: 'history',
    // The UI lives under /ui/ (SPEC §4.4); the web image serves dist/spa there.
    publicPath: '/ui/',
    vueOptionsAPI: false,
    typescript: { strict: true, vueShim: true },
    // import.meta.env.WAYSTATION_MOCK: a build-time constant, so mock code is dropped from normal builds.
    defineEnv: { WAYSTATION_MOCK: mock ? '1' : '' },
    afterBuild: ({ quasarConf }) => {
      // The MSW service worker only belongs in mock builds.
      if (!mock) rmSync(join(quasarConf.build?.distDir ?? 'dist/spa', 'mockServiceWorker.js'), { force: true })
    },
  },

  devServer: {
    port: 9000,
    host: '0.0.0.0',
    open: false,
    allowedHosts: true,
    proxy: mock ? undefined : { '/api': { target: apiTarget, changeOrigin: false } },
  },

  framework: {
    config: { dark: false },
    iconSet: 'svg-material-symbols-outlined',
    plugins: ['Notify', 'Dialog', 'Dark'],
  },
}))
