<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Profile (UI_DESIGN §4.11): who you are and your API tokens. Theme lives in the user menu. -->
<template>
  <q-page class="ws-page">
    <PageHeader :title="t('nav.profile')" :caption="t('profile.caption')" />
    <div class="profile">
      <PageCard :title="t('profile.account')">
        <dl class="profile__facts">
          <div>
            <dt>{{ t('profile.displayName') }}</dt>
            <dd>{{ user?.display_name }}</dd>
          </div>
          <div>
            <dt>{{ t('profile.username') }}</dt>
            <dd class="mono">{{ user?.username }}</dd>
          </div>
          <div v-if="user?.email">
            <dt>{{ t('profile.email') }}</dt>
            <dd>{{ user.email }}</dd>
          </div>
          <div>
            <dt>{{ t('profile.source') }}</dt>
            <dd>{{ user?.source === 'local' ? t('profile.sourceLocal') : user?.source }}</dd>
          </div>
        </dl>
      </PageCard>
      <ApiTokensCard />
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import PageCard from '@/components/PageCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import ApiTokensCard from '@/components/tokens/ApiTokensCard.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const user = computed(() => auth.user)
</script>

<style scoped>
.profile {
  display: grid;
  gap: 16px;
}

.profile__facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px 24px;
  margin: 0;
}

.profile__facts dt {
  font-size: var(--fs-12);
  color: var(--text-secondary);
}

.profile__facts dd {
  margin: 2px 0 0;
  font-weight: 500;
  overflow-wrap: anywhere;
}
</style>
