<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Storage by format: donut + legend with sizes, and the data directory's free space. -->
<template>
  <PageCard :title="t('dashboard.storage.title')" :caption="data ? t('dashboard.storage.caption', { blobs: formatCount(data.blob_count), dedup: formatBytes(data.dedup_saved_bytes) }) : undefined">
    <q-skeleton v-if="!data" height="160px" />
    <DonutChart v-else :items="items" :label="t('dashboard.storage.title')" :format="formatBytes" :caption="t('dashboard.storage.total')">
      <template #label="{ item }">
        <FormatIcon :mark="formatMark(item.key as Format)" :size="14" decorative />
        {{ item.label }}
      </template>
    </DonutChart>
  </PageCard>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { api, call } from '@/api/client'
import type { Format } from '@/api/types'
import DonutChart from '@/components/charts/DonutChart.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { t } from '@/i18n'
import { formatColor, formatLabel, formatMark } from '@/utils/domain'
import { formatBytes, formatCount } from '@/utils/format'

const { data } = useAsync(() => call(api.GET('/stats/storage')))
const items = computed(() =>
  (data.value?.by_format ?? []).map((f) => ({ key: f.format, label: formatLabel(f.format), value: f.bytes, color: formatColor(f.format) })),
)
</script>
