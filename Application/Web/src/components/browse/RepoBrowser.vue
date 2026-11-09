<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Browse renderer framework: picks the format-aware view; every other format (raw, yum until its package view
     ships) gets the generic folder listing of cached files. -->
<template>
  <DockerBrowser v-if="repo.format === 'docker'" :repo="repo" />
  <NpmBrowser v-else-if="repo.format === 'npm'" :repo="repo" />
  <AptBrowser v-else-if="repo.format === 'apt'" :repo="repo" />
  <HelmBrowser v-else-if="repo.format === 'helm'" :repo="repo" />
  <PypiBrowser v-else-if="repo.format === 'pypi'" :repo="repo" />
  <FolderBrowser v-else :repo="repo" />
</template>

<script setup lang="ts">
import type { Repository } from '@/api/types'

import AptBrowser from './AptBrowser.vue'
import DockerBrowser from './DockerBrowser.vue'
import FolderBrowser from './FolderBrowser.vue'
import HelmBrowser from './HelmBrowser.vue'
import NpmBrowser from './NpmBrowser.vue'
import PypiBrowser from './PypiBrowser.vue'

defineProps<{ repo: Repository }>()
</script>
