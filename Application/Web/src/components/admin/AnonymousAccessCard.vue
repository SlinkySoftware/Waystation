<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Anonymous access (SPEC §4.9): the roles of requests without credentials. The built-in `anonymous` role and roles
     with repository permissions only; none = every request must authenticate. Shows what anonymous requests could read. -->
<template>
  <PageCard :title="t('admin.anonymous.title')" :caption="t('admin.anonymous.caption')">
    <template #title>
      <span class="anon__title"><q-icon :name="iconAnonymous" size="20px" /> {{ t('admin.anonymous.title') }}</span>
    </template>
    <div v-if="state.error.value && !state.data.value" class="text-negative">{{ t('admin.anonymous.loadFailed') }}: {{ errorMessage(state.error.value) }}</div>
    <template v-else>
      <ProblemBanner :error="problem" />
      <div class="anon__row">
        <q-select
          v-model="roles"
          outlined
          dense
          multiple
          emit-value
          map-options
          use-chips
          options-dense
          :options="options"
          :label="t('admin.anonymous.roles')"
          :loading="state.loading.value"
          class="col"
        >
          <template #option="{ opt, itemProps, selected, toggleOption }">
            <q-item v-bind="itemProps" @click="toggleOption(opt)">
              <q-item-section side><q-checkbox :model-value="selected" dense size="sm" @update:model-value="toggleOption(opt)" /></q-item-section>
              <q-item-section>
                <q-item-label>{{ opt.label }} <span class="mono text-faint anon__id">{{ opt.value }}</span></q-item-label>
                <q-item-label caption>{{ opt.description }}</q-item-label>
              </q-item-section>
            </q-item>
          </template>
        </q-select>
        <q-btn unelevated no-caps color="primary" :label="t('admin.anonymous.save')" :disable="!dirty" :loading="saving" @click="save" />
      </div>
      <div class="anon__preview" :class="{ 'anon__preview--off': !roles.length }">
        <q-icon :name="roles.length ? iconShow : iconLock" size="18px" />
        <span v-if="!roles.length">{{ t('admin.anonymous.none') }}</span>
        <span v-else>
          {{ t('admin.anonymous.preview', { count: readable.length }) }}<template v-if="readable.length">: </template>
          <span class="anon__repos">{{ readable.slice(0, 12).join(', ') }}{{ readable.length > 12 ? '…' : '' }}</span>
        </span>
      </div>
    </template>
  </PageCard>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { RolePermission } from '@/api/types'
import PageCard from '@/components/PageCard.vue'
import { useAsync } from '@/composables/useAsync'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { useDirectoryStore } from '@/stores/directory'
import { useRepositoriesStore } from '@/stores/repositories'
import { iconAnonymous, iconLock, iconShow } from '@/utils/icons'
import { scopeMatches } from '@/utils/rbac'

import { hasGlobalAction } from './forms'
import ProblemBanner from './ProblemBanner.vue'

const ANONYMOUS: RolePermission[] = [
  { action: 'browse', scope: 'type:proxy' },
  { action: 'read', scope: 'type:proxy' },
]

const notify = useNotify()
const directory = useDirectoryStore()
const repos = useRepositoriesStore()
onMounted(() => {
  void directory.loadRoles().catch(() => undefined)
  void repos.load().catch(() => undefined)
})

const state = useAsync(() => call(api.GET('/anonymous-access')))
const roles = ref<string[]>([])
const saving = ref(false)
const problem = ref<unknown>(null)
watch(state.data, (value) => (roles.value = [...(value?.roles ?? [])]), { immediate: true })

const dirty = computed(() => {
  const saved = state.data.value?.roles ?? []
  return saved.length !== roles.value.length || saved.some((r) => !roles.value.includes(r))
})

const options = computed(() => [
  { value: 'anonymous', label: t('admin.anonymous.roleName'), description: t('admin.anonymous.defaultRole') },
  ...directory.roles.filter((r) => !hasGlobalAction(r)).map((r) => ({ value: r.id, label: r.name, description: r.description })),
])

const readable = computed(() => {
  const perms = roles.value.flatMap((id) => (id === 'anonymous' ? ANONYMOUS : (directory.roleById.get(id)?.permissions ?? [])))
  return repos.items.filter((repo) => perms.some((p) => p.action !== 'browse' && scopeMatches(p.scope, repo))).map((r) => r.name)
})

async function save(): Promise<void> {
  saving.value = true
  problem.value = null
  try {
    state.data.value = await call(api.PUT('/anonymous-access', { body: { roles: roles.value } }))
    notify.success(t('admin.anonymous.saved'))
  } catch (error) {
    problem.value = error
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.anon__title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.anon__row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 10px;
}

.anon__id {
  margin-left: 6px;
  font-size: var(--fs-12);
}

.anon__preview {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--status-degraded-bg);
  color: var(--text-primary);
  font-size: var(--fs-13);
}

.anon__preview .q-icon {
  color: var(--status-degraded);
  flex: none;
}

.anon__preview--off {
  background: var(--status-ok-bg);
}

.anon__preview--off .q-icon {
  color: var(--status-ok);
}

.anon__repos {
  color: var(--text-secondary);
}
</style>
