<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Dialog wrapping a validated q-form; emits `submit` only when every rule passes. Footer stays visible; on phones
     it becomes full screen with a sticky action bar (UI_DESIGN §5). -->
<template>
  <q-dialog v-model="open" :maximized="$q.screen.lt.sm" :persistent="saving" transition-show="jump-up" transition-hide="jump-down">
    <q-card class="form-dialog" :style="$q.screen.lt.sm ? undefined : { width, maxWidth: '96vw' }">
      <q-form greedy class="form-dialog__form" @submit="emit('submit')">
        <header class="form-dialog__head">
          <div class="col">
            <div class="form-dialog__title">{{ title }}</div>
            <div v-if="caption" class="form-dialog__caption">{{ caption }}</div>
          </div>
          <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" :disable="saving" />
        </header>
        <div class="form-dialog__body">
          <slot />
        </div>
        <footer class="form-dialog__foot">
          <slot name="footer-start" />
          <q-space />
          <q-btn v-close-popup flat no-caps :label="readonly ? t('common.close') : t('common.cancel')" :disable="saving" />
          <q-btn v-if="!readonly" unelevated no-caps color="primary" type="submit" :label="submitLabel ?? t('common.save')" :loading="saving" :disable="submitDisabled" />
        </footer>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'

import { t } from '@/i18n'
import { iconClose } from '@/utils/icons'

withDefaults(
  defineProps<{
    title: string
    caption?: string
    saving?: boolean
    readonly?: boolean
    submitLabel?: string
    submitDisabled?: boolean
    width?: string
  }>(),
  { caption: undefined, saving: false, readonly: false, submitLabel: undefined, submitDisabled: false, width: '560px' },
)
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ submit: [] }>()
const $q = useQuasar()
</script>

<style scoped>
.form-dialog {
  display: flex;
  flex-direction: column;
  max-height: 92vh;
}

.form-dialog__form {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

.form-dialog__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 20px 12px;
  border-bottom: 1px solid var(--border);
}

.form-dialog__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.form-dialog__caption {
  margin-top: 2px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.form-dialog__body {
  padding: 16px 20px 20px;
  overflow-y: auto;
  flex: 1;
}

.form-dialog__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  background: var(--surface);
  position: sticky;
  bottom: 0;
}
</style>
