<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Audit log (UI_DESIGN §4.10, SPEC §4.11): filterable table (filters in the URL), cursor paging, a detail panel
     with the field diff, and CSV / NDJSON export of the current filter set. Cards on phones. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.audit')" :caption="t('audit.caption')">
      <template #actions>
        <q-btn-dropdown unelevated no-caps color="primary" :icon="iconDownload" :label="t('audit.export')" :loading="exporting">
          <q-list dense>
            <q-item v-close-popup clickable @click="exportAs('csv')"><q-item-section>{{ t('audit.exportCsv') }}</q-item-section></q-item>
            <q-item v-close-popup clickable @click="exportAs('ndjson')"><q-item-section>{{ t('audit.exportNdjson') }}</q-item-section></q-item>
          </q-list>
        </q-btn-dropdown>
      </template>
    </PageHeader>

    <q-card class="audit">
      <AuditFilters />

      <q-table
        class="ws-table"
        flat
        :rows="items"
        :columns="columns"
        row-key="id"
        :loading="loading && !items.length"
        :grid="$q.screen.lt.sm"
        :pagination="{ rowsPerPage: 0 }"
        hide-pagination
        @row-click="(_e: Event, row: AuditRecord) => show(row)"
      >
        <template #body-cell-time="{ row }">
          <q-td class="cursor-pointer"><TimeCell :value="row.ts" /></q-td>
        </template>
        <template #body-cell-actor="{ row }">
          <q-td class="cursor-pointer">
            <div class="audit__actor">
              <span class="audit__strong">{{ row.actor.kind === 'system' ? t('audit.system') : row.actor.name }}</span>
              <span class="audit__chip">{{ actorChip(row) }}</span>
              <q-icon v-if="row.actor.token_id" :name="iconToken" size="14px" class="text-faint">
                <q-tooltip>{{ t('audit.viaToken', { id: row.actor.token_id }) }}</q-tooltip>
              </q-icon>
            </div>
          </q-td>
        </template>
        <template #body-cell-action="{ row }">
          <q-td class="cursor-pointer">
            <div class="audit__action">
              <q-icon :name="auditCategoryIcon(row.action)" size="18px" class="text-faint" />
              <span>{{ auditActionLabel(row.action, row.outcome) }}</span>
            </div>
          </q-td>
        </template>
        <template #body-cell-target="{ row }">
          <q-td class="cursor-pointer">
            <div v-if="row.target" class="audit__target">
              <span class="audit__kind">{{ auditTargetKindLabel(row.target.kind) }}</span>
              <span class="audit__strong ellipsis">{{ auditTargetText(row) }}</span>
            </div>
            <span v-else class="text-faint">–</span>
          </q-td>
        </template>
        <template #body-cell-outcome="{ row }">
          <q-td class="cursor-pointer"><AuditOutcomePill :outcome="row.outcome" :reason="row.reason" /></q-td>
        </template>
        <template #body-cell-ip="{ row }">
          <q-td class="cursor-pointer mono">{{ row.client_ip ?? '–' }}</q-td>
        </template>

        <template #item="{ row }">
          <div class="audit__card" role="button" tabindex="0" @click="show(row)" @keydown.enter="show(row)">
            <div class="audit__action">
              <q-icon :name="auditCategoryIcon(row.action)" size="18px" class="text-faint" />
              <span class="audit__strong col">{{ auditActionLabel(row.action, row.outcome) }}</span>
              <AuditOutcomePill :outcome="row.outcome" :reason="row.reason" />
            </div>
            <div class="audit__card-meta">
              <span>{{ row.actor.name }}</span>
              <span v-if="row.target">{{ auditTargetText(row) }}</span>
              <span class="mono">{{ row.client_ip }}</span>
              <TimeCell :value="row.ts" />
            </div>
          </div>
        </template>

        <template #no-data>
          <EmptyState v-if="error" class="full-width" illustration="error" :title="t('audit.loadFailed')" :text="errorMessage(error)">
            <q-btn flat no-caps :label="t('common.retry')" @click="reload" />
          </EmptyState>
          <EmptyState v-else-if="!loading" class="full-width" :illustration="filtered ? 'search' : 'activity'" :title="filtered ? t('audit.noMatch') : t('audit.emptyTitle')" :text="filtered ? t('audit.noMatchText') : t('audit.emptyText')">
            <q-btn v-if="filtered" flat no-caps :label="t('audit.clearFilters')" @click="clear" />
          </EmptyState>
        </template>
      </q-table>

      <footer v-if="items.length" class="audit__footer">
        <span class="text-faint">{{ nextCursor ? t('audit.shownMore', { count: items.length }) : t('audit.shown', { count: items.length }) }}</span>
        <q-btn v-if="nextCursor" outline no-caps :loading="loading" :label="t('common.loadMore')" @click="loadMore" />
      </footer>
    </q-card>

    <AuditDrawer v-model="drawerOpen" :record="selected" />
  </q-page>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import { computed, ref, shallowRef, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { AuditRecord } from '@/api/types'
import AuditDrawer from '@/components/audit/AuditDrawer.vue'
import AuditFilters from '@/components/audit/AuditFilters.vue'
import AuditOutcomePill from '@/components/audit/AuditOutcomePill.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import TimeCell from '@/components/TimeCell.vue'
import { isFiltered, toApiQuery, useAuditFilters } from '@/composables/useAuditFilters'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { auditActionLabel, auditAuthLabel, auditCategoryIcon, auditTargetKindLabel, auditTargetText, providerLabel } from '@/utils/audit'
import { iconDownload, iconToken } from '@/utils/icons'

const PAGE = 100
const notify = useNotify()
const { state, clear } = useAuditFilters()
const filtered = computed(() => isFiltered(state.value))

const items = shallowRef<AuditRecord[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)
let generation = 0
let query = toApiQuery(state.value)

async function fetchPage(cursor: string | null): Promise<void> {
  const mine = ++generation
  loading.value = true
  try {
    const page = await call(api.GET('/audit', { params: { query: { ...query, limit: PAGE, cursor: cursor ?? undefined } } }))
    if (mine !== generation) return
    items.value = cursor ? [...items.value, ...page.items] : page.items
    nextCursor.value = page.next_cursor
    error.value = null
  } catch (cause) {
    if (mine !== generation) return
    error.value = cause
    if (cursor) notify.error(cause)
    else items.value = []
  } finally {
    if (mine === generation) loading.value = false
  }
}

function reload(): Promise<void> {
  query = toApiQuery(state.value) // relative ranges are pinned when the filters change
  return fetchPage(null)
}

const loadMore = () => fetchPage(nextCursor.value)
watch(() => JSON.stringify(state.value), () => void reload(), { immediate: true })

const selected = shallowRef<AuditRecord | null>(null)
const drawerOpen = ref(false)
function show(row: AuditRecord): void {
  selected.value = row
  drawerOpen.value = true
}

function actorChip(row: AuditRecord): string {
  return providerLabel(row.actor.provider) ?? auditAuthLabel(row.actor.auth)
}

const columns = computed<QTableColumn<AuditRecord>[]>(() => [
  { name: 'time', label: t('audit.col.time'), field: 'ts', align: 'left' },
  { name: 'actor', label: t('audit.col.actor'), field: (r) => r.actor.name, align: 'left' },
  { name: 'action', label: t('audit.col.action'), field: 'action', align: 'left' },
  { name: 'target', label: t('audit.col.target'), field: (r) => r.target?.name ?? '', align: 'left', classes: 'gt-xs', headerClasses: 'gt-xs' },
  { name: 'outcome', label: t('audit.col.outcome'), field: 'outcome', align: 'left' },
  { name: 'ip', label: t('audit.col.ip'), field: (r) => r.client_ip ?? '', align: 'left', classes: 'gt-sm', headerClasses: 'gt-sm' },
])

// Export the current filter set through the generated client, then hand the file to the browser.
const exporting = ref(false)
async function exportAs(format: 'csv' | 'ndjson'): Promise<void> {
  exporting.value = true
  try {
    const { data, error: problem, response } = await api.GET('/audit/export', { params: { query: { ...toApiQuery(state.value), format } }, parseAs: 'blob' })
    if (!response.ok || !data) throw problem ?? new Error(response.statusText || t('audit.exportFailed'))
    const name = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') ?? '')?.[1] ?? `waystation-audit.${format}`
    const url = URL.createObjectURL(data)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    notify.success(t('audit.exported'))
  } catch (cause) {
    notify.error(cause, t('audit.exportFailed'))
  } finally {
    exporting.value = false
  }
}
</script>

<style scoped>
.audit {
  overflow: hidden;
}

.audit__strong {
  font-weight: 600;
  color: var(--text-primary);
}

.audit__actor,
.audit__action,
.audit__target {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.audit__target {
  max-width: 260px;
}

.audit__chip,
.audit__kind {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
}

.audit__chip {
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
}

.audit__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-13);
}

.audit__card {
  width: calc(100% - 24px);
  min-width: 0;
  margin: 0 12px 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  cursor: pointer;
}

.audit__card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin: 8px 0 0 26px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
