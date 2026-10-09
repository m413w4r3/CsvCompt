export type ViewMode = 'treasury' | 'family_life'
export type PersonalMode = 'envelope' | 'observed'

export interface CommonFilters {
  periodStart?: string
  periodEnd?: string
  accountIds: string[]
  categoryIds: string[]
  flowKinds: string[]
  status: string[]
  mode: ViewMode
  personalMode: PersonalMode
}

export const DEFAULT_FILTERS: CommonFilters = {
  accountIds: [], categoryIds: [], flowKinds: [], status: [], mode: 'treasury', personalMode: 'envelope',
}

const isoDate = (value: string | null): string | undefined => {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined
  const date = new Date(`${value.slice(0, 4)}-${value.slice(5, 7)}-${value.slice(8, 10)}T00:00:00Z`)
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value ? value : undefined
}

export function parseFilters(search: string): CommonFilters {
  const params = new URLSearchParams(search)
  const mode = params.get('mode')
  const personal = params.get('personal_mode')
  return {
    periodStart: isoDate(params.get('period_start')),
    periodEnd: isoDate(params.get('period_end')),
    accountIds: [...new Set(params.getAll('account_ids').filter(Boolean))].sort(),
    categoryIds: [...new Set(params.getAll('category_ids').filter(Boolean))].sort(),
    flowKinds: [...new Set(params.getAll('flow_kind').filter(Boolean))].sort(),
    status: [...new Set(params.getAll('status').filter(Boolean))].sort(),
    mode: mode === 'family_life' ? 'family_life' : 'treasury',
    personalMode: personal === 'observed' ? 'observed' : 'envelope',
  }
}

export function serializeFilters(filters: CommonFilters): string {
  const params = new URLSearchParams()
  if (filters.periodStart) params.set('period_start', filters.periodStart)
  if (filters.periodEnd) params.set('period_end', filters.periodEnd)
  for (const value of [...new Set(filters.accountIds)].sort()) params.append('account_ids', value)
  for (const value of [...new Set(filters.categoryIds)].sort()) params.append('category_ids', value)
  for (const value of [...new Set(filters.flowKinds)].sort()) params.append('flow_kind', value)
  for (const value of [...new Set(filters.status)].sort()) params.append('status', value)
  if (filters.mode !== 'treasury') params.set('mode', filters.mode)
  if (filters.personalMode !== 'envelope') params.set('personal_mode', filters.personalMode)
  return params.toString()
}
