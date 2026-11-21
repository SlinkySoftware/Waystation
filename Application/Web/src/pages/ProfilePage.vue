<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Profile (UI_DESIGN §4.11): who you are (display name and e-mail editable for local accounts), appearance (theme
     and density), password and two-factor sign-in (local accounts), API tokens and your browser sessions. While a
     one-time password or two-factor setup is pending, only the cards that resolve it are shown. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.profile')" :caption="t('profile.caption')" />
    <div class="profile">
      <div class="profile__pair">
        <AccountCard />
        <AppearanceCard />
      </div>
      <div v-if="local" class="profile__pair">
        <ChangePasswordCard />
        <TwoFactorCard v-if="!auth.mustChangePassword" />
      </div>
      <ApiTokensCard v-if="unlocked" />
      <MySessionsCard v-if="unlocked" />
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import AccountCard from '@/components/account/AccountCard.vue'
import AppearanceCard from '@/components/account/AppearanceCard.vue'
import ChangePasswordCard from '@/components/account/ChangePasswordCard.vue'
import MySessionsCard from '@/components/account/MySessionsCard.vue'
import TwoFactorCard from '@/components/account/TwoFactorCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import ApiTokensCard from '@/components/tokens/ApiTokensCard.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const local = computed(() => auth.user?.source === 'local')
const unlocked = computed(() => !auth.mustChangePassword && !auth.totpSetupRequired)
</script>

<style scoped>
.profile {
  display: grid;
  gap: 16px;
}

.profile__pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
}

@media (max-width: 1023px) {
  .profile__pair {
    grid-template-columns: 1fr;
  }
}
</style>
