<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- "Where used" for a content digest (/digests/{digest}/usage): the visible repositories holding the blob and the
     docker manifests referencing it. Opened from the Storage page's largest artifacts. -->
<template>
  <q-dialog v-model="open" :maximized="$q.screen.lt.sm">
    <q-card class="wu" :style="$q.screen.lt.sm ? undefined : { width: '720px', maxWidth: '96vw' }">
      <header class="wu__head">
        <div>
          <div class="wu__title">{{ name ? t('storage.artifacts.whereUsedFor', { name }) : t('storage.whereUsed.title') }}</div>
          <div class="wu__caption">{{ t('storage.whereUsed.caption') }}</div>
        </div>
        <q-btn flat dense round :icon="iconClose" :aria-label="t('storage.whereUsed.close')" @click="open = false" />
      </header>
      <div class="wu__body">
        <dl class="wu__facts">
          <dt>{{ t('storage.whereUsed.digest') }}</dt>
          <dd class="mono">
            {{ shortDigest(`sha256:${digest}`, 20) }}
            <q-btn flat dense round size="sm" :icon="iconCopy" :aria-label="t('storage.whereUsed.digest')" @click="copy(`sha256:${digest}`, t('storage.whereUsed.digest'))" />
          </dd>
          <template v-if="data?.size != null">
            <dt>{{ t('storage.whereUsed.size') }}</dt>
            <dd>{{ formatBytes(data.size) }}</dd>
          </template>
        </dl>
        <div v-if="error && !data" class="text-negative">{{ t('storage.whereUsed.loadFailed') }}</div>
        <q-skeleton v-else-if="!data" height="120px" />
        <EmptyState v-else-if="!data.items.length" compact :icon="iconLink" :title="t('storage.whereUsed.none')" />
        <template v-else>
          <div class="wu__count">{{ t('storage.whereUsed.count', { count: data.items.length }) }}</div>
          <table class="wu__table">
            <thead>
              <tr>
                <th scope="col">{{ t('storage.whereUsed.repository') }}</th>
                <th scope="col">{{ t('storage.whereUsed.kind') }}</th>
                <th scope="col">{{ t('storage.whereUsed.location') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(u, i) in data.items" :key="i">
                <td><RepoChip :name="u.repository" :format="u.format" :mark-override="repos.markFor(u.repository, u.format)" /></td>
                <td>{{ t(`storage.whereUsed.kinds.${u.kind}`) }}</td>
                <td class="mono wu__loc" :title="location(u)">{{ truncateMiddle(location(u), 64) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="data.truncated" class="wu__count text-faint">{{ t('storage.whereUsed.truncated', { count: data.items.length }) }}</div>
        </template>
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { api, call } from '@/api/client'
import type { DigestUse } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import RepoChip from '@/components/RepoChip.vue'
import { useAsync } from '@/composables/useAsync'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useRepositoriesStore } from '@/stores/repositories'
import { formatBytes, shortDigest, truncateMiddle } from '@/utils/format'
import { iconClose, iconCopy, iconLink } from '@/utils/icons'

const props = defineProps<{ digest: string; name?: string | null }>()
const open = defineModel<boolean>({ required: true })

const repos = useRepositoriesStore()
const copy = useCopy()
const { data, error } = useAsync(() => call(api.GET('/digests/{digest}/usage', { params: { path: { digest: props.digest } } })), [() => props.digest])

function location(u: DigestUse): string {
  if (u.kind === 'asset') return u.path ?? ''
  return `${u.image ?? ''}@${shortDigest(u.manifest_digest, 12)}`
}
</script>

<style scoped>
.wu__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 16px 8px 20px;
}

.wu__title {
  font-size: var(--fs-16);
  font-weight: 600;
}

.wu__caption {
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.wu__body {
  padding: 8px 20px 20px;
}

.wu__facts {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 16px;
  margin: 0 0 12px;
  font-size: var(--fs-13);
}

.wu__facts dt {
  color: var(--text-secondary);
}

.wu__facts dd {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 4px;
}

.wu__count {
  margin: 4px 0 8px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.wu__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-13);
}

.wu__table th {
  text-align: left;
  font-weight: 600;
  font-size: var(--fs-12);
  color: var(--text-secondary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}

.wu__table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

.wu__loc {
  word-break: break-all;
}
</style>
