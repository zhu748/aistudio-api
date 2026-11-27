import type { LogEntry } from '@/types'

/** thoughtSignature 专项日志在运行日志里的 name 标识 */
export const THOUGHT_SIGNATURE_LOG_NAME = 'aistudio.thought_signature'

/** 日志行跳转链接里展示的请求 ID 前缀长度 */
export const SHORT_REQUEST_ID = 16

/**
 * 日志行跳转到请求详情页的路由：thoughtSignature 日志额外定位到签名 Tab。
 * 参数只取日志条目里实际用到的两个字段，便于复用（历史归档、请求详情等）。
 */
export function requestLogRoute(entry: Pick<LogEntry, 'name' | 'request_id'>) {
  return {
    path: '/requests',
    query: {
      request_id: entry.request_id,
      ...(entry.name === THOUGHT_SIGNATURE_LOG_NAME ? { tab: 'signature' } : {}),
    },
  }
}
