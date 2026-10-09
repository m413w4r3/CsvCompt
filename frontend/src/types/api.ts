export interface HealthResponse {
  status: 'ok' | 'degraded' | 'unavailable'
  api_version: string
  storage_status: 'ok' | 'degraded' | 'unavailable'
  llm_enabled: boolean
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: Record<string, unknown> | null }
}

export type ApiHealthState =
  | { kind: 'loading' }
  | { kind: 'ok'; data: HealthResponse }
  | { kind: 'offline' }
  | { kind: 'error'; message: string }
