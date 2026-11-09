<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Where an account or group comes from: Local, LDAP or OIDC (label + icon, provider name in the tooltip). -->
<template>
  <span class="source-badge" :class="`source-badge--${kind}`">
    <q-icon :name="kind === 'local' ? iconPerson : kind === 'ldap' ? iconAuthProviders : iconExternal" size="13px" />
    {{ t(`admin.source.${kind}`) }}
    <q-tooltip>{{ help }}</q-tooltip>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'
import { iconAuthProviders, iconExternal, iconPerson } from '@/utils/icons'
import { parseSource } from '@/utils/rbac'

const props = defineProps<{ source: string }>()
const directory = useDirectoryStore()
const kind = computed(() => parseSource(props.source).kind)
const help = computed(() => (kind.value === 'local' ? t('admin.source.localHelp') : t('admin.source.externalHelp', { provider: directory.providerName(props.source) ?? props.source })))
</script>

<style scoped>
.source-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 20px;
  padding: 0 7px;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
  white-space: nowrap;
}

.source-badge--ldap {
  color: var(--cache-hit);
  border-color: color-mix(in srgb, var(--cache-hit) 45%, transparent);
}

.source-badge--oidc {
  color: var(--cache-stale);
  border-color: color-mix(in srgb, var(--cache-stale) 45%, transparent);
}
</style>
