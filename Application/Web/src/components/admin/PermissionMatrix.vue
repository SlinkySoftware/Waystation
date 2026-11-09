<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A role's permissions as a matrix: scope rows × repository actions (write/delete/manage tick browse and read too),
     each row showing how many repositories its scope matches now; global actions below. Read-only for built-ins. -->
<template>
  <div class="matrix">
    <div class="matrix__section-head">
      <div class="matrix__heading">{{ t('admin.roleForm.repoPermissions') }}</div>
      <div class="matrix__help">{{ t('admin.roleForm.repoPermissionsHelp') }}</div>
    </div>
    <div class="matrix__wrap">
      <table class="matrix__table">
        <thead>
          <tr>
            <th class="matrix__scope-col">{{ t('admin.roleForm.scope') }}</th>
            <th v-for="a in REPO_ACTIONS" :key="a">
              {{ t(`admin.action.${a}`) }}
              <q-tooltip>{{ t(`admin.actionHelp.${a}`) }}</q-tooltip>
            </th>
            <th class="matrix__match-col">{{ t('admin.roleForm.matchesTitle') }}</th>
            <th v-if="!readonly" class="matrix__remove-col"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in model.rows" :key="i" :class="{ 'matrix__row--bad': problems.rows[i] }">
            <td class="matrix__scope-col">
              <span v-if="readonly" class="mono">{{ row.scope }}</span>
              <q-input
                v-else
                :model-value="row.scope"
                dense
                outlined
                hide-bottom-space
                input-class="mono"
                :aria-label="t('admin.roleForm.scope')"
                :error="Boolean(problems.rows[i])"
                :error-message="rowError(i)"
                @update:model-value="(v) => setScope(i, String(v ?? ''))"
              />
            </td>
            <td v-for="a in REPO_ACTIONS" :key="a" class="matrix__cell">
              <q-checkbox
                :model-value="row.actions.includes(a)"
                dense
                :disable="readonly"
                :aria-label="`${t(`admin.action.${a}`)} · ${row.scope}`"
                @update:model-value="(on: boolean) => setAction(i, a, on)"
              />
            </td>
            <td class="matrix__match-col">
              <span class="text-muted num">{{ t('admin.roleForm.matches', { count: matchCount(row.scope) }) }}</span>
              <q-tooltip v-if="matchNames(row.scope).length">{{ matchNames(row.scope).join(', ') }}</q-tooltip>
            </td>
            <td v-if="!readonly" class="matrix__remove-col">
              <q-btn flat round dense size="sm" :icon="iconClose" :aria-label="t('admin.roleForm.removeRow', { scope: row.scope })" @click="removeRow(i)" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="matrix__foot">
      <q-btn v-if="!readonly" flat dense no-caps color="primary" :icon="iconAdd" :label="t('admin.roleForm.addRow')" @click="addRow" />
      <span class="matrix__help">{{ t('admin.roleForm.impliedHint') }}</span>
    </div>

    <div class="matrix__section-head matrix__section-head--global">
      <div class="matrix__heading">{{ t('admin.roleForm.globalPermissions') }}</div>
      <div class="matrix__help">{{ t('admin.roleForm.globalHelp') }}</div>
    </div>
    <div class="matrix__globals">
      <label v-for="a in GLOBAL_ACTIONS" :key="a" class="matrix__global" :class="{ 'matrix__global--on': model.global.includes(a), 'matrix__global--off': !readonly && !allowed(a) }">
        <q-checkbox :model-value="model.global.includes(a)" dense :disable="readonly || !allowed(a)" @update:model-value="(on: boolean) => setGlobal(a, on)" />
        <span>
          <span class="matrix__global-name">{{ t(`admin.action.${a}`) }}</span>
          <span class="matrix__global-help">{{ allowed(a) || readonly ? t(`admin.actionHelp.${a}`) : t('tokens.form.notHeld') }}</span>
        </span>
      </label>
    </div>
    <div v-if="!readonly && problems.empty" class="matrix__error" role="alert">{{ t('admin.roleForm.empty') }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'

import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { useRepositoriesStore } from '@/stores/repositories'
import { iconAdd, iconClose } from '@/utils/icons'
import { GLOBAL_ACTIONS, type GlobalAction, type PermissionMatrix, REPO_ACTIONS, type RepoAction, scopeMatches } from '@/utils/rbac'

import { matrixProblems, mayAssignRole, toggleAction } from './forms'

const props = withDefaults(defineProps<{ readonly?: boolean; held?: GlobalAction[] }>(), { readonly: false, held: () => [] })
const model = defineModel<PermissionMatrix>({ required: true })
const auth = useAuthStore()
const repos = useRepositoriesStore()
onMounted(() => void repos.load().catch(() => undefined))

const problems = computed(() => matrixProblems(model.value))

function rowError(i: number): string | undefined {
  const p = problems.value.rows[i]
  if (p === 'scope') return t('admin.roleForm.scopeInvalid')
  if (p === 'duplicate') return t('admin.roleForm.scopeDuplicate')
  if (p === 'actions') return t('admin.roleForm.scopeNoActions')
  return undefined
}

/** Global actions the caller may add (those already on the role may stay). */
function allowed(action: GlobalAction): boolean {
  return props.held.includes(action) || mayAssignRole({ permissions: [{ action, scope: '*' }] }, auth.can)
}

const matchNames = (scope: string) => (scope.trim() ? repos.items.filter((r) => scopeMatches(scope.trim(), r)).map((r) => r.name) : [])
const matchCount = (scope: string) => matchNames(scope).length

/** Change a copy of the matrix and emit it. */
function update(change: (m: PermissionMatrix) => void): void {
  const copy = { rows: model.value.rows.map((r) => ({ ...r, actions: [...r.actions] })), global: [...model.value.global] }
  change(copy)
  model.value = copy
}

function setScope(i: number, scope: string): void {
  update((m) => {
    m.rows[i]!.scope = scope
  })
}

function setAction(i: number, a: RepoAction, on: boolean): void {
  update((m) => {
    m.rows[i]!.actions = toggleAction(m.rows[i]!.actions, a, on)
  })
}

function addRow(): void {
  update((m) => {
    m.rows.push({ scope: '', actions: ['browse', 'read'] })
  })
}

function removeRow(i: number): void {
  update((m) => {
    m.rows.splice(i, 1)
  })
}

function setGlobal(a: GlobalAction, on: boolean): void {
  update((m) => {
    m.global = on ? GLOBAL_ACTIONS.filter((x) => x === a || m.global.includes(x)) : m.global.filter((x) => x !== a)
  })
}
</script>

<style scoped>
.matrix__section-head {
  margin-bottom: 8px;
}

.matrix__section-head--global {
  margin-top: 18px;
}

.matrix__heading {
  font-size: var(--fs-13);
  font-weight: 650;
}

.matrix__help {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.matrix__wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.matrix__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-13);
}

.matrix__table th {
  padding: 8px 6px;
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-secondary);
  text-align: center;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  background: var(--surface-sunken);
}

.matrix__table td {
  padding: 6px;
  border-bottom: 1px solid var(--border);
  text-align: center;
  vertical-align: top;
}

.matrix__table tbody tr:last-child td {
  border-bottom: 0;
}

.matrix__cell {
  padding-top: 10px !important;
}

.matrix__scope-col {
  text-align: left !important;
  min-width: 190px;
}

.matrix__table td.matrix__scope-col > .mono {
  display: inline-block;
  padding-top: 4px;
}

.matrix__match-col {
  white-space: nowrap;
  padding-top: 12px !important;
  font-size: var(--fs-12);
}

.matrix__remove-col {
  width: 36px;
  padding-top: 8px !important;
}

.matrix__foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  margin-top: 6px;
}

.matrix__globals {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 8px;
}

.matrix__global {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.matrix__global--on {
  border-color: var(--q-primary);
}

.matrix__global--off {
  opacity: 0.6;
}

.matrix__global-name {
  display: block;
  font-weight: 600;
  font-size: var(--fs-13);
}

.matrix__global-help {
  display: block;
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.matrix__error {
  margin-top: 10px;
  font-size: var(--fs-13);
  color: var(--cache-error);
}
</style>
