<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- "Test upstream": reachability, auth result and sample latency before saving. -->
<template>
  <div class="up-test">
    <q-btn outline dense no-caps :icon="iconSync" :label="t('repoForm.test')" :loading="busy" :disable="!model.upstreamUrl" @click="run" />
    <div v-if="result" class="up-test__result" :class="result.reachable ? 'up-test__result--ok' : 'up-test__result--bad'" role="status">
      <div class="up-test__summary">
        <q-icon :name="result.reachable ? iconOk : iconError" size="18px" />
        <strong>{{ result.reachable ? t('repoForm.testOk', { ms: Math.round(result.latency_ms ?? 0) }) : t('repoForm.testFailed') }}</strong>
        <span v-if="result.auth" class="text-muted">· {{ t(`repoForm.authResult.${result.auth}`) }}</span>
        <span v-if="result.rate_limit" class="text-muted">· {{ t('repoForm.testRateLimit', { remaining: result.rate_limit.remaining, limit: result.rate_limit.limit }) }}</span>
      </div>
      <ul class="up-test__checks">
        <li v-for="c in result.checks" :key="c.name">
          <q-icon :name="c.ok ? iconCheck : iconClose" size="14px" :class="c.ok ? 'text-positive' : 'text-negative'" />
          <span class="mono">{{ c.name }}</span>
          <span class="text-muted">{{ c.message }}</span>
        </li>
      </ul>
    </div>
    <div v-else-if="error" class="up-test__result up-test__result--bad" role="alert">{{ error }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { UpstreamTestResult } from '@/api/types'
import { t } from '@/i18n'
import { iconCheck, iconClose, iconError, iconOk, iconSync } from '@/utils/icons'

import { type RepoFormModel, toWrite } from './form'

const props = defineProps<{ model: RepoFormModel; repository: string | null }>()

const busy = ref(false)
const result = ref<UpstreamTestResult | null>(null)
const error = ref<string | null>(null)

watch(
  () => [props.model.upstreamUrl, props.model.authKind],
  () => {
    result.value = null
    error.value = null
  },
)

async function run(): Promise<void> {
  busy.value = true
  error.value = null
  result.value = null
  try {
    const write = toWrite(props.model)
    result.value = await call(
      api.POST('/repositories/test-upstream', {
        body: { format: write.format, proxy: write.proxy!, format_options: write.format_options, ...(props.repository ? { repository: props.repository } : {}) },
      }),
    )
  } catch (cause) {
    error.value = cause instanceof ApiError && cause.problem.errors?.length ? cause.problem.errors.map((e) => e.message).join(' ') : errorMessage(cause)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.up-test {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.up-test__result {
  width: 100%;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-13);
}

.up-test__result--ok {
  background: var(--status-ok-bg);
}

.up-test__result--bad {
  background: var(--cache-error-bg);
  color: var(--cache-error);
}

.up-test__summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.up-test__result--ok .up-test__summary {
  color: var(--status-ok);
}

.up-test__checks {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: grid;
  gap: 2px;
  color: var(--text-primary);
}

.up-test__checks li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-12);
}
</style>
