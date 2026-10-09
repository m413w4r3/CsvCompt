import { request } from './client'
import type { HealthResponse } from '../../types/api'

export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/health', { signal })
}
