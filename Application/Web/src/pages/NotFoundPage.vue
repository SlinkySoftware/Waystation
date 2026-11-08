<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<template>
  <q-page class="ws-page">
    <q-card>
      <EmptyState illustration="notFound" :title="t('notFound.title')" :text="t('notFound.text')">
        <q-btn unelevated no-caps color="primary" :label="t('placeholder.back')" :to="{ name: 'dashboard' }" />
      </EmptyState>
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
import EmptyState from '@/components/EmptyState.vue'
import { t } from '@/i18n'
</script>
