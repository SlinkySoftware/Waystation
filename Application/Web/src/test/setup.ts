// SPDX-License-Identifier: GPL-3.0-or-later
import { config } from '@vue/test-utils'
import { Dialog, Notify, Quasar } from 'quasar'

config.global.plugins = [[Quasar, { plugins: { Notify, Dialog } }]]
