<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Shown by the router guard instead of a page whose capability the session lacks (SPEC §4.10): calm, no request
     that would only be refused, a way back. -->
<template>
  <q-page class="ws-page">
    <q-card>
      <EmptyState illustration="notFound" :title="t('noAccess.title')" :text="page ? t('noAccess.text', { page }) : t('noAccess.textGeneric')">
        <q-btn unelevated no-caps color="primary" :label="t('placeholder.back')" :to="{ name: 'dashboard' }" />
      </EmptyState>
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import EmptyState from '@/components/EmptyState.vue'
import { t } from '@/i18n'

const route = useRoute()
/** The path the user asked for (shown as text only; never navigated to from here). */
const page = computed(() => (typeof route.query.page === 'string' && route.query.page.startsWith('/') ? route.query.page.slice(0, 200) : null))
</script>
