<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<template>
  <q-layout view="hHh lpR fFf">
    <q-page-container>
      <router-view />
    </q-page-container>
  </q-layout>
</template>

<script setup lang="ts">
defineOptions({ name: 'BlankLayout' })
</script>
