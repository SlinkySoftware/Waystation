<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A failed load: the error with a retry action, or, when the server refused it (403: the session lacks the
     permission, SPEC §4.10), the calm "not available to you" state without a retry that would only fail again. -->
<template>
  <EmptyState v-if="refused" :compact="compact" illustration="notFound" :title="t('noAccess.sectionTitle')" :text="t('noAccess.sectionText')" />
  <EmptyState v-else :compact="compact" :icon="iconError" :title="title" :text="errorMessage(error)">
    <q-btn unelevated no-caps color="primary" :label="t('common.retry')" :loading="loading" @click="emit('retry')" />
  </EmptyState>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { errorMessage, isRefused } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import { t } from '@/i18n'
import { iconError } from '@/utils/icons'

const props = withDefaults(defineProps<{ error: unknown; title: string; loading?: boolean; compact?: boolean }>(), { loading: false, compact: false })
const emit = defineEmits<{ retry: [] }>()

const refused = computed(() => isRefused(props.error))
</script>
