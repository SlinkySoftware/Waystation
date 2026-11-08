<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Browse renderer framework: picks the format-aware view; formats without one get the generic folder tree. -->
<template>
  <DockerBrowser v-if="repo.format === 'docker'" :repo="repo" />
  <NpmBrowser v-else-if="repo.format === 'npm'" :repo="repo" />
  <AptBrowser v-else-if="repo.format === 'apt'" :repo="repo" />
  <FolderBrowser v-else :repo="repo" />
</template>

<script setup lang="ts">
import type { Repository } from '@/api/types'

import AptBrowser from './AptBrowser.vue'
import DockerBrowser from './DockerBrowser.vue'
import FolderBrowser from './FolderBrowser.vue'
import NpmBrowser from './NpmBrowser.vue'

defineProps<{ repo: Repository }>()
</script>
