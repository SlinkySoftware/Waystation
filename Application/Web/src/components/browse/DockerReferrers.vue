<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Referrers of a manifest (UI_DESIGN §4.4, T-U05): signatures, SBOMs and attestations whose OCI `subject` is this
     image (the index and the selected platform's manifest), from `GET /repositories/{name}/docker/referrers`. -->
<template>
  <section class="refs" :aria-labelledby="headingId">
    <div class="refs__head">
      <h3 :id="headingId" class="section-label">{{ t('ui.referrers.title') }}</h3>
      <span v-if="rows.length" class="refs__count">{{ rows.length }}</span>
    </div>
    <q-skeleton v-if="state.loading.value && !state.data.value" height="44px" />
    <div v-else-if="state.error.value" class="refs__empty text-negative">{{ errorMessage(state.error.value) }}</div>
    <div v-else-if="!rows.length" class="refs__empty">{{ t('ui.referrers.empty') }}</div>
    <ul v-else class="refs__list">
      <li v-for="r in rows" :key="r.digest" class="refs__item">
        <span class="ws-badge refs__kind" :class="`refs__kind--${kindOf(r.artifact_type).key}`">
          <q-icon :name="kindOf(r.artifact_type).icon" />{{ t(`ui.referrers.kind.${kindOf(r.artifact_type).key}`) }}
        </span>
        <div class="refs__main">
          <div class="mono refs__type">{{ r.artifact_type || r.media_type }}</div>
          <div class="refs__meta">
            <CopyField :value="r.digest" compact :label="t('browse.docker.digest')" />
            <SizeCell v-if="r.size" :bytes="r.size" />
            <span v-if="r.for !== subject" class="text-faint">{{ t('ui.referrers.forPlatform') }}</span>
            <TimeCell v-if="created(r)" :value="created(r)" />
          </div>
        </div>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed, useId } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { DockerReferrer, Repository } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { iconAttachment, iconPackage, iconSignature, iconVerified } from '@/utils/icons'

import { referrerKind } from './referrers'

const props = defineProps<{ repo: Repository; image: string; subject: string; platformDigest?: string | null }>()
const headingId = useId()

const digests = computed(() => [...new Set([props.subject, props.platformDigest].filter((d): d is string => Boolean(d)))])
const state = useAsync(
  async () => {
    const pages = await Promise.all(
      digests.value.map((digest) => call(api.GET('/repositories/{name}/docker/referrers', { params: { path: { name: props.repo.name }, query: { image: props.image, digest } } }))),
    )
    return pages.flatMap((page) => page.items.map((item) => ({ ...item, for: page.subject })))
  },
  [() => props.repo.name, () => props.image, digests],
)
const rows = computed(() => state.data.value ?? [])

const ICONS = { signature: iconSignature, sbom: iconPackage, attestation: iconVerified, artifact: iconAttachment }
function kindOf(type: string | null | undefined) {
  const key = referrerKind(type)
  return { key, icon: ICONS[key] }
}

function created(r: DockerReferrer): string | null {
  return r.annotations?.['org.opencontainers.image.created'] ?? null
}
</script>

<style scoped>
.refs {
  display: grid;
  gap: 8px;
}

.refs__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.refs__head h3 {
  margin: 0;
}

.refs__count {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.refs__empty {
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.refs__list {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.refs__item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
}

.refs__item:first-child {
  border-top: 0;
}

.refs__kind {
  flex: none;
  min-width: 104px;
  justify-content: center;
  color: var(--text-secondary);
  background: var(--surface-sunken);
}

.refs__kind--signature {
  color: var(--status-ok);
  background: var(--status-ok-bg);
}

.refs__kind--sbom {
  color: var(--cache-hit);
  background: var(--cache-hit-bg);
}

.refs__kind--attestation {
  color: var(--cache-stale);
  background: var(--cache-stale-bg);
}

.refs__main {
  min-width: 0;
  flex: 1;
}

.refs__type {
  font-size: var(--fs-13);
  overflow-wrap: anywhere;
}

.refs__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 14px;
  margin-top: 4px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.refs__meta :deep(.copy-field) {
  max-width: min(420px, 100%);
}
</style>
