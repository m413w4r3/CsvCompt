import { parseIsoDate } from './dates'

export type ViewMode = 'treasury' | 'family_life'
export type PersonalMode = 'envelope' | 'observed'
export type FlowKind =
  | 'expense' | 'income_recurring' | 'income_other' | 'transfer_savings' | 'transfer_owned'
  | 'allocation_personal' | 'loan_disbursement' | 'loan_repayment' | 'capital_event'
  | 'cash_withdrawal_pending' | 'refund' | 'intermediary_pending' | 'reconciliation' | 'unknown'

export interface CommonFilters {
  periodStart?: string
  periodEnd?: string
  accountIds: string[]
  categoryIds: string[]
  flowKinds: FlowKind[]
  status: string[]
  mode: ViewMode
  personalMode: PersonalMode
}

export const DEFAULT_FILTERS: CommonFilters = {
  accountIds: [], categoryIds: [], flowKinds: [], status: [], mode: 'treasury', personalMode: 'envelope',
}

const isoDate = (value: string | null): string | undefined => value && parseIsoDate(value) ? value : undefined
const flowKinds = new Set<FlowKind>([
  'expense', 'income_recurring', 'income_other', 'transfer_savings', 'transfer_owned',
  'allocation_personal', 'loan_disbursement', 'loan_repayment', 'capital_event',
  'cash_withdrawal_pending', 'refund', 'intermediary_pending', 'reconciliation', 'unknown',
])

export function parseFilters(search: string): CommonFilters {
  const params = new URLSearchParams(search)
  const mode = params.get('mode')
  const personal = params.get('personal_mode')
  let periodStart = isoDate(params.get('period_start'))
  let periodEnd = isoDate(params.get('period_end'))
  if (periodStart && periodEnd && periodStart > periodEnd) {
    periodStart = undefined
    periodEnd = undefined
  }
  return {
    periodStart,
    periodEnd,
    accountIds: [...new Set(params.getAll('account_ids').filter(Boolean))].sort(),
    categoryIds: [...new Set(params.getAll('category_ids').filter(Boolean))].sort(),
    flowKinds: [...new Set(params.getAll('flow_kind').filter((value): value is FlowKind => flowKinds.has(value as FlowKind)))].sort(),
    status: [...new Set(params.getAll('status').filter(Boolean))].sort(),
    mode: mode === 'family_life' ? 'family_life' : 'treasury',
    personalMode: personal === 'observed' ? 'observed' : 'envelope',
  }
}

export function serializeFilters(filters: CommonFilters): string {
  const params = new URLSearchParams()
  let periodStart = filters.periodStart && parseIsoDate(filters.periodStart) ? filters.periodStart : undefined
  let periodEnd = filters.periodEnd && parseIsoDate(filters.periodEnd) ? filters.periodEnd : undefined
  if (periodStart && periodEnd && periodStart > periodEnd) {
    periodStart = undefined
    periodEnd = undefined
  }
  if (periodStart) params.set('period_start', periodStart)
  if (periodEnd) params.set('period_end', periodEnd)
  for (const value of [...new Set(filters.accountIds)].sort()) params.append('account_ids', value)
  for (const value of [...new Set(filters.categoryIds)].sort()) params.append('category_ids', value)
  for (const value of [...new Set(filters.flowKinds)].filter((flow): flow is FlowKind => flowKinds.has(flow)).sort()) params.append('flow_kind', value)
  for (const value of [...new Set(filters.status)].sort()) params.append('status', value)
  if (filters.mode === 'family_life') params.set('mode', filters.mode)
  if (filters.personalMode === 'observed') params.set('personal_mode', filters.personalMode)
  return params.toString()
}
