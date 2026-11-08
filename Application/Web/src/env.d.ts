// SPDX-License-Identifier: GPL-3.0-or-later
/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** '1' in mock builds (npm run dev:mock / build:mock), '' otherwise. */
  readonly WAYSTATION_MOCK: string
}
