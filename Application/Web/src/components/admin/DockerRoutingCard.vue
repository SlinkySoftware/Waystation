<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Docker routing (SPEC §4.4): the registry host clients use, the default docker repository (unprefixed names and
     registry-mirror pulls; at most one) and each docker repository's upstream aliases. The default is a repository
     option, so changing it saves that repository (the server clears it on the others). -->
<template>
  <PageCard :title="t('admin.settings.docker')" :caption="t('admin.settings.dockerCaption', { host: config.config?.docker.registry_host ?? '' })">
    <ProblemBanner :error="problem" />
    <div v-if="!dockerRepos.length" class="text-faint">{{ t('admin.settings.noDockerRepos') }}</div>
    <template v-else>
      <div class="docker__row">
        <q-select
          v-model="selected"
          outlined
          dense
          emit-value
          map-options
          options-dense
          :options="options"
          :label="t('admin.settings.defaultRepo')"
          :hint="t('admin.settings.defaultRepoHint')"
          class="col"
        />
        <q-btn unelevated no-caps color="primary" :label="t('common.save')" :disable="selected === current" :loading="saving" @click="save" />
      </div>
      <ul class="docker__list">
        <li v-for="repo in dockerRepos" :key="repo.name">
          <RepoChip :name="repo.name" format="docker" />
          <span v-if="repo.name === current" class="docker__default">{{ t('admin.settings.defaultRepo') }}</span>
          <span class="docker__aliases">
            <span class="text-faint">{{ t('admin.settings.aliases') }}:</span>
            <template v-if="aliases(repo).length">
              <span v-for="alias in aliases(repo)" :key="alias" class="mono docker__alias">{{ alias }}</span>
            </template>
            <span v-else class="text-faint">{{ t('admin.settings.aliasesNone') }}</span>
          </span>
        </li>
      </ul>
    </template>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api, call } from '@/api/client'
import type { DockerOptions, Repository } from '@/api/types'
import PageCard from '@/components/PageCard.vue'
import RepoChip from '@/components/RepoChip.vue'
import { modelFromRepository, toWrite } from '@/components/repositories/form'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useConfigStore } from '@/stores/config'
import { useRepositoriesStore } from '@/stores/repositories'

import ProblemBanner from './ProblemBanner.vue'

const NONE = ''
const notify = useNotify()
const config = useConfigStore()
const repos = useRepositoriesStore()
onMounted(() => {
  void config.load()
  void repos.load(true).catch(() => undefined)
})

const options_ = (repo: Repository) => (repo.format_options ?? {}) as DockerOptions
const aliases = (repo: Repository) => options_(repo).aliases ?? []
const dockerRepos = computed(() => repos.items.filter((r) => r.format === 'docker'))
const current = computed(() => dockerRepos.value.find((r) => options_(r).default_repo)?.name ?? NONE)
const selected = ref<string>(NONE)
watch(current, (value) => (selected.value = value), { immediate: true })

// The repository form writes proxy repositories; hosted and group ones are changed on their own settings page.
const options = computed(() => [
  { value: NONE, label: t('admin.settings.defaultRepoNone') },
  ...dockerRepos.value.map((r) => ({ value: r.name, label: `${r.name} (${r.type})`, disable: r.type !== 'proxy' })),
])

const saving = ref(false)
const problem = ref<unknown>(null)

async function write(repo: Repository, isDefault: boolean): Promise<Repository> {
  const model = { ...modelFromRepository(repo), defaultRepo: isDefault }
  return call(api.PUT('/repositories/{name}', { params: { path: { name: repo.name } }, body: toWrite(model) }))
}

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  try {
    const target = dockerRepos.value.find((r) => r.name === selected.value)
    const previous = dockerRepos.value.find((r) => r.name === current.value)
    if (target) await write(target, true)
    else if (previous) await write(previous, false)
    await repos.load(true)
    config.config = config.config ? { ...config.config, docker: { ...config.config.docker, default_repo: target?.name ?? null } } : null
    notify.success(target ? t('admin.settings.defaultSaved', { name: target.name }) : t('admin.settings.defaultCleared'))
  } catch (error) {
    problem.value = error
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.docker__row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 10px;
}

.docker__list {
  display: grid;
  gap: 8px;
  margin: 18px 0 0;
  padding: 0;
  list-style: none;
}

.docker__list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  font-size: var(--fs-13);
}

.docker__default {
  padding: 0 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 650;
  color: var(--status-ok);
  background: var(--status-ok-bg);
}

.docker__aliases {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px;
}

.docker__alias {
  padding: 0 6px;
  border-radius: 4px;
  background: var(--surface-sunken);
  font-size: var(--fs-12);
}
</style>
