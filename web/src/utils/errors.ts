/**
 * 统一提取可展示的错误文案。
 *
 * - Error 实例取 `message`（空串回落到 fallback）
 * - 非空字符串原样返回
 * - 其余（对象、undefined、null、空串）一律回落 fallback
 *
 * 注意：`api/client.ts` 的 ApiError 已经把服务端 `detail`（字符串或
 * `detail.message` / `error.message`）归一化进 `.message`，所以这里
 * 不需要特判 ApiError——它就是一个普通 Error。
 */
export function errorMessage(err: unknown, fallback = '操作失败'): string {
  if (err instanceof Error) return err.message || fallback
  if (typeof err === 'string' && err) return err
  return fallback
}
