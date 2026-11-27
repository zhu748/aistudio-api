/**
 * 请求代次守卫：并发 / 乱序返回时只让最新一代写状态，避免旧响应覆盖新数据。
 *
 * ```ts
 * const guard = useLatest()
 * const isLatest = guard.begin()
 * const data = await api()   // 期间若又发起了更新的一代，isLatest() 即为 false
 * if (isLatest()) state.value = data
 * ```
 */
export function useLatest() {
  let latest = 0
  return {
    /** 开启新的一代，返回「是否仍为最新一代」的判定函数 */
    begin(): () => boolean {
      const current = ++latest
      return () => current === latest
    },
    /** 让所有在途代次立即失效（组件卸载、请求标识变更等场景） */
    invalidate(): void {
      latest += 1
    },
  }
}
