export interface TransactionItem {
  id: string
  account_id: string
  account_alias: string
  booked_on: string
  amount_cents: number | null
  raw_label_preview: string
  occurrence_rank: number
  import_count: number
  classification_state: 'unclassified' | 'classified'
  residual_cents: number | null
  total_out_of_range?: boolean
}

export interface TransactionPage {
  items: TransactionItem[]
  next_cursor: string | null
  total_count: number
  summary: {
    count: number
    debit_total_cents: number | null
    credit_total_cents: number | null
    net_cents: number | null
    total_out_of_range?: boolean
  }
  snapshot_at: string
}

export interface TransactionDetail extends TransactionItem {
  raw_label: string
  identity_label: string
  sources: Array<{ import_file_id: string; imported_at: string; section: number; logical_row_idx: number }>
  allocations: Array<{ allocation_id: string; category_id: string; flow_kind: string; amount_cents: number | null; total_out_of_range?: boolean }>
}

export interface ImportSection {
  section_index: number
  account_mapping: { status: 'known' | 'unknown' | 'ambiguous'; account_id: string | null; alias: string | null }
  account_resolution: 'known' | 'unknown' | 'ambiguous'
  account_id: string | null
  account_alias: string | null
  period_from: string | null
  period_to: string | null
  coverage_from: string | null
  coverage_through: string | null
  complete_through: string | null
  operation_count: number
  debit_total_cents: number | null
  credit_total_cents: number | null
  net_cents: number | null
  total_out_of_range: boolean
  provisional_days: string[]
}

export interface ImportLinkCandidate {
  transaction_id: string
  occurrence_rank: number
  imported_at_utc: string
}

export interface OutOfPeriodCandidateGroup {
  section_index: number
  logical_row_idx: number
  candidates: ImportLinkCandidate[]
}

export interface ImportPreview {
  preview_id: string
  version: number
  generated_on: string
  operation_count: number
  anomaly_counts: Record<string, number>
  already_imported: boolean
  import_file_id: string | null
  sections: ImportSection[]
  warnings: Array<{ code: string; message: string; severity: string; section: number | null }>
  rejections: Array<{ code: string; message: string; severity: string; section: number | null; logical_row_idx: number | null }>
  out_of_period_candidates: OutOfPeriodCandidateGroup[]
  can_commit: boolean
  expires_at_utc: string
}

export type PeriodCompleteness = 'complete' | 'partial' | 'unknown' | 'not_started'

export interface ImportListItem {
  import_file_id: string
  file_sha256: string
  status: 'previewed' | 'committed' | 'rejected' | 'no_op'
  created_at_utc: string
  operation_count: number
  created_transaction_count: number
  source_row_count: number
}
