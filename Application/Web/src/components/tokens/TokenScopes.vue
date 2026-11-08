<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A token's scopes and repository limits as compact chips (labels, not colour alone). -->
<template>
  <div class="token-scopes">
    <span v-for="scope in token.scopes" :key="scope" class="token-scopes__chip" :class="{ 'token-scopes__chip--admin': scope === 'admin' }">
      {{ scopeLabel(scope) }}
      <q-tooltip>{{ scopeHelp(scope) }}</q-tooltip>
    </span>
    <span class="token-scopes__repos">
      <template v-if="token.repositories.length">
        <span v-for="pattern in token.repositories" :key="pattern" class="token-scopes__repo mono">{{ pattern }}</span>
      </template>
      <template v-else>{{ t('tokens.allRepositories') }}</template>
    </span>
  </div>
</template>

<script setup lang="ts">
import type { ApiToken } from '@/api/types'
import { t } from '@/i18n'

import { scopeHelp, scopeLabel } from './tokens'

defineProps<{ token: ApiToken }>()
</script>

<style scoped>
.token-scopes {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
}

.token-scopes__chip {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--q-primary);
  background: var(--surface-brand);
}

.token-scopes__chip--admin {
  color: var(--status-degraded);
  background: var(--status-degraded-bg);
}

.token-scopes__repos {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.token-scopes__repo {
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
</style>
