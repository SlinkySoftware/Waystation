<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Administration → System (UI_DESIGN §4.10, SPEC §4.12): version, MongoDB and Valkey health, the data volume (with
     the free-space floor where writes stop) and the support bundle download (a redacted zip, T-O10). Refreshes every
     15 s while open. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.system')" :caption="t('admin.system.caption')">
      <template #actions>
        <q-btn outline no-caps :icon="iconRefresh" :label="t('admin.system.refresh')" :loading="state.loading.value" @click="state.reload" />
      </template>
    </PageHeader>

    <q-card v-if="state.error.value && !info">
      <EmptyState illustration="error" :title="t('admin.system.loadFailed')" :text="errorMessage(state.error.value)">
        <q-btn flat no-caps :label="t('common.retry')" @click="state.reload" />
      </EmptyState>
    </q-card>

    <div v-else class="system">
      <div class="system__facts">
        <div class="system__fact q-card">
          <div class="system__label">{{ t('admin.system.version') }}</div>
          <div class="system__value mono">{{ info?.version ?? '…' }}</div>
        </div>
        <div class="system__fact q-card">
          <div class="system__label">{{ t('admin.system.python') }}</div>
          <div class="system__value mono">{{ info?.python ?? '…' }}</div>
        </div>
        <div class="system__fact q-card">
          <div class="system__label">{{ t('admin.system.uptime') }}</div>
          <div class="system__value"><TimeCell :value="info?.started_at" /></div>
        </div>
      </div>

      <PageCard :title="t('admin.system.components')" :caption="t('admin.system.componentsCaption')">
        <div class="system__components">
          <q-skeleton v-for="n in info ? 0 : 2" :key="n" height="92px" />
          <div v-for="c in info?.components ?? []" :key="c.name" class="component" :class="`component--${c.status}`">
            <q-icon :name="c.name === 'mongodb' ? iconStorage : iconMemory" size="28px" class="component__icon" />
            <div class="col">
              <div class="component__head">
                <span class="component__name">{{ t(`admin.system.${c.name}`) }}</span>
                <span class="ws-badge" :class="c.status === 'ok' ? 'tone-ok' : 'tone-blocked'">
                  <q-icon :name="c.status === 'ok' ? iconOk : iconError" />
                  {{ t(`admin.system.${c.status}`) }}
                </span>
              </div>
              <div class="component__help">{{ t(`admin.system.${c.name}Help`) }}</div>
              <div class="component__meta">
                <span v-if="c.version">{{ t('admin.system.versionOf', { version: c.version }) }}</span>
                <span v-if="c.latency_ms !== null" class="num">{{ t('admin.system.latency', { ms: c.latency_ms.toFixed(2) }) }}</span>
                <span v-if="c.detail" class="mono text-negative">{{ c.detail }}</span>
              </div>
            </div>
          </div>
        </div>
      </PageCard>

      <PageCard :title="t('admin.system.storage')" :caption="t('admin.system.storageCaption')">
        <template v-if="storage">
          <div class="volume-path">
            <span class="system__label">{{ t('admin.system.path') }}</span>
            <span class="mono">{{ storage.path }}</span>
          </div>
          <template v-if="storage.total_bytes">
            <div class="volume" role="img" :aria-label="`${t('admin.system.used')} ${formatBytes(storage.used_bytes)}, ${t('admin.system.free')} ${formatBytes(storage.free_bytes)}`">
              <div class="volume__used" :class="{ 'volume__used--low': low }" :style="{ width: share(storage.used_bytes) }" />
              <div class="volume__floor" :style="{ left: `calc(100% - ${share(storage.min_free_bytes)})` }">
                <q-tooltip>{{ t('admin.system.minFree', { size: formatBytes(storage.min_free_bytes) }) }}</q-tooltip>
              </div>
            </div>
            <dl class="volume__legend">
              <div><dt>{{ t('admin.system.used') }}</dt><dd class="num">{{ formatBytes(storage.used_bytes) }} <span class="text-faint">{{ share(storage.used_bytes) }}</span></dd></div>
              <div><dt>{{ t('admin.system.free') }}</dt><dd class="num">{{ formatBytes(storage.free_bytes) }}</dd></div>
              <div><dt>{{ t('admin.system.total') }}</dt><dd class="num">{{ formatBytes(storage.total_bytes) }}</dd></div>
            </dl>
            <div class="text-faint volume__floor-text">{{ t('admin.system.minFree', { size: formatBytes(storage.min_free_bytes) }) }}</div>
            <div v-if="low" class="volume__warn" role="note"><q-icon :name="iconWarning" size="16px" /> {{ t('admin.system.lowSpace') }}</div>
          </template>
          <div v-else class="text-negative">{{ t('admin.system.unreadable') }}</div>
        </template>
        <q-skeleton v-else height="80px" />
      </PageCard>

      <PageCard :title="t('admin.system.supportBundle')" :caption="t('admin.system.supportBundleCaption')">
        <template #actions>
          <q-btn unelevated no-caps color="primary" :icon="iconDownload" :label="t('admin.system.supportBundleDownload')" :loading="downloading" data-test="support-bundle" @click="downloadBundle" />
        </template>
        <p class="bundle__text">{{ t('admin.system.supportBundleText') }}</p>
        <div class="bundle__note" role="note"><q-icon :name="iconShield" size="16px" /> {{ t('admin.system.supportBundleRedacted') }}</div>
      </PageCard>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { usePolling } from '@/composables/usePolling'
import { t } from '@/i18n'
import { formatBytes } from '@/utils/format'
import { iconDownload, iconError, iconMemory, iconOk, iconRefresh, iconShield, iconStorage, iconWarning } from '@/utils/icons'

const state = useAsync(() => call(api.GET('/system')))
usePolling(() => true, () => state.reload(), 15_000)
const info = computed(() => state.data.value)
const storage = computed(() => info.value?.storage ?? null)
const share = (bytes: number | null) => (storage.value?.total_bytes && bytes !== null ? `${((bytes / storage.value.total_bytes) * 100).toFixed(1)}%` : '0%')
/** Within twice the floor where writes stop. */
const low = computed(() => Boolean(storage.value && storage.value.free_bytes !== null && storage.value.free_bytes < storage.value.min_free_bytes * 2))

// The support bundle: fetched through the generated client (session cookie), then handed to the browser as a file.
const notify = useNotify()
const downloading = ref(false)
async function downloadBundle(): Promise<void> {
  downloading.value = true
  try {
    const { data, error: problem, response } = await api.GET('/system/support-bundle', { parseAs: 'blob' })
    if (!response.ok || !data) throw problem ?? new Error(response.statusText || t('admin.system.supportBundleFailed'))
    const name = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') ?? '')?.[1] ?? 'waystation-support.zip'
    const url = URL.createObjectURL(data)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    notify.success(t('admin.system.supportBundleDownloaded'))
  } catch (cause) {
    notify.error(cause, t('admin.system.supportBundleFailed'))
  } finally {
    downloading.value = false
  }
}
</script>

<style scoped>
.system {
  display: grid;
  gap: 16px;
}

.system__facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

.system__fact {
  padding: 14px 16px;
}

.system__label {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.system__value {
  margin-top: 4px;
  font-size: var(--fs-16);
  font-weight: 600;
}

.system__components {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
}

.component {
  display: flex;
  gap: 14px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.component--unavailable {
  border-color: var(--status-blocked);
}

.component__icon {
  color: var(--q-primary);
}

.component--unavailable .component__icon {
  color: var(--status-blocked);
}

.component__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.component__name {
  font-weight: 650;
}

.component__help {
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.component__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  margin-top: 6px;
  font-size: var(--fs-13);
}

.volume-path {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 12px;
}

.volume {
  position: relative;
  height: 14px;
  border-radius: 999px;
  background: var(--chart-track);
}

.volume__used {
  height: 100%;
  border-radius: 999px;
  background: var(--brand-500);
  transition: width 600ms var(--ease-out);
}

.volume__used--low {
  background: var(--status-degraded);
}

.volume__floor {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 2px;
  background: var(--status-blocked);
}

.volume__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 32px;
  margin: 12px 0 0;
}

.volume__legend dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.volume__legend dd {
  margin: 0;
  font-weight: 600;
}

.volume__floor-text {
  margin-top: 6px;
  font-size: var(--fs-12);
}

.bundle__text {
  margin: 0;
  color: var(--text-secondary);
}

.bundle__note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 10px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.bundle__note .q-icon {
  flex: none;
  margin-top: 2px;
  color: var(--q-primary);
}

.volume__warn {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  color: var(--status-degraded);
  font-size: var(--fs-13);
}
</style>
