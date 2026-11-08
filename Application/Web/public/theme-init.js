// SPDX-License-Identifier: GPL-3.0-or-later
// First paint in the right theme, before the app boots: the cached preference (stores/theme.ts), else the OS.
;(function () {
  try {
    var mode = localStorage.getItem('ws.theme') || 'system'
    var dark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
    document.documentElement.style.background = dark ? '#1c1c1e' : '#f5f6f8'
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
  } catch (e) {
    // Storage blocked: the app applies the theme once it boots.
  }
})()
