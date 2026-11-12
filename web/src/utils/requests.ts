export const resultLabels: Record<string, string> = {
  success: '成功',
  error: '失败',
  rate_limited: '限流',
  cancelled: '已取消',
  timeout: '超时',
  interrupted: '服务中断',
  running: '执行中',
}
export const protocolLabels: Record<string, string> = { gemini: 'Gemini', openai: 'OpenAI', claude: 'Claude' }
export const stageLabels: Record<string, string> = {
  attempt_error: '上游异常',
  received: '接收请求',
  signing: '生成签名',
  signed: '签名完成',
  configured: '解析生成参数',
  thinking: '思考输出',
  answering: '回复输出',
  upstream: '请求上游',
  upstream_end: '尝试结束',
  streaming: '流式输出',
  response: '发送响应',
  finished: '调用结束',
}
export function duration(value?: number | null) {
  return value == null ? '—' : value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(2)}s`
}
export function resultClass(result: string) {
  return result === 'success'
    ? 'bg-emerald-50 text-emerald-700'
    : result === 'running'
      ? 'bg-sky-50 text-sky-700'
      : result === 'rate_limited'
        ? 'bg-amber-50 text-amber-800'
        : result === 'cancelled'
          ? 'bg-slate-100 text-slate-600'
          : 'bg-rose-50 text-rose-700'
}
export function downloadJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
