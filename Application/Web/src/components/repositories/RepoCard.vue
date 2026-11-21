<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- One repository as a card: the list's grid view and its phone layout (UI_DESIGN §4.3). The whole card opens the
     repository; the name is the link (so screen readers get one link per card, not a clickable div). -->
<template>
  <article class="repo-card" :class="{ 'repo-card--rich': rich }" @click="open">
    <header class="repo-card__head">
      <FormatIcon :mark="repoMark(repo)" :size="rich ? 26 : 22" brand />
      <div class="repo-card__titles">
        <router-link :to="{ name: 'repository', params: { name: repo.name } }" class="repo-card__name" @click.stop>{{ repo.name }}</router-link>
        <div class="repo-card__meta">
          <q-icon :name="typeMeta(repo.type).icon" size="14px" />{{ typeMeta(repo.type).label }} · {{ formatLabel(repo.format) }}
          <ManagedBadge :item="repo" class="q-ml-xs" />
        </div>
      </div>
      <StatusPill :status="repo.status" :tooltip="false" />
    </header>
    <p v-if="rich" class="repo-card__desc">{{ repo.description || ' ' }}</p>
    <div v-if="rich" class="repo-card__spark">
      <Sparkline :values="repo.stats.requests_spark_24h" :height="34" />
    </div>
    <dl class="repo-card__stats">
      <div>
        <dt>{{ t('ui.repoList.stored') }}</dt>
        <dd><SizeCell :bytes="repo.stats.size_bytes" /></dd>
      </div>
      <div>
        <dt>{{ t('ui.repoList.artifacts') }}</dt>
        <dd>{{ formatCount(repo.stats.artifact_count) }}</dd>
      </div>
      <div>
        <dt>{{ t('ui.repoList.hit') }}</dt>
        <dd>{{ formatPercent(repo.stats.hit_ratio_24h) }}</dd>
      </div>
      <div v-if="rich">
        <dt>{{ t('ui.repoList.requests') }}</dt>
        <dd>{{ formatCount(repo.stats.requests_24h) }}</dd>
      </div>
    </dl>
    <QuotaMeter v-if="repo.quota" :quota="repo.quota" compact class="repo-card__quota" />
    <footer class="repo-card__foot">
      <span class="repo-card__activity">{{ t('ui.repoList.lastActivity') }} <TimeCell :value="repo.stats.last_activity_at" :placeholder="t('repos.never')" /></span>
      <q-btn v-if="rich" flat dense round size="sm" :icon="iconLink" :aria-label="t('common.copyWhat', { what: repo.url })" @click.stop="copy(repo.url, t('repos.url'))">
        <q-tooltip>{{ repo.url }}</q-tooltip>
      </q-btn>
    </footer>
  </article>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'

import type { Repository } from '@/api/types'
import Sparkline from '@/components/charts/Sparkline.vue'
import FormatIcon from '@/components/FormatIcon.vue'
import ManagedBadge from '@/components/ManagedBadge.vue'
import SizeCell from '@/components/SizeCell.vue'
import StatusPill from '@/components/StatusPill.vue'
import TimeCell from '@/components/TimeCell.vue'
import { useCopy } from '@/composables/useNotify'
import { t } from '@/i18n'
import { formatLabel, repoMark, typeMeta } from '@/utils/domain'
import { formatCount, formatPercent } from '@/utils/format'
import { iconLink } from '@/utils/icons'

import QuotaMeter from './QuotaMeter.vue'

const props = withDefaults(defineProps<{ repo: Repository; rich?: boolean }>(), { rich: false })
const router = useRouter()
const copy = useCopy()

function open(): void {
  void router.push({ name: 'repository', params: { name: props.repo.name } })
}
</script>

<style scoped>
.repo-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  cursor: pointer;
  transition: border-color var(--dur) var(--ease-out);
}

.repo-card:hover,
.repo-card:focus-within {
  border-color: var(--q-primary);
}

.repo-card--rich {
  padding: 16px;
  box-shadow: var(--shadow-card);
}

.repo-card__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
}

.repo-card__titles {
  flex: 1;
  min-width: 0;
}

.repo-card__name {
  font-weight: 650;
  color: var(--text-primary);
  text-decoration: none;
  overflow-wrap: anywhere;
}

.repo-card__name:hover,
.repo-card__name:focus-visible {
  color: var(--q-primary);
  text-decoration: underline;
}

.repo-card__meta {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.repo-card__desc {
  min-height: 1.45em;
  margin: 0;
  font-size: var(--fs-13);
  color: var(--text-secondary);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.repo-card__spark :deep(.sparkline) {
  width: 100%;
}

.repo-card__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(60px, 1fr));
  gap: 4px 12px;
  margin: 0;
}

.repo-card__stats dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.repo-card__stats dd {
  margin: 0;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.repo-card__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 24px;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}
</style>
