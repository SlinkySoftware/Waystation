// SPDX-License-Identifier: GPL-3.0-or-later
// Toasts for results (UI_DESIGN §3), and copy-to-clipboard with feedback.
import { copyToClipboard, useQuasar } from 'quasar'

import { errorMessage } from '@/api/client'
import { t } from '@/i18n'
import { iconCheck, iconError } from '@/utils/icons'

export function useNotify() {
  const $q = useQuasar()
  return {
    success(message: string): void {
      $q.notify({ message, icon: iconCheck, color: 'positive', textColor: 'white', position: 'bottom-right', timeout: 2500 })
    },
    error(error: unknown, prefix?: string): void {
      const text = errorMessage(error)
      $q.notify({ message: prefix ? `${prefix}: ${text}` : text, icon: iconError, color: 'negative', textColor: 'white', position: 'bottom-right', timeout: 6000 })
    },
  }
}

export function useCopy() {
  const notify = useNotify()
  return async (text: string, what?: string): Promise<void> => {
    try {
      await copyToClipboard(text)
      notify.success(what ? t('common.copiedWhat', { what }) : t('common.copied'))
    } catch (error) {
      notify.error(error, t('common.copyFailed'))
    }
  }
}
