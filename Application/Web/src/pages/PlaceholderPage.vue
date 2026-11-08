<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Pages on the plan but not built in this milestone: say so plainly, and where it is tracked. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="title" />
    <q-card>
      <EmptyState illustration="soon" :title="t('placeholder.title', { page: title })" :text="t('placeholder.text', { task: route.meta.planned ?? '' })">
        <q-btn unelevated no-caps color="primary" :label="t('placeholder.back')" :to="{ name: 'dashboard' }" />
      </EmptyState>
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { t } from '@/i18n'

const route = useRoute()
const title = computed(() => (route.meta.title ? t(route.meta.title) : ''))
</script>
