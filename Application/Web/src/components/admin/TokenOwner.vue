<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- A token's owner: a user (links to the user page) or a service account. -->
<template>
  <span class="token-owner">
    <q-icon :name="owner.kind === 'user' ? iconPerson : iconServiceAccount" size="16px" class="text-faint" />
    <router-link v-if="owner.kind === 'user'" :to="{ name: 'admin-user', params: { id: owner.id } }" class="text-link" @click.stop>{{ owner.name }}</router-link>
    <span v-else>{{ owner.name }}</span>
  </span>
</template>

<script setup lang="ts">
import type { TokenOwnerRef } from '@/api/types'
import { iconPerson, iconServiceAccount } from '@/utils/icons'

defineProps<{ owner: TokenOwnerRef }>()
</script>

<style scoped>
.token-owner {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
</style>
