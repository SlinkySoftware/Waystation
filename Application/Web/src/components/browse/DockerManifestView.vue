<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Manifest page: platform switcher, layers with a stacked size bar, pull commands by tag and by digest. -->
<template>
  <div v-if="m.data.value" class="manifest">
    <div class="manifest__top">
      <div class="manifest__facts">
        <div class="fact"><span class="fact__k">{{ t('browse.docker.digest') }}</span><CopyField :value="m.data.value.digest" :label="t('browse.docker.digest')" /></div>
        <div class="fact"><span class="fact__k">{{ t('browse.docker.mediaType') }}</span><span class="mono fact__v">{{ m.data.value.media_type }}</span></div>
        <div class="fact"><span class="fact__k">{{ t('browse.docker.created') }}</span><TimeCell :value="m.data.value.created_at" /></div>
        <div class="fact"><span class="fact__k">{{ t('browse.docker.compressed') }}</span><SizeCell :bytes="m.data.value.total_size" /></div>
      </div>
      <div class="manifest__pull">
        <div class="section-label">{{ t('browse.docker.pull') }}</div>
        <CopyField :value="m.data.value.pull_by_tag ?? ''" :label="t('browse.docker.pullTag')" />
        <CopyField :value="m.data.value.pull_by_digest ?? ''" :label="t('browse.docker.pullDigest')" />
      </div>
    </div>

    <div v-if="m.data.value.is_index" class="manifest__platforms">
      <span class="section-label">{{ t('browse.docker.platform') }}</span>
      <q-btn-toggle
        :model-value="platformKey"
        no-caps
        dense
        unelevated
        toggle-color="primary"
        class="seg"
        :options="m.data.value.manifests.map((x) => ({ label: plat(x.platform), value: plat(x.platform) }))"
        @update:model-value="(v: string) => nav({ platform: v }, true)"
      />
    </div>

    <div class="manifest__layers">
      <div class="manifest__bar" role="img" :aria-label="t('browse.docker.layerBar')">
        <span
          v-for="(l, i) in m.data.value.layers"
          :key="l.digest"
          class="manifest__seg"
          :style="{ flexGrow: Math.max(l.size, m.data.value.total_size * 0.004), background: `var(--chart-${(i % 4) + 1})` }"
        >
          <q-tooltip>{{ t('browse.docker.layerN', { n: i + 1 }) }} · {{ formatBytes(l.size) }}</q-tooltip>
        </span>
      </div>
      <q-table class="ws-table" flat dense :rows="m.data.value.layers" :columns="columns" row-key="digest" :pagination="{ rowsPerPage: 0 }" hide-pagination>
        <template #body-cell-n="{ rowIndex }">
          <q-td><span class="layer-n" :style="{ background: `var(--chart-${(rowIndex % 4) + 1})` }" />{{ rowIndex + 1 }}</q-td>
        </template>
        <template #body-cell-digest="{ row }">
          <q-td><CopyField :value="shortDigest(row.digest, 16)" compact /></q-td>
        </template>
        <template #body-cell-created_by="{ row }">
          <q-td class="layer-cmd mono" :title="row.created_by ?? ''">{{ row.created_by }}</q-td>
        </template>
        <template #body-cell-size="{ row }">
          <q-td class="text-right"><SizeCell :bytes="row.size" /></q-td>
        </template>
        <template #body-cell-cached="{ row }">
          <q-td><CacheBadge v-if="row.cached" state="HIT" /><span v-else class="text-faint">{{ t('browse.notCached') }}</span></q-td>
        </template>
      </q-table>
    </div>
  </div>
  <div v-else-if="m.error.value" class="q-pa-md text-negative">{{ errorMessage(m.error.value) }}</div>
  <q-skeleton v-else height="320px" class="q-ma-md" />
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { DockerLayer, Platform, Repository } from '@/api/types'
import CacheBadge from '@/components/CacheBadge.vue'
import CopyField from '@/components/CopyField.vue'
import SizeCell from '@/components/SizeCell.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useQueryNavigate, useQueryParam } from '@/composables/useQueryParam'
import { t } from '@/i18n'
import { formatBytes, shortDigest } from '@/utils/format'

const props = defineProps<{ repo: Repository; image: string; reference: string }>()

const platform = useQueryParam('platform')
const nav = useQueryNavigate()
const m = useAsync(
  () =>
    call(
      api.GET('/repositories/{name}/docker/manifest', {
        params: { path: { name: props.repo.name }, query: { image: props.image, reference: props.reference, platform: platform.value ?? undefined } },
      }),
    ),
  [() => props.image, () => props.reference, platform],
)

const plat = (p: Platform) => `${p.os}/${p.architecture}${p.variant ? `/${p.variant}` : ''}`
const platformKey = computed(() => (m.data.value?.platform ? plat(m.data.value.platform) : null))

const columns = computed<QTableColumn<DockerLayer>[]>(() => [
  { name: 'n', label: '#', field: 'digest', align: 'left' },
  { name: 'digest', label: t('browse.docker.digest'), field: 'digest', align: 'left' },
  { name: 'created_by', label: t('browse.docker.createdBy'), field: (r) => r.created_by ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
  { name: 'size', label: t('browse.size'), field: 'size', align: 'right' },
  { name: 'cached', label: t('browse.cache'), field: (r) => r.cached ?? false, align: 'left' },
])
</script>

<style scoped>
.manifest__top {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 16px 24px;
  padding: 16px;
  border-top: 1px solid var(--border);
}

.manifest__facts {
  display: grid;
  gap: 8px;
}

.fact {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  font-size: var(--fs-13);
}

.fact__k {
  color: var(--text-secondary);
}

.fact__v {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.manifest__pull {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.manifest__platforms {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 0 16px 14px;
}

.seg {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  flex-wrap: wrap;
}

.seg :deep(.q-btn) {
  font-family: var(--font-mono);
  font-size: var(--fs-12);
  padding: 0 10px;
  min-height: 28px;
}

.manifest__bar {
  display: flex;
  gap: 2px;
  height: 10px;
  margin: 0 16px 12px;
  border-radius: 999px;
  overflow: hidden;
}

.manifest__seg {
  min-width: 3px;
}

.layer-n {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 8px;
  border-radius: 2px;
}

.layer-cmd {
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

@media (max-width: 1023px) {
  .manifest__top {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
