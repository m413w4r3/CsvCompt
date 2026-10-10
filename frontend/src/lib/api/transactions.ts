import { request } from './client'
import type { TransactionDetail, TransactionPage } from '../../types/transactions'

export interface TransactionFilters {
  account_ids?: string[]
  period_start?: string
  period_end?: string
  direction?: 'debit' | 'credit'
  amount_min_cents?: number
  amount_max_cents?: number
  q?: string
  import_file_id?: string
  classification?: 'all' | 'unclassified'
  flow_kind?: string
}

export function getTransactions(filters: TransactionFilters, cursor?: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (Array.isArray(value)) value.forEach((item) => params.append(key, item))
    else if (value !== undefined && value !== '') params.set(key, String(value))
  })
  if (cursor) params.set('cursor', cursor)
  const query = params.toString()
  return request<TransactionPage>(`/transactions${query ? `?${query}` : ''}`, { signal })
}

export function getTransaction(id: string, signal?: AbortSignal) {
  return request<TransactionDetail>(`/transactions/${encodeURIComponent(id)}`, { signal })
}
