import type { ApiErrorBody } from '../../types/api'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details: Record<string, unknown> | null

  constructor(status: number, body: ApiErrorBody['error']) {
    super(body.message)
    this.name = 'ApiError'
    this.code = body.code
    this.status = status
    this.details = body.details
  }
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs = 8_000,
): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new DOMException('Délai dépassé', 'TimeoutError')), timeoutMs)
  const signal = options.signal
    ? AbortSignal.any([options.signal, controller.signal])
    : controller.signal

  try {
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      redirect: 'error',
      signal,
      headers: { Accept: 'application/json', ...options.headers },
    })
    const payload: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined)
    if (!response.ok) {
      const error = payload && typeof payload === 'object' && 'error' in payload
        ? (payload as ApiErrorBody).error
        : { code: 'HTTP_ERROR', message: 'La requête a échoué.', details: null }
      throw new ApiError(response.status, error)
    }
    return payload as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new ApiError(0, { code: 'TIMEOUT', message: 'Le serveur local ne répond pas.', details: null })
    }
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, { code: 'NETWORK_ERROR', message: 'Impossible de joindre le serveur local.', details: null })
  } finally {
    clearTimeout(timer)
  }
}
