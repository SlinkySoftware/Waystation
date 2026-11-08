<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Browse renderer framework: picks the format-aware view; raw gets the folder listing; formats whose renderer
     has not shipped yet (yum) get a clear "coming" state instead of an empty or broken tree. -->
<template>
  <DockerBrowser v-if="repo.format === 'docker'" :repo="repo" />
  <NpmBrowser v-else-if="repo.format === 'npm'" :repo="repo" />
  <AptBrowser v-else-if="repo.format === 'apt'" :repo="repo" />
  <PageCard v-else-if="repo.format === 'yum'">
    <EmptyState illustration="soon" :title="t('browse.yum.soonTitle')" :text="t('browse.yum.soonText')" />
  </PageCard>
  <FolderBrowser v-else :repo="repo" />
</template>

<script setup lang="ts">
import type { Repository } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageCard from '@/components/PageCard.vue'
import { t } from '@/i18n'

import AptBrowser from './AptBrowser.vue'
import DockerBrowser from './DockerBrowser.vue'
import FolderBrowser from './FolderBrowser.vue'
import NpmBrowser from './NpmBrowser.vue'

defineProps<{ repo: Repository }>()
</script>
