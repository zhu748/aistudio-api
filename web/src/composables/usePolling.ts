import { onUnmounted } from 'vue'

export interface PollingOptions {
  /** 任务标识（可选，仅用于调试标注；去重由调用方自行保证） */
  key?: string
  /** 是否在挂载或恢复可见时立即执行一次（默认 true） */
  immediate?: boolean
  /** 最大错误退避间隔（毫秒，默认 60000ms） */
  maxBackoffMs?: number
}

export interface PollingHandle {
  /** 立即执行一次回调 */
  run: () => void
  /** 暂停轮询（可恢复） */
  pause: () => void
  /** 恢复轮询 */
  resume: () => void
}

interface ActiveTask {
  fn: () => void | Promise<void>
  intervalMs: number
  maxBackoffMs: number
  timer?: number
  paused: boolean
  running: boolean
  failureCount: number
}

// 用 Set 而非以 key 为键的 Map：以 key 为键时两个同 key 消费者会互相覆盖
// 注册项，先卸载者还会无条件删掉共享条目，使另一个的计时器不再被页面隐藏冻结。
const activeTasks = new Set<ActiveTask>()
let visibilityListenerRegistered = false

function setupGlobalVisibilityListener() {
  if (visibilityListenerRegistered || typeof window === 'undefined') return
  visibilityListenerRegistered = true

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      for (const task of activeTasks.values()) {
        stopTaskTimer(task)
      }
    } else {
      for (const task of activeTasks.values()) {
        if (!task.paused) {
          task.failureCount = 0
          void runTask(task)
          scheduleNext(task)
        }
      }
    }
  })
}

async function runTask(task: ActiveTask) {
  if (task.running || (typeof document !== 'undefined' && document.hidden)) return
  task.running = true
  try {
    await task.fn()
    task.failureCount = 0
  } catch (err) {
    task.failureCount++
    console.debug('[usePolling] task execution error (consecutive failures: %d):', task.failureCount, err)
  } finally {
    task.running = false
  }
}

function scheduleNext(task: ActiveTask) {
  if (task.timer || task.paused || (typeof document !== 'undefined' && document.hidden)) return

  let delay = task.intervalMs
  if (task.failureCount > 1) {
    delay = Math.min(task.intervalMs * Math.pow(1.5, task.failureCount - 1), task.maxBackoffMs)
  }

  task.timer = window.setTimeout(() => {
    task.timer = undefined
    void runTask(task).then(() => {
      scheduleNext(task)
    })
  }, delay)
}

function stopTaskTimer(task: ActiveTask) {
  if (task.timer) {
    clearTimeout(task.timer)
    task.timer = undefined
  }
}

/**
 * 页面可见时周期性调度的统一轮询管理。
 * 支持页面隐藏自动冻结、失败指数退避与销毁自动释放。
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  options: PollingOptions = {}
): PollingHandle {
  setupGlobalVisibilityListener()

  const immediate = options.immediate !== false
  const maxBackoffMs = options.maxBackoffMs || 60000

  const task: ActiveTask = {
    fn,
    intervalMs,
    maxBackoffMs,
    paused: false,
    running: false,
    failureCount: 0,
  }
  activeTasks.add(task)

  if (typeof window !== 'undefined' && window.location.protocol.startsWith('http')) {
    if (!document.hidden) {
      if (immediate) {
        void runTask(task).then(() => {
          scheduleNext(task)
        })
      } else {
        scheduleNext(task)
      }
    }
  }

  const handle: PollingHandle = {
    run: () => {
      void runTask(task)
    },
    pause: () => {
      task.paused = true
      stopTaskTimer(task)
    },
    resume: () => {
      task.paused = false
      task.failureCount = 0
      scheduleNext(task)
    },
  }

  onUnmounted(() => {
    // 先置 paused：在途任务的 `.then(() => scheduleNext(task))` 会读取该标志，
    // 否则卸载后仍会被重新排期成脱离 activeTasks 管理的「僵尸轮询」。
    task.paused = true
    stopTaskTimer(task)
    activeTasks.delete(task)
  })

  return handle
}
