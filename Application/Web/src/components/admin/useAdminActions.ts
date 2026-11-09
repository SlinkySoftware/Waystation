// SPDX-License-Identifier: GPL-3.0-or-later
// Confirmed administrative actions on users (disable/enable, unlock, sign out everywhere, delete) and the way a refusal
// is shown: a delegation (403) or rule (409) refusal opens a dialog naming the reason, anything else is a toast.
import { useQuasar } from 'quasar'

import { api, ApiError, call, errorMessage } from '@/api/client'
import type { UserDetail, UserSummary } from '@/api/types'
import { useNotify } from '@/composables/useNotify'
import { t } from '@/i18n'

type AnyUser = UserSummary | UserDetail

export function useProblemDialog() {
  const $q = useQuasar()
  const notify = useNotify()
  return (error: unknown): void => {
    if (error instanceof ApiError && (error.status === 403 || error.status === 409)) {
      const text = errorMessage(error)
      $q.dialog({
        title: t(error.status === 403 ? 'admin.problem.forbidden' : 'admin.problem.conflict'),
        message: text.charAt(0).toUpperCase() + text.slice(1),
        ok: { flat: true, noCaps: true, label: t('common.close') },
      })
    } else notify.error(error)
  }
}

export function useUserActions(onChanged: (user: UserDetail | null) => void) {
  const $q = useQuasar()
  const notify = useNotify()
  const showProblem = useProblemDialog()

  function confirm(opts: { title: string; message: string; ok: string; danger?: boolean; prompt?: string }, run: () => Promise<void>): void {
    $q.dialog({
      title: opts.title,
      message: opts.message,
      ...(opts.prompt ? { prompt: { model: '', type: 'text', label: t('admin.users.deleteConfirm'), isValid: (v: string) => v === opts.prompt, outlined: true, dense: true } } : {}),
      cancel: { flat: true, noCaps: true, label: t('common.cancel') },
      ok: { unelevated: true, noCaps: true, color: opts.danger ? 'negative' : 'primary', label: opts.ok },
      persistent: true,
    }).onOk(() => void run().catch(showProblem))
  }

  async function setDisabled(user: AnyUser, disabled: boolean): Promise<void> {
    const updated = await call(api.PATCH('/users/{user_id}', { params: { path: { user_id: user.id } }, body: { disabled } }))
    notify.success(t(disabled ? 'admin.users.disabled' : 'admin.users.enabled', { name: user.username }))
    onChanged(updated)
  }

  return {
    showProblem,
    toggleDisabled(user: AnyUser): void {
      if (user.disabled) void setDisabled(user, false).catch(showProblem)
      else confirm({ title: t('admin.users.disableTitle', { name: user.username }), message: t('admin.users.disableText'), ok: t('admin.users.disable'), danger: true }, () => setDisabled(user, true))
    },
    unlock(user: AnyUser): void {
      void call(api.POST('/users/{user_id}/unlock', { params: { path: { user_id: user.id } } }))
        .then((updated) => {
          notify.success(t('admin.users.unlocked', { name: user.username }))
          onChanged(updated)
        })
        .catch(showProblem)
    },
    signOut(user: AnyUser): void {
      confirm({ title: t('admin.users.signOutTitle', { name: user.username }), message: t('admin.users.signOutText'), ok: t('admin.users.signOut') }, async () => {
        const result = await call(api.DELETE('/users/{user_id}/sessions', { params: { path: { user_id: user.id } } }))
        notify.success(t('admin.users.signedOut', { count: result.revoked }))
        onChanged(null)
      })
    },
    remove(user: AnyUser, after?: () => void): void {
      confirm({ title: t('admin.users.deleteTitle', { name: user.username }), message: t('admin.users.deleteText'), ok: t('admin.users.delete'), danger: true, prompt: user.username }, async () => {
        await call(api.DELETE('/users/{user_id}', { params: { path: { user_id: user.id } } }))
        notify.success(t('admin.users.deleted', { name: user.username }))
        if (after) after()
        else onChanged(null)
      })
    },
  }
}
