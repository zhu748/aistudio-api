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

  /**
   * 非安全上下文兜底：管理面板常以 http://<服务器IP>:8080 访问，
   * navigator.clipboard 在非 secure context 下是 undefined（不是权限问题），
   * 退化为隐藏 textarea + execCommand('copy')。
   */
  function legacyCopy(text: string): boolean {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.focus()
      ta.select()
      const ok = document.execCommand('copy')
      ta.remove()
      return ok
    } catch {
      return false
    }
  }

  async function copy(text: string, key: string | number, message = '已复制到剪贴板') {
    let ok = false
    let unsupportedContext = false
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      try {
        await navigator.clipboard.writeText(text)
        ok = true
      } catch {
        ok = false
      }
    } else {
      unsupportedContext = true
    }
    if (!ok) ok = legacyCopy(text)
    if (!ok) {
      toast.error(
        unsupportedContext
          ? '复制失败：当前为 HTTP 环境，请改用 HTTPS 或 localhost 访问面板后复制'
          : '复制失败，浏览器可能拒绝了剪贴板权限',
      )
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
