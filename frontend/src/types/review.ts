export type ReviewStatus =
  | 'unclassified'
  | 'suggested'
  | 'rule'
  | 'memory'
  | 'human'
  | 'ignored'
  | 'review'

export type ReviewScope = 'this_transaction' | 'selection' | 'future_rule'
export type ReviewAction = 'confirm' | 'classify' | 'ignore' | 'mark_for_review'
export type SuggestionSource = 'manual' | 'rule' | 'memory' | 'heuristic' | 'llm' | string

export interface ReviewQueueItem {
  review_group_id: string
  payee_id: string | null
  counterparty: string | null
  status: ReviewStatus
  quality_flags: string[]
  transaction_count: number
  amount_cents: number | null
  impact_cents: number | null
  latest_booked_on: string
  total_out_of_range: boolean
}

export interface ReviewQueuePage {
  items: ReviewQueueItem[]
  page: { limit: number; next_cursor: string | null; snapshot_at: string }
}

export interface ReviewAllocation {
  flow_kind: string
  category_id: string
  amount_cents: number | null
  origin: SuggestionSource
  beneficiary: string
  cadence: string
  flexibility: string
  acquisition: string
}

export interface ReviewHistoryItem {
  decision_id: string
  action: string
  actor: string
  created_at_utc: string
  undone: boolean
}

export interface ReviewSuggestion {
  suggestion_id: string
  source: SuggestionSource
  category_id: string | null
  flow_kind: string | null
  amount_cents: number | null
  total_out_of_range: boolean
  confidence: string | null
  status: string
}

export interface ReviewCandidate {
  source: SuggestionSource
  priority: number
  category_id: string | null
  flow_kind: string | null
  amount_cents: number | null
  total_out_of_range: boolean
  confidence: string | null
  rule_id: string | null
  rule_priority: number | null
  suggestion_id: string | null
}

export interface ReviewTransaction {
  transaction_id: string
  account_id: string
  booked_on: string
  amount_cents: number | null
  residual_cents: number | null
  total_out_of_range: boolean
  version: number
  status: ReviewStatus
  allocations: ReviewAllocation[]
  history: ReviewHistoryItem[]
  suggestions: ReviewSuggestion[]
  evaluation: {
    version: number
    human_decision_protected: boolean
    conflict_rule_ids: string[]
    candidates: ReviewCandidate[]
  }
}

export interface ReviewGroup {
  review_group_id: string
  status: ReviewStatus
  transaction_count: number
  transactions: ReviewTransaction[]
}

export interface ReviewAllocationInput {
  flow_kind: string
  category_id: string
  amount_cents: number
  beneficiary: string
  cadence: string
  flexibility: string
  acquisition: string
}

export interface ReviewSelectionItem {
  transaction_id: string
  expected_version: number
  allocations: ReviewAllocationInput[]
}

export interface ReviewDecisionRequest {
  transaction_id: string
  expected_version: number
  actor: string
  action: ReviewAction
  scope: ReviewScope
  selection?: ReviewSelectionItem[]
  allocations?: ReviewAllocationInput[]
  suggestion_id?: string
  rule?: FutureRuleDraft
}

export interface ReviewDecisionResponse {
  scope: string
  items: Array<{
    transaction_id: string
    decision_id: string | null
    version: number
    status: string
    residual_cents?: number | null
    total_out_of_range?: boolean
  }>
  status: string
}

export interface RuleSimulation {
  scanned_count: number
  matched_count: number
  rows: Array<{
    transaction_id: string
    transaction_version: number
    booked_on: string
    amount_cents: number | null
    total_out_of_range: boolean
    after: Array<{ category_id?: string; amount_cents?: number | null }>
    ambiguous: boolean
  }>
  totals_by_category: Record<string, { transaction_count: number; amount_cents: number | null; total_out_of_range: boolean }>
  conflict_transaction_ids: string[]
  human_decision_ignored_ids: string[]
  already_classified_ids: string[]
  ambiguous_transaction_ids: string[]
  truncated: boolean
  database_changed: false
}

export interface FutureRuleDraft {
  name: string
  condition: {
    payee_id?: string
    direction?: 'debit' | 'credit'
    account_ids?: string[]
    min_amount_cents?: number
    max_amount_cents?: number
    flow_kind?: string
  }
  action: {
    flow_kind: string
    category_id: string
    beneficiary: string
    cadence: string
    flexibility: string
    acquisition: string
  }
  priority: number
}

export interface FutureRuleResponse {
  simulation: RuleSimulation
  rule: { rule_id: string; version: number; status: string; application_scope: 'future_only' }
  applied: false
}

export interface UndoResponse {
  decision_id: string
  undone_decision_id: string
  transaction_id: string
  version: number
  status: string
  idempotent: boolean
}

export interface RefundLinkResponse {
  transaction_link_id: string
  status: 'proposed' | 'confirmed' | 'rejected' | string
  version: number
  amount_cents: number | null
  total_out_of_range?: boolean
}

export interface CategoryNode {
  category_id: string
  parent_category_id: string | null
  label: string
  taxonomy_version: number
  allowed_flow_kinds: string[]
  is_active: boolean
  children: CategoryNode[]
}

export interface ReviewCategories {
  items: CategoryNode[]
  taxonomy_version: number
  taxonomy_sha256: string
  dimensions: Record<string, string[]>
}

export interface ReviewAccount {
  account_id: string
  alias: string
  account_role: string
  is_active: boolean
  version: number
}
