<script setup lang="ts">
import { useConfirm } from '@/composables/useConfirm.ts'
import Modal from './Modal.vue'
import Button from './Button.vue'
import { AlertTriangle, Info } from 'lucide-vue-next'

const { isOpen, options, handleConfirm, handleCancel } = useConfirm()
</script>

<template>
  <Modal
    :model-value="isOpen"
    max-width="sm"
    :title="options.title || '操作确认'"
    @update:model-value="(val) => !val && handleCancel()"
  >
    <div class="flex items-start gap-3.5">
      <div
        class="w-9 h-9 rounded-xl flex items-center justify-center shrink-0 border"
        :class="[
          options.variant === 'danger'
            ? 'bg-rose-50 text-rose-600 border-rose-200/70'
            : options.variant === 'warning'
              ? 'bg-amber-50 text-amber-600 border-amber-200/70'
              : 'bg-brand-50 text-brand-600 border-brand-200/70',
        ]"
      >
        <AlertTriangle
          v-if="options.variant === 'danger' || options.variant === 'warning'"
          class="w-4 h-4"
        />
        <Info
          v-else
          class="w-4 h-4"
        />
      </div>
      <div class="text-[13px] text-slate-600 leading-relaxed pt-0.5">
        {{ options.message }}
      </div>
    </div>

    <template #footer>
      <Button
        variant="ghost"
        size="sm"
        @click="handleCancel"
      >
        {{ options.cancelText || '取消' }}
      </Button>
      <Button
        :variant="options.variant === 'danger' ? 'danger' : 'primary'"
        size="sm"
        @click="handleConfirm"
      >
        {{ options.confirmText || '确认' }}
      </Button>
    </template>
  </Modal>
</template>
