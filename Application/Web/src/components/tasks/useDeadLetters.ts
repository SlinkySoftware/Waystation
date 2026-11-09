// SPDX-License-Identifier: GPL-3.0-or-later
// Dead letters for the Tasks page: first page + "Load more", retry (queued again) and discard (confirmed). Both are
// idempotent on the server, so a double click or a stale row is harmless; the list reloads after each.
import { useQuasar } from 'quasar'
import { type Ref, ref, shallowRef, watch } from 'vue'

import { api, call } from '@/api/client'
import type { DeadLetter, DeadLetterPage } from '@/api/types'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'

import { kindLabel } from './tasks'

export function useDeadLetters(enabled: Ref<boolean>, changed: () => void) {
  const $q = useQuasar()
  const notify = useNotify()
  const page = shallowRef<DeadLetterPage | null>(null)
  const rows = shallowRef<DeadLetter[]>([])
  const loading = ref(false)
  const busy = ref<string | null>(null)

  async function reload(): Promise<void> {
    if (!enabled.value) return
    loading.value = true
    try {
      page.value = await call(api.GET('/tasks/dead-letters', { params: { query: { limit: 50 } } }))
      rows.value = page.value.items
    } catch (error) {
      notify.error(error)
    } finally {
      loading.value = false
    }
  }

  async function more(): Promise<void> {
    const cursor = page.value?.next_cursor
    if (!cursor) return
    loading.value = true
    try {
      const next = await call(api.GET('/tasks/dead-letters', { params: { query: { limit: 50, cursor } } }))
      page.value = next
      rows.value = [...rows.value, ...next.items.filter((d) => !rows.value.some((r) => r.id === d.id))]
    } catch (error) {
      notify.error(error)
    } finally {
      loading.value = false
    }
  }

  async function retry(letter: DeadLetter): Promise<void> {
    busy.value = letter.id
    try {
      const job = await call(api.POST('/tasks/dead-letters/{letter_id}/retry', { params: { path: { letter_id: letter.id } } }))
      notify.success(t(job.status === 'merged' ? 'tasks.dead.retriedMerged' : 'tasks.dead.retried', { job: job.job_id }))
    } catch (error) {
      notify.error(error)
    } finally {
      busy.value = null
    }
    await reload()
    changed()
  }

  function discard(letter: DeadLetter): void {
    $q.dialog({
      title: t('tasks.dead.discardTitle'),
      message: t('tasks.dead.discardText', { task: letter.kind ? kindLabel(letter.kind) : t('tasks.dead.malformed') }),
      cancel: { flat: true, noCaps: true, label: t('common.cancel') },
      ok: { unelevated: true, noCaps: true, color: 'negative', label: t('tasks.dead.discard') },
    }).onOk(async () => {
      busy.value = letter.id
      try {
        await call(api.DELETE('/tasks/dead-letters/{letter_id}', { params: { path: { letter_id: letter.id } } }))
        notify.success(t('tasks.dead.discarded'))
      } catch (error) {
        notify.error(error)
      } finally {
        busy.value = null
      }
      await reload()
      changed()
    })
  }

  watch(enabled, () => void reload(), { immediate: true })
  return { page, rows, loading, busy, reload, more, retry, discard }
}
