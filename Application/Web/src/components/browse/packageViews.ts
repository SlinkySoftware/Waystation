// SPDX-License-Identifier: GPL-3.0-or-later
// Formats with a package view (the repository's Packages tab, `RepoBrowser.vue`); every format also has the generic
// two-pane file tree (Browse tab). Keep in step with RepoBrowser.vue.
import type { Format } from '@/api/types'

export const PACKAGE_VIEW_FORMATS: readonly Format[] = ['docker', 'npm', 'apt', 'helm', 'galaxy', 'maven', 'pypi', 'go', 'yum', 'terraform', 'swift', 'nuget', 'composer', 'cargo', 'rubygems', 'apk']

export function hasPackageView(format: Format): boolean {
  return PACKAGE_VIEW_FORMATS.includes(format)
}

/** The tab that shows a repository's contents best: its package view, else the file tree. */
export function contentTab(format: Format): 'packages' | 'browse' {
  return hasPackageView(format) ? 'packages' : 'browse'
}
