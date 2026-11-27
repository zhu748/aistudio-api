import { watch, onUnmounted, type WatchSource } from 'vue'

export interface DebouncedWatchOptions {
  /** 防抖延迟（毫秒，默认 300） */
  delay?: number
  /** 是否立即执行一次（默认 false） */
  immediate?: boolean
  /** 每次源变化同步执行的「前置动作」（如关闭流、清空列表），不参与防抖 */
  onChange?: () => void
}

/**
 * 防抖 watch：源变化后 delay 毫秒才执行一次 callback，期间的变化只保留最后一次；
 * 组件卸载时自动清理定时器并停止监听。
 */
export function useDebouncedWatch(
  source: WatchSource<unknown> | WatchSource<unknown>[],
  callback: () => void | Promise<void>,
  options: DebouncedWatchOptions = {},
): void {
  const delay = options.delay ?? 300
  let timer: ReturnType<typeof setTimeout> | null = null
  const stop = watch(
    source as WatchSource<unknown>,
    () => {
      options.onChange?.()
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        void callback()
      }, delay)
    },
    { immediate: options.immediate },
  )
  onUnmounted(() => {
    if (timer) clearTimeout(timer)
    stop()
  })
}
