<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '@/components/layout/AppLayout.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

const route = useRoute()
const isLoginPage = computed(() => route.meta.public === true)
</script>

<template>
  <div class="min-h-screen bg-slate-50 text-slate-900">
    <ConfirmDialog />
    <router-view v-if="isLoginPage" />
    <AppLayout v-else>
      <router-view />
    </AppLayout>
  </div>
</template>
