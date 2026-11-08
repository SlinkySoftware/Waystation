// SPDX-License-Identifier: GPL-3.0-or-later
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import pluginVue from 'eslint-plugin-vue'

export default defineConfigWithVueTs(
  { ignores: ['dist/**', '.quasar/**', 'node_modules/**', 'public/**', 'src/api/schema.d.ts', 'coverage/**'] },
  pluginVue.configs['flat/recommended'],
  vueTsConfigs.recommended,
  {
    rules: {
      // TypeScript and <script setup> everywhere (CLAUDE.md).
      'vue/block-lang': ['error', { script: { lang: 'ts' } }],
      'vue/component-api-style': ['error', ['script-setup']],
      'vue/define-macros-order': ['error', { order: ['defineProps', 'defineModel', 'defineEmits'] }],
      // Untrusted text (package names, READMEs, upstream errors) is never rendered as HTML.
      'vue/no-v-html': 'error',
      'vue/no-unused-refs': 'error',
      'vue/multi-word-component-names': 'off',
      // Formatting is not linted (no Prettier in this repo yet); keep templates readable by hand.
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/multiline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/attributes-order': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
)
