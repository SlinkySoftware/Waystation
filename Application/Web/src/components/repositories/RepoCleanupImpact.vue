<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- What the attached policies would remove from this repository now: one totals-only preview per policy scoped
     to this repository (`repositories: [name]`, `limit: 0`), each with the full candidate list one click away. -->
<template>
  <PageCard :title="t('cleanup.repo.impactTitle', { name: repo.name })" :caption="t('cleanup.repo.impactCaption')" flush>
    <template #actions>
      <span v-if="total !== null" class="impact-repo__total">{{ t('cleanup.repo.total', { size: formatBytes(total) }) }}</span>
      <q-btn flat round dense :icon="iconRefresh" :loading="loading" :aria-label="t('common.retry')" @click="load" />
    </template>
    <ul class="impact-repo">
      <li v-for="policy in policies" :key="policy.id" class="impact-repo__row">
        <span class="impact-repo__name">{{ policy.name }}</span>
        <span class="impact-repo__value">
          <template v-if="results[policy.id]?.error">
            <span class="impact-repo__error">{{ errorMessage(results[policy.id]!.error) }}</span>
          </template>
          <template v-else-if="results[policy.id]?.totals">
            {{ t('cleanup.impact.count', { count: results[policy.id]!.totals!.count, n: formatCount(results[policy.id]!.totals!.count) }) }}
            · <SizeCell :bytes="results[policy.id]!.totals!.bytes" />
          </template>
          <q-skeleton v-else type="text" width="140px" />
        </span>
        <q-btn
          flat
          dense
          no-caps
          color="primary"
          :icon="iconShow"
          :label="t('cleanup.action.preview')"
          :disable="!results[policy.id]?.totals?.count"
          @click="review(policy)"
        />
      </li>
    </ul>
    <PreviewDialog v-model="reviewing" :request="request" :title="reviewTitle" />
  </PageCard>
</template>

<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { CleanupPolicy, CleanupPreview, Repository } from '@/api/types'
import { normaliseCriteria } from '@/components/cleanup/cleanup'
import PreviewDialog from '@/components/cleanup/PreviewDialog.vue'
import PageCard from '@/components/PageCard.vue'
import SizeCell from '@/components/SizeCell.vue'
import type { PreviewScope } from '@/composables/useCleanupPreview'
import { t } from '@/i18n'
import { formatBytes, formatCount } from '@/utils/format'
import { iconRefresh, iconShow } from '@/utils/icons'

const props = defineProps<{ repo: Repository; policies: CleanupPolicy[] }>()

interface Result {
  totals?: CleanupPreview
  error?: unknown
}

const results = shallowRef<Record<string, Result>>({})
const loading = ref(false)
const reviewing = ref(false)
const request = ref<PreviewScope | null>(null)
const reviewTitle = ref('')
let generation = 0

const scopeFor = (policy: CleanupPolicy): PreviewScope => ({ format: policy.format, criteria: normaliseCriteria(policy.criteria, policy.format), repositories: [props.repo.name] })

const total = computed(() => {
  const values = Object.values(results.value)
  if (!values.length || values.some((r) => !r.totals && !r.error)) return null
  return values.reduce((a, r) => a + (r.totals?.bytes ?? 0), 0)
})

async function load(): Promise<void> {
  const mine = ++generation
  loading.value = true
  results.value = {}
  const entries = await Promise.all(
    props.policies.map(async (policy): Promise<[string, Result]> => {
      try {
        return [policy.id, { totals: await call(api.POST('/cleanup-policies/preview', { body: { ...scopeFor(policy), limit: 0 } })) }]
      } catch (error) {
        return [policy.id, { error }]
      }
    }),
  )
  if (mine !== generation) return
  results.value = Object.fromEntries(entries)
  loading.value = false
}

function review(policy: CleanupPolicy): void {
  request.value = scopeFor(policy)
  reviewTitle.value = t('cleanup.preview.title', { name: policy.name })
  reviewing.value = true
}

watch(() => props.policies.map((p) => `${p.id}:${p.updated_at}:${p.last_run?.status ?? ''}`).join(), () => void load(), { immediate: true })
</script>

<style scoped>
.impact-repo {
  list-style: none;
  margin: 0;
  padding: 0;
}

.impact-repo__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 12px;
  align-items: center;
  padding: 8px 16px;
  border-top: 1px solid var(--border);
}

.impact-repo__name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
}

.impact-repo__value {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.impact-repo__total {
  font-size: var(--fs-13);
  font-weight: 600;
}

.impact-repo__error {
  font-size: var(--fs-12);
  color: var(--status-blocked);
}

@media (max-width: 599px) {
  .impact-repo__row {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .impact-repo__value {
    grid-column: 1;
    grid-row: 2;
  }
}
</style>
