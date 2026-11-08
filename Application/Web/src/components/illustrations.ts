// SPDX-License-Identifier: GPL-3.0-or-later
// Empty-state illustrations (UI_DESIGN §3, §8), drawn by EmptyIllustration.vue.

export const ILLUSTRATIONS = ['repositories', 'cache', 'search', 'activity', 'error', 'notFound', 'soon'] as const

export type IllustrationName = (typeof ILLUSTRATIONS)[number]
