export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

const TOKEN_KEY = 'asp_api_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token: string): void {
  if (token.trim()) {
    localStorage.setItem(TOKEN_KEY, token.trim())
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

type UnauthorizedHandler = () => void
let onUnauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorizedHandler = handler
}

// 默认请求超时：fetch 本身没有超时，后端卡住时前端会无限等待
const DEFAULT_TIMEOUT_MS = 30000

/** request 额外支持的选项（不属于 RequestInit 的部分）。 */
export interface RequestOptions extends RequestInit {
  /** 覆盖默认超时（毫秒）。 */
  timeoutMs?: number
}

export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs, ...init } = options
  const headers = new Headers(init.headers || {})
  const token = getToken()

  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  if (!headers.has('Content-Type') && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }

  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json')
  }

  // 调用方已自带 signal 时不覆盖，否则用 AbortController 施加默认超时
  let timeoutId: ReturnType<typeof setTimeout> | undefined
  let signal = init.signal
  if (!signal && typeof AbortController !== 'undefined') {
    const controller = new AbortController()
    timeoutId = setTimeout(() => controller.abort(), timeoutMs ?? DEFAULT_TIMEOUT_MS)
    signal = controller.signal
  }

  let response: Response
  let text: string
  try {
    try {
      response = await fetch(url, {
        ...init,
        headers,
        signal,
      })
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new ApiError(0, '请求超时，请稍后重试')
      }
      throw err
    }

    if (!response.ok) {
      if (response.status === 401) {
        clearToken()
        if (onUnauthorizedHandler) {
          onUnauthorizedHandler()
        } else if (
          typeof window !== 'undefined' &&
          !window.location.pathname.startsWith('/login')
        ) {
          const redirect = encodeURIComponent(
            window.location.pathname + window.location.search
          )
          window.location.href = `/login?redirect=${redirect}`
        }
      }

      let errorDetail = `请求失败 (${response.status})`
      try {
        const errJson = await response.json()
        if (errJson && typeof errJson.detail === 'string') {
          errorDetail = errJson.detail
        } else if (errJson && typeof errJson.detail?.message === 'string') {
          // FastAPI 的 detail 可能是对象（本项目大量抛结构化 detail）
          errorDetail = errJson.detail.message
        } else if (errJson && typeof errJson.error?.message === 'string') {
          errorDetail = errJson.error.message
        }
      } catch {
        // ignore json parse error
      }
      throw new ApiError(response.status, errorDetail)
    }

    // 读取响应体也必须在超时窗口内：否则慢速滴流的响应体可以无限挂起
    text = await response.text()
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId)
  }

  // 204 No Content
  if (response.status === 204) {
    return {} as T
  }

  // 200 但响应体为空时，直接 JSON.parse 会抛 SyntaxError，这里按空对象处理
  if (!text.trim()) {
    return {} as T
  }
  return JSON.parse(text) as T
}
