<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Members of a local group: add users (search by username prefix), remove one. Refusals (delegation 403, the last
     administrator 409) are shown above the list. -->
<template>
  <q-dialog v-model="open" :maximized="$q.screen.lt.sm" @hide="emit('closed')">
    <q-card class="members" :style="$q.screen.lt.sm ? undefined : { width: '600px', maxWidth: '96vw' }">
      <header class="members__head">
        <div class="col">
          <div class="members__title">{{ t('admin.members.title', { name: group?.name ?? '' }) }}</div>
          <div class="members__caption">{{ t('admin.members.caption') }}</div>
        </div>
        <q-btn v-close-popup flat round dense :icon="iconClose" :aria-label="t('common.close')" />
      </header>
      <div class="members__body">
        <ProblemBanner :error="problem" />
        <div class="members__add">
          <q-select
            v-model="picked"
            outlined
            dense
            multiple
            use-input
            use-chips
            emit-value
            map-options
            input-debounce="200"
            :options="candidates"
            :label="t('admin.members.add')"
            :placeholder="picked.length ? undefined : t('admin.members.addPlaceholder')"
            class="col"
            @filter="search"
          />
          <q-btn unelevated no-caps color="primary" :icon="iconGroupAdd" :label="t('admin.members.addSubmit')" :disable="!picked.length" :loading="adding" @click="add" />
        </div>

        <div v-if="list.error.value" class="text-negative">{{ t('admin.members.loadFailed') }}: {{ errorMessage(list.error.value) }}</div>
        <div v-else-if="!list.items.value.length && !list.loading.value" class="members__empty text-faint">{{ t('admin.members.empty') }}</div>
        <ul v-else class="members__list">
          <li v-for="u in list.items.value" :key="u.id" class="members__row">
            <UserCell :user="u" class="col" />
            <SourceBadge :source="u.source" class="gt-xs" />
            <q-btn flat round dense size="sm" :icon="iconPersonRemove" :aria-label="t('admin.members.remove', { name: u.username })" :loading="removing === u.id" @click="remove(u)">
              <q-tooltip>{{ t('admin.members.remove', { name: u.username }) }}</q-tooltip>
            </q-btn>
          </li>
        </ul>
        <div v-if="list.nextCursor.value" class="members__more">
          <q-btn outline no-caps :loading="list.loading.value" :label="t('common.loadMore')" @click="list.loadMore" />
        </div>
      </div>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { ref, watch } from 'vue'

import { api, call, errorMessage } from '@/api/client'
import type { Group, UserSummary } from '@/api/types'
import { useCursorList } from '@/composables/useCursorList'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'
import { iconClose, iconGroupAdd, iconPersonRemove } from '@/utils/icons'

import ProblemBanner from './ProblemBanner.vue'
import SourceBadge from './SourceBadge.vue'
import UserCell from './UserCell.vue'

const props = defineProps<{ group: Group | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ changed: []; closed: [] }>()
const $q = useQuasar()
const notify = useNotify()

const picked = ref<string[]>([])
const candidates = ref<{ value: string; label: string }[]>([])
const adding = ref(false)
const removing = ref<string | null>(null)
const problem = ref<unknown>(null)

const list = useCursorList((cursor) => {
  if (!props.group || !open.value) return Promise.resolve({ items: [], next_cursor: null })
  return call(api.GET('/groups/{group_id}/members', { params: { path: { group_id: props.group.id }, query: { cursor: cursor ?? undefined, limit: 100 } } }))
}, [() => props.group?.id, open])

watch(open, (isOpen) => {
  if (!isOpen) return
  picked.value = []
  problem.value = null
})

function search(value: string, update: (fn: () => void) => void): void {
  const q = value.trim().toLowerCase()
  void call(api.GET('/users', { params: { query: { q: q || undefined, limit: 20 } } }))
    .then((page) => {
      const members = new Set(list.items.value.map((u) => u.id))
      update(() => {
        candidates.value = page.items.filter((u) => !members.has(u.id)).map((u) => ({ value: u.id, label: `${u.username} (${u.display_name})` }))
      })
    })
    .catch(() => update(() => (candidates.value = [])))
}

async function add(): Promise<void> {
  if (!props.group) return
  adding.value = true
  problem.value = null
  try {
    await call(api.POST('/groups/{group_id}/members', { params: { path: { group_id: props.group.id } }, body: { user_ids: picked.value } }))
    notify.success(t('admin.members.added', { count: picked.value.length }))
    picked.value = []
    await list.reload()
    emit('changed')
  } catch (error) {
    problem.value = error
  } finally {
    adding.value = false
  }
}

async function remove(user: UserSummary): Promise<void> {
  if (!props.group) return
  removing.value = user.id
  problem.value = null
  try {
    await call(api.DELETE('/groups/{group_id}/members/{user_id}', { params: { path: { group_id: props.group.id, user_id: user.id } } }))
    notify.success(t('admin.members.removed', { name: user.username, group: props.group.name }))
    await list.reload()
    emit('changed')
  } catch (error) {
    problem.value = error
  } finally {
    removing.value = null
  }
}
</script>

<style scoped>
.members {
  display: flex;
  flex-direction: column;
  max-height: 88vh;
}

.members__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 20px 12px;
  border-bottom: 1px solid var(--border);
}

.members__title {
  font-size: var(--fs-16);
  font-weight: 650;
}

.members__caption {
  margin-top: 2px;
  font-size: var(--fs-13);
  color: var(--text-secondary);
}

.members__body {
  padding: 16px 20px 20px;
  overflow-y: auto;
}

.members__add {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 14px;
}

.members__list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.members__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}

.members__row:last-child {
  border-bottom: 0;
}

.members__empty {
  padding: 16px 0;
}

.members__more {
  display: flex;
  justify-content: center;
  margin-top: 10px;
}
</style>
