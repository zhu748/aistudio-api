import { ref } from 'vue'

export interface ConfirmOptions {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  variant?: 'danger' | 'warning' | 'primary'
}

const isOpen = ref(false)
const options = ref<ConfirmOptions>({
  title: '操作确认',
  message: '',
  confirmText: '确认',
  cancelText: '取消',
  variant: 'danger',
})

let resolveFn: ((value: boolean) => void) | null = null

export function useConfirm() {
  function confirm(opts: ConfirmOptions | string): Promise<boolean> {
    if (resolveFn) {
      resolveFn(false)
      resolveFn = null
    }

    if (typeof opts === 'string') {
      options.value = {
        title: '操作确认',
        message: opts,
        confirmText: '确认执行',
        cancelText: '取消',
        variant: 'danger',
      }
    } else {
      options.value = {
        title: opts.title || '操作确认',
        message: opts.message,
        confirmText: opts.confirmText || '确认执行',
        cancelText: opts.cancelText || '取消',
        variant: opts.variant || 'danger',
      }
    }

    isOpen.value = true
    return new Promise<boolean>((resolve) => {
      resolveFn = resolve
    })
  }

  function handleConfirm() {
    isOpen.value = false
    if (resolveFn) {
      const r = resolveFn
      resolveFn = null
      r(true)
    }
  }

  function handleCancel() {
    isOpen.value = false
    if (resolveFn) {
      const r = resolveFn
      resolveFn = null
      r(false)
    }
  }

  return {
    isOpen,
    options,
    confirm,
    handleConfirm,
    handleCancel,
  }
}
