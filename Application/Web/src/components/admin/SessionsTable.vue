<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Browser sessions (newest first): browser, address, when started and last used, expiry. -->
<template>
  <div v-if="!sessions.length" class="sessions__empty text-faint">{{ t('admin.sessions.empty') }}</div>
  <ul v-else class="sessions">
    <li v-for="s in sessions" :key="s.id" class="sessions__row">
      <q-icon :name="iconSessions" size="20px" class="sessions__icon" />
      <div class="sessions__main">
        <div class="sessions__browser">
          {{ browserLabel(s.user_agent) ?? t('admin.sessions.unknownBrowser') }}
          <span v-if="s.current" class="sessions__current">{{ t('admin.sessions.current') }}</span>
          <span v-if="s.provider_id" class="text-faint sessions__via">{{ t('admin.sessions.via', { provider: providerName(s.provider_id) }) }}</span>
          <q-tooltip v-if="s.user_agent" max-width="420px">{{ s.user_agent }}</q-tooltip>
        </div>
        <div class="sessions__meta">
          <span class="mono">{{ s.ip ?? '–' }}</span>
          <span>{{ t('admin.sessions.started') }} <TimeCell :value="s.created_at" /></span>
          <span>{{ t('admin.sessions.lastSeen') }} <TimeCell :value="s.last_seen_at" /></span>
          <span>{{ t('admin.sessions.expires') }} <TimeCell :value="s.expires_at" /></span>
        </div>
      </div>
    </li>
  </ul>
</template>

<script setup lang="ts">
import type { SessionInfo } from '@/api/types'
import TimeCell from '@/components/TimeCell.vue'
import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'
import { iconSessions } from '@/utils/icons'
import { browserLabel } from '@/utils/userAgent'

defineProps<{ sessions: SessionInfo[] }>()
const directory = useDirectoryStore()
const providerName = (id: string) => directory.providers.find((p) => p.id === id)?.name ?? id
</script>

<style scoped>
.sessions {
  margin: 0;
  padding: 0;
  list-style: none;
}

.sessions__row {
  display: flex;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}

.sessions__row:last-child {
  border-bottom: 0;
}

.sessions__icon {
  color: var(--text-tertiary);
  margin-top: 2px;
}

.sessions__main {
  min-width: 0;
}

.sessions__browser {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.sessions__current {
  padding: 0 7px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--status-ok);
  background: var(--status-ok-bg);
}

.sessions__via {
  font-weight: 400;
  font-size: var(--fs-12);
}

.sessions__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
