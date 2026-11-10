import { onUnmounted, ref } from 'vue'
import { useToastStore } from '@/stores/toast.ts'

/**
 * 复制文本到剪贴板，并在 2 秒内记录“已复制”的键；
 * 同一键重复触发时重置计时。
 */
export function useClipboard() {
  const toast = useToastStore()
  const copied = ref<string | number | null>(null)
  // 计时器放在实例作用域：模块级共享会让两个组件互相取消对方的复位计时，
  // 先复制的那个「已复制」高亮会永久残留。
  let timer: ReturnType<typeof setTimeout> | undefined

  async function copy(text: string, key: string | number, message = '已复制到剪贴板') {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      toast.error('复制失败，浏览器可能拒绝了剪贴板权限')
      return
    }
    copied.value = key
    toast.success(message)
    clearTimeout(timer)
    timer = setTimeout(() => {
      if (copied.value === key) copied.value = null
    }, 2000)
  }

  onUnmounted(() => clearTimeout(timer))

  return { copied, copy }
}
