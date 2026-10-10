import { request } from './client'
import type { ImportListItem, ImportPreview } from '../../types/transactions'

export function createImportPreview(file: File) {
  const form = new FormData()
  form.set('file', file)
  return request<ImportPreview>('/imports/preview', { method: 'POST', body: form })
}

export function getImportPreview(id: string) {
  return request<ImportPreview>(`/imports/preview/${encodeURIComponent(id)}`)
}

export function cancelImportPreview(id: string) {
  return request<void>(`/imports/preview/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function updateAccountMapping(id: string, section: number, body: Record<string, unknown>) {
  return request<ImportPreview>(`/imports/${encodeURIComponent(id)}/sections/${section}/account-mapping`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}

export function updateCoverage(id: string, section: number, body: Record<string, unknown>) {
  return request<ImportPreview>(`/imports/${encodeURIComponent(id)}/sections/${section}/coverage`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}

export function resolveMultiplicity(id: string, logicalRow: number, body: Record<string, unknown>) {
  return request<ImportPreview>(`/imports/${encodeURIComponent(id)}/rows/${logicalRow}/multiplicity`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}

export function commitImport(id: string, expectedVersion: number, idempotencyKey: string) {
  return request<Record<string, unknown>>(`/imports/${encodeURIComponent(id)}/commit`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) })
}

export function listImports(cursor?: string | null) {
  const params = new URLSearchParams()
  if (cursor) params.set('cursor', cursor)
  return request<{ items: ImportListItem[]; page: { next_cursor: string | null } }>(`/imports${params.size ? `?${params}` : ''}`)
}

export function listImportRejections(id: string) {
  return request<{ items: Array<{ code: string; message: string; logical_row_idx: number | null }> }>(`/imports/${encodeURIComponent(id)}/rejections`)
}
