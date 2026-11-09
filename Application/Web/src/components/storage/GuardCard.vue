<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Low-disk guard (SPECIFICATION §4.8): free space against the guard, whether writes are refused now, and (for
     settings admins; the server decides) a dialog to change the minimum free bytes / percentage. -->
<template>
  <PageCard :title="t('storage.guard.title')" :caption="t('storage.guard.caption')">
    <template #actions>
      <q-btn v-if="canEdit && guard" outline no-caps color="primary" :icon="iconEdit" :label="t('storage.guard.edit')" @click="openDialog" />
    </template>

    <div v-if="state.error.value && !guard" class="guard__error" role="alert">
      {{ errorMessage(state.error.value) || t('storage.guard.loadFailed') }}
      <q-btn flat dense no-caps :label="t('common.retry')" @click="state.reload" />
    </div>
    <q-skeleton v-else-if="!guard" height="64px" />
    <dl v-else class="guard__stats">
      <div class="guard__stat">
        <dt>{{ t('storage.guard.state') }}</dt>
        <dd>
          <span class="ws-badge" :class="guard.low ? 'tone-blocked' : 'tone-ok'" data-test="guard-state">
            <q-icon :name="guard.low ? iconWarning : iconCheck" />
            {{ guard.low ? t('storage.guard.low') : t('storage.guard.ok') }}
          </span>
          <span v-if="guard.low && guard.low_since" class="guard__sub">{{ t('storage.guard.lowSince', { time: absoluteTime(guard.low_since) }) }}</span>
        </dd>
      </div>
      <div class="guard__stat">
        <dt>{{ t('storage.guard.free') }}</dt>
        <dd><span class="guard__value">{{ formatBytes(guard.free_bytes) }}</span></dd>
      </div>
      <div class="guard__stat">
        <dt>{{ t('storage.guard.floor') }}</dt>
        <dd>
          <span class="guard__value">{{ formatBytes(guard.floor_bytes) }}</span>
          <span class="guard__sub">{{ t('storage.guard.floorSub', { bytes: formatBytes(guard.min_free_bytes), percent: guard.min_free_percent, total: formatBytes(guard.total_bytes) }) }}</span>
        </dd>
      </div>
      <div class="guard__stat">
        <dt>{{ t('storage.guard.updated') }}</dt>
        <dd><TimeCell :value="guard.updated_at ?? null" :placeholder="t('storage.guard.never')" /></dd>
      </div>
    </dl>

    <FormDialog v-model="editing" :title="t('storage.guard.dialogTitle')" :caption="t('storage.guard.dialogCaption')" :saving="saving" width="480px" @submit="save">
      <div class="guard__form">
        <q-input
          v-model.number="form.minFreeGiB"
          outlined
          dense
          type="number"
          min="0"
          step="any"
          suffix="GiB"
          :label="t('storage.guard.minFree')"
          :hint="t('storage.guard.minFreeHint')"
          :rules="[(v: number) => (Number.isFinite(v) && v >= 0) || t('storage.guard.minFreeHint')]"
          :error="!!errors.min_free_bytes"
          :error-message="errors.min_free_bytes"
        />
        <q-input
          v-model.number="form.minFreePercent"
          outlined
          dense
          type="number"
          min="0"
          max="90"
          step="any"
          suffix="%"
          :label="t('storage.guard.minPercent')"
          :hint="t('storage.guard.minPercentHint')"
          :rules="[(v: number) => (Number.isFinite(v) && v >= 0 && v <= 90) || t('storage.guard.minPercentHint')]"
          :error="!!errors.min_free_percent"
          :error-message="errors.min_free_percent"
        />
      </div>
    </FormDialog>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import FormDialog from '@/components/FormDialog.vue'
import PageCard from '@/components/PageCard.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { absoluteTime, formatBytes } from '@/utils/format'
import { iconCheck, iconEdit, iconWarning } from '@/utils/icons'

defineProps<{ canEdit: boolean }>()
const notify = useNotify()
const GiB = 1024 ** 3

const state = useAsync(() => call(api.GET('/storage/guard')))
const guard = computed(() => state.data.value)

const editing = ref(false)
const saving = ref(false)
const errors = ref<Record<string, string>>({})
const form = reactive({ minFreeGiB: 0, minFreePercent: 0 })

function openDialog(): void {
  if (!guard.value) return
  form.minFreeGiB = Math.round((guard.value.min_free_bytes / GiB) * 100) / 100
  form.minFreePercent = guard.value.min_free_percent
  errors.value = {}
  editing.value = true
}

async function save(): Promise<void> {
  saving.value = true
  errors.value = {}
  try {
    state.data.value = await call(
      api.PUT('/storage/guard', { body: { min_free_bytes: Math.max(0, Math.round(form.minFreeGiB * GiB)), min_free_percent: form.minFreePercent } }),
    )
    editing.value = false
    notify.success(t('storage.guard.saved'))
  } catch (error) {
    if (error instanceof ApiError && Object.keys(error.fieldErrors).length) errors.value = error.fieldErrors
    else notify.error(error)
  } finally {
    saving.value = false
  }
}

defineExpose({ reload: state.reload })
</script>

<style scoped>
.guard__error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-secondary);
}

.guard__stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.guard__stat {
  padding: 10px 14px;
  border-left: 1px solid var(--border);
  min-width: 0;
}

.guard__stat:first-child {
  border-left: 0;
}

.guard__stat dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.guard__stat dd {
  margin: 4px 0 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
}

.guard__value {
  font-size: var(--fs-16);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.guard__sub {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.guard__form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

@media (max-width: 1023px) {
  .guard__stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .guard__stat:nth-child(3) {
    border-left: 0;
  }

  .guard__stat:nth-child(n + 3) {
    border-top: 1px solid var(--border);
  }
}
</style>
