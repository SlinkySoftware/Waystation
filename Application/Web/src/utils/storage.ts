// SPDX-License-Identifier: GPL-3.0-or-later
// localStorage that never throws (private mode, blocked storage).

export function readString(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeString(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, value)
  } catch {
    // Ignore: preferences just won't persist.
  }
}
