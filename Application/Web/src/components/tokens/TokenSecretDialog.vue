<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Shows a new token's secret exactly once, with copy and the common ways to use it. Persistent: it only closes
     through the explicit "I have copied it" button, and the secret is dropped from memory when it closes. -->
<template>
  <q-dialog v-model="open" persistent :maximized="$q.screen.lt.sm" @hide="emit('closed')">
    <q-card class="secret" :style="$q.screen.lt.sm ? undefined : { width: '600px', maxWidth: '96vw' }">
      <header class="secret__head">
        <div class="secret__title">{{ t('tokens.secret.title') }}</div>
        <div class="secret__caption">{{ t('tokens.secret.caption', { name: created?.token.name ?? '' }) }}</div>
      </header>
      <div class="secret__body">
        <div class="secret__warning" role="alert">
          <q-icon :name="iconWarning" size="18px" />
          {{ t('tokens.secret.warning') }}
        </div>
        <div class="secret__label">{{ t('tokens.secret.label') }}</div>
        <div class="secret__value">
          <code class="mono" data-testid="token-secret">{{ secret }}</code>
          <q-btn unelevated no-caps color="primary" :icon="iconCopy" :label="t('common.copy')" @click="copy(secret, t('tokens.secret.label'))" />
        </div>
        <div class="secret__label">{{ t('tokens.secret.usage') }}</div>
        <div v-for="example in examples" :key="example.label" class="secret__example">
          <div class="secret__example-label">{{ example.label }}</div>
          <CopyField :value="example.text" :label="example.label" />
        </div>
      </div>
      <footer class="secret__foot">
        <q-btn unelevated no-caps color="primary" :label="t('tokens.secret.done')" @click="open = false" />
      </footer>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { computed } from 'vue'

import type { ApiTokenCreated } from '@/api/types'
import CopyField from '@/components/CopyField.vue'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useConfigStore } from '@/stores/config'
import { iconCopy, iconWarning } from '@/utils/icons'

const props = defineProps<{ created: ApiTokenCreated | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ closed: [] }>()

const $q = useQuasar()
const copy = useCopy()
const auth = useAuthStore()
const config = useConfigStore()

const secret = computed(() => props.created?.secret ?? '')
const examples = computed(() => {
  const host = config.config?.docker.registry_host ?? 'registry.example.com'
  const base = config.config?.base_url ?? `https://${host}`
  const user = auth.user?.username ?? 'me'
  const npmHost = base.replace(/^https?:/, '')
  return [
    { label: t('tokens.secret.docker'), text: `echo '${secret.value}' | docker login ${host} --username ${user} --password-stdin` },
    { label: t('tokens.secret.npm'), text: `${npmHost}/repository/<repo>/:_authToken=${secret.value}` },
    { label: t('tokens.secret.curl'), text: `curl -u ${user}:${secret.value} ${base}/repository/<repo>/<path>` },
  ]
})
</script>

<style scoped>
.secret {
  display: flex;
  flex-direction: column;
}

.secret__head {
  padding: 18px 20px 12px;
  border-bottom: 1px solid var(--border);
}

.secret__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.secret__caption {
  margin-top: 2px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.secret__body {
  padding: 16px 20px 20px;
  display: grid;
  gap: 10px;
}

.secret__warning {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
  font-size: var(--fs-13);
}

.secret__label {
  margin-top: 6px;
  font-size: var(--fs-13);
  font-weight: 600;
}

.secret__value {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 8px 8px 12px;
  border: 1px solid var(--q-primary);
  border-radius: var(--radius-sm);
  background: var(--surface-brand);
}

.secret__value code {
  flex: 1;
  min-width: 0;
  word-break: break-all;
  font-size: var(--fs-14);
  user-select: all;
}

.secret__example {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.secret__example-label {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.secret__foot {
  display: flex;
  justify-content: flex-end;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
</style>
