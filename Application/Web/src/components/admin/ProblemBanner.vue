<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A refused request, said plainly: 403 (delegation: the caller may not do it), 409 (a rule such as the last
     administrator or a role still in use), 422 (fields), else the server's message. -->
<template>
  <div v-if="error" class="problem" :class="`problem--${kind}`" role="alert">
    <q-icon :name="kind === 'forbidden' ? iconBlocked : kind === 'conflict' ? iconWarning : iconError" size="20px" />
    <div class="col">
      <div class="problem__title">{{ t(`admin.problem.${kind}`) }}</div>
      <div class="problem__detail">{{ detail }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { ApiError, errorMessage } from '@/api/client'
import { t } from '@/i18n'
import { iconBlocked, iconError, iconWarning } from '@/utils/icons'

const props = defineProps<{ error: unknown }>()

const kind = computed(() => {
  const status = props.error instanceof ApiError ? props.error.status : 0
  if (status === 403) return 'forbidden'
  if (status === 409) return 'conflict'
  if (status === 422) return 'invalid'
  if (status === 404) return 'notFound'
  return 'failed'
})

/** The server's sentence, capitalised; for 422 the first field message when there is no detail. */
const detail = computed(() => {
  const error = props.error
  let text = errorMessage(error)
  if (error instanceof ApiError && error.status === 422) {
    const fields = Object.values(error.fieldErrors)
    if (fields.length && (!error.problem.detail || error.problem.detail === 'The request has invalid fields.')) text = fields.join(' · ')
  }
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : ''
})
</script>

<style scoped>
.problem {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 14px;
  border-radius: var(--radius-sm);
  border: 1px solid currentColor;
  font-size: var(--fs-13);
}

.problem--forbidden {
  color: var(--status-blocked);
  background: var(--status-blocked-bg);
}

.problem--conflict {
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
}

.problem--invalid,
.problem--notFound,
.problem--failed {
  color: var(--cache-error);
  background: var(--cache-error-bg);
}

.problem__title {
  font-weight: 650;
}

.problem__detail {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
</style>
