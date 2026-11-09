<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Shows new recovery codes exactly once, with copy and download (a text file made in the browser). Persistent: it
     closes only through "I have saved them", and the codes are dropped from memory when it closes. -->
<template>
  <q-dialog v-model="open" persistent :maximized="$q.screen.lt.sm" @hide="emit('closed')">
    <q-card class="codes" :style="$q.screen.lt.sm ? undefined : { width: '520px', maxWidth: '96vw' }">
      <header class="codes__head">
        <div class="codes__title">{{ t('profile.totp.codesTitle') }}</div>
      </header>
      <div class="codes__body">
        <div class="codes__warning" role="alert">
          <q-icon :name="iconWarning" size="18px" />
          {{ t('profile.totp.codesText') }}
        </div>
        <ol class="codes__list mono" data-testid="recovery-codes">
          <li v-for="code in codes" :key="code">{{ code }}</li>
        </ol>
        <div class="codes__actions">
          <q-btn outline no-caps :icon="iconCopy" :label="t('profile.totp.copyCodes')" @click="copy(codes.join('\n'), t('profile.totp.codesTitle'))" />
          <q-btn outline no-caps :icon="iconDownload" :label="t('profile.totp.downloadCodes')" @click="download" />
        </div>
      </div>
      <footer class="codes__foot">
        <q-btn unelevated no-caps color="primary" :label="t('profile.totp.saved')" @click="open = false" />
      </footer>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'

import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useConfigStore } from '@/stores/config'
import { iconCopy, iconDownload, iconWarning } from '@/utils/icons'

const props = defineProps<{ codes: string[]; account: string }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ closed: [] }>()
const $q = useQuasar()
const copy = useCopy()
const config = useConfigStore()

function download(): void {
  const instance = config.config?.instance_name ?? 'Waystation'
  const text = [`${instance} recovery codes for ${props.account}`, '', ...props.codes, ''].join('\n')
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${instance.replace(/[^A-Za-z0-9._-]+/g, '-').toLowerCase()}-recovery-codes.txt`
  link.click()
  URL.revokeObjectURL(url)
}
</script>

<style scoped>
.codes {
  display: flex;
  flex-direction: column;
}

.codes__head {
  padding: 18px 20px 12px;
  border-bottom: 1px solid var(--border);
}

.codes__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.codes__body {
  display: grid;
  gap: 12px;
  padding: 16px 20px 20px;
}

.codes__warning {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
  font-size: var(--fs-13);
}

.codes__list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 24px;
  margin: 0;
  padding: 12px 12px 12px 36px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  font-size: var(--fs-14);
  user-select: all;
}

.codes__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.codes__foot {
  display: flex;
  justify-content: flex-end;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
</style>
