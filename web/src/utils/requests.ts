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
export const sigProtocolLabels: Record<string, string> = {
  gemini: 'Gemini',
  openai: 'OpenAI Chat',
  responses: 'Responses',
  claude: 'Claude',
}
export const sigCarrierLabels: Record<string, string> = {
  function_call: '工具调用 (FC)',
  function_response: '工具结果',
  thought: '思考文本',
  tail_empty: '空尾载体',
  text: '正文文本',
  inline_data: '内联媒体',
  code: '代码执行',
  system: '系统指令',
  other: '其他',
}
export const sigSourceLabels: Record<string, string> = {
  client_native: 'Gemini 原生',
  client_extra_content: 'extra_content',
  client_claude_envelope: 'thinking.signature',
  client_encrypted_content: 'encrypted_content',
  client_reasoning_details: 'reasoning_details',
  client_provider_fields: 'provider_specific_fields',
  client_top_level: '顶层字段',
  client_tool_function: 'tool.function',
  warm_cache: '温缓存恢复',
  cold_cache: '冷缓存恢复',
  skip_fallback: 'SKIP 兜底',
  outbound_wire: '原样发往上游',
  upstream_wire: '上游线缆',
  thoughtSignature: 'thoughtSignature',
  'thinking.signature': 'thinking.signature',
  extra_content: 'extra_content',
  'extra_content+encrypted_content': 'extra_content + encrypted_content',
  dropped: '协议丢弃',
}
export const sigActionLabels: Record<string, string> = {
  warm_hit: '温缓存命中',
  cold_hit: '冷缓存命中',
  skip_fill: 'SKIP 兜底补填',
  conflict: '冲突扣留',
  miss: '未命中',
}
export const sigReasonLabels: Record<string, string> = {
  scope_miss: '作用域未命中（切号/动态Prompt/首遇）',
  unobserved_thought: '未观测历史含无签名思考（保留原样，不伪造 SKIP，允许后续续链缓存）',
  thought_digest_mismatch: '回传思考摘要不一致',
  explicit_owner_mismatch: '显式签名归属不匹配',
  call_id_mismatch: '工具调用 ID 不匹配',
  warm_conflict_withheld: '温缓存同轮多版本冲突',
  cold_conflict_withheld: '冷缓存同轮多版本冲突',
  cold_cohort_mismatch: '冷缓存调用批次校验未通过',
  legacy_seen_old: '旧版缓存观测保留',
  explicit_sig_unobserved: '携带未观测到的外部显式签名',
  chain_broken: '前置轮次断链',
  tail_empty_unrepresentable: '纯文本空尾签名无法映射到当前兼容协议（已交温缓存托管）',
  text_part_unrepresentable: '正文文本载体无法在当前兼容协议携带签名',
  compound_part_unrepresentable: '复合 Part 签名禁止跨字段拆分挪用',
  multi_thought_scalar_limit: 'OpenAI 标量 reasoning 无法承载多片思考签名',
  inline_data_unrepresentable: '内联媒体签名无法映射到当前兼容协议',
  code_part_unrepresentable: '代码执行 Part 签名无法映射到当前兼容协议',
  stop_truncated: '命中停止序列被截断',
  invalid_base64: '非合法 Base64 签名已丢弃',
  foreign_encrypted_content_ignored: '外部 OpenAI encrypted_content 已忽略',
  foreign_claude_signature_ignored: '外部 Anthropic thinking.signature 已忽略',
  foreign_reasoning_details_ignored: '非 Google reasoning_details 已忽略',
  compound_part_stripped: '复合 functionResponse 不可无损表示，签名已剥离',
  lossy_code_stripped: '有损代码 Part 转换，签名已剥离',
  lossy_code_result_stripped: '有损代码结果转换，签名已剥离',
  media_or_server_tool: '含媒体或服务端工具（按策略不入温缓存）',
  too_large: '单轮体积超限未缓存',
  tool_interrupted: '工具流异常中断未缓存',
  scope_incompatible: '历史断链未缓存本轮',
  no_signable_content: '无思考/工具/签名内容',
  conflict_withheld: '同历史多次不同生成已扣留',
}
export const sigKindLabels: Record<string, string> = {
  real: '真签名',
  skip_sentinel: 'SKIP 哨兵',
  invalid: '非法签名',
  missing: '缺失',
}
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
