import { vi } from 'vitest'

/**
 * Faux backend de revue en mémoire pour les tests de composants : il reproduit le regroupement
 * (contrepartie + statut), l'incrément de version et l'historique, sans toucher au réseau.
 * Toutes les données sont synthétiques.
 */
export const ACCOUNT_ID = '11111111-1111-4111-8111-111111111111'
export const PAYEE_MARKET = '22222222-2222-4222-8222-222222222221'
export const PAYEE_AMAZON = '22222222-2222-4222-8222-222222222222'
export const tid = (n: number) => `33333333-3333-4333-8333-${String(n).padStart(12, '0')}`

export interface FakeTx {
  transaction_id: string
  payee_id: string | null
  booked_on: string
  amount_cents: number
  label: string
  version: number
  allocations: Array<{ category_id: string; flow_kind: string; amount_cents: number; origin: string }>
  history: Array<{ decision_id: string; action: string; actor: string; created_at_utc: string; undone: boolean }>
  suggestions: Array<{ suggestion_id: string; source: string; category_id: string | null; flow_kind: string | null; amount_cents: number | null; status: string; confidence: string | null }>
  ignored?: boolean
}

export interface Call {
  method: string
  path: string
  search: string
  body: unknown
  headers: Record<string, string>
}

export interface FakeApiOptions {
  txs?: FakeTx[]
  failures?: Array<{ match: (call: Call) => boolean; status: number; code: string; message?: string; once?: boolean; details?: Record<string, unknown> }>
  pageSize?: number
  queueDelay?: boolean
}

const CATEGORIES = [
  {
    category_id: 'groceries', parent_category_id: null, label: 'Alimentation et courses', taxonomy_version: 1,
    allowed_flow_kinds: ['expense'], is_active: true,
    children: [
      { category_id: 'groceries_supermarket', parent_category_id: 'groceries', label: 'Supermarché', taxonomy_version: 1, allowed_flow_kinds: ['expense'], is_active: true, children: [] },
      { category_id: 'groceries_bakery', parent_category_id: 'groceries', label: 'Boulangerie', taxonomy_version: 1, allowed_flow_kinds: ['expense'], is_active: true, children: [] },
    ],
  },
  { category_id: 'health', parent_category_id: null, label: 'Santé', taxonomy_version: 1, allowed_flow_kinds: ['expense'], is_active: true, children: [] },
  { category_id: 'income_salary_a', parent_category_id: null, label: 'Salaire A', taxonomy_version: 1, allowed_flow_kinds: ['income_recurring'], is_active: true, children: [] },
]
const DIMENSIONS = {
  beneficiary: ['household', 'adult_a', 'unknown'],
  cadence: ['recurring', 'occasional', 'unknown'],
  flexibility: ['fixed', 'unknown'],
  acquisition: ['new', 'unknown'],
}

export function defaultTransactions(): FakeTx[] {
  const make = (n: number, payee: string | null, day: string, cents: number, label: string, extra: Partial<FakeTx> = {}): FakeTx => ({
    transaction_id: tid(n), payee_id: payee, booked_on: day, amount_cents: cents, label, version: 1,
    allocations: [], history: [], suggestions: [], ...extra,
  })
  return [
    make(1, PAYEE_MARKET, '2026-03-01', -1234, 'Paiement par carte\nX0001 MARCHE SYNTHETIQUE'),
    make(2, PAYEE_MARKET, '2026-03-02', -2000, 'Paiement par carte\nX0002 MARCHE SYNTHETIQUE'),
    make(3, PAYEE_MARKET, '2026-03-03', -999, 'Paiement par carte\nX0003 MARCHE SYNTHETIQUE'),
    make(4, PAYEE_AMAZON, '2026-03-04', -5000, 'Paiement par carte\nX0004 AMAZON COMMANDE SYNTHETIQUE'),
    make(5, PAYEE_AMAZON, '2026-03-05', -7000, 'Paiement par carte\nX0005 AMAZON COMMANDE SYNTHETIQUE'),
    make(6, null, '2026-03-06', -300, 'Retrait au distributeur\nX0006 DAB SYNTHETIQUE'),
  ]
}

function statusOf(tx: FakeTx): string {
  if (tx.allocations.some((item) => item.origin === 'manual')) return 'human'
  if (tx.allocations.length) return 'rule'
  if (tx.ignored) return 'ignored'
  if (tx.suggestions.some((item) => item.status === 'review')) return 'suggested'
  return 'unclassified'
}

const DISPLAY: Record<string, string> = { [PAYEE_MARKET]: 'Marché synthétique', [PAYEE_AMAZON]: 'Amazon' }

function flagsOf(payee: string | null): string[] {
  return payee === PAYEE_AMAZON ? ['ambiguous_merchant'] : []
}

function groupIdOf(tx: FakeTx): string {
  return `g-${tx.payee_id ?? tx.transaction_id}-${statusOf(tx)}`
}

export function installFakeApi(options: FakeApiOptions = {}) {
  const txs = options.txs ?? defaultTransactions()
  const calls: Call[] = []
  const failures = [...(options.failures ?? [])]
  let decisionCounter = 0
  const respond = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  const error = (status: number, code: string, message = 'Erreur synthétique', details: Record<string, unknown> | null = null) =>
    respond({ error: { code, message, details } }, status)

  function groups(search: URLSearchParams) {
    const map = new Map<string, FakeTx[]>()
    for (const tx of txs) {
      const status = statusOf(tx)
      if (search.get('status') && search.get('status') !== status) continue
      if (search.get('direction') === 'credit' && tx.amount_cents < 0) continue
      if (search.get('direction') === 'debit' && tx.amount_cents > 0) continue
      const id = groupIdOf(tx)
      map.set(id, [...(map.get(id) ?? []), tx])
    }
    return [...map.entries()]
      .map(([id, members]) => {
        const residual = members.reduce((sum, tx) => sum + Math.abs(tx.amount_cents - tx.allocations.reduce((s, a) => s + a.amount_cents, 0)), 0)
        const first = members[0]!
        return {
          review_group_id: id,
          payee_id: first.payee_id,
          counterparty: first.payee_id ? DISPLAY[first.payee_id] ?? 'Contrepartie' : null,
          status: statusOf(first),
          quality_flags: flagsOf(first.payee_id),
          transaction_count: members.length,
          amount_cents: members.reduce((sum, tx) => sum + tx.amount_cents, 0),
          impact_cents: residual,
          latest_booked_on: members.map((tx) => tx.booked_on).sort().at(-1)!,
          total_out_of_range: false,
        }
      })
      .sort((left, right) => right.impact_cents - left.impact_cents || left.review_group_id.localeCompare(right.review_group_id))
  }

  function detail(id: string) {
    const members = txs.filter((tx) => groupIdOf(tx) === id)
    if (members.length === 0) return null
    return {
      review_group_id: id,
      status: statusOf(members[0]!),
      transaction_count: members.length,
      transactions: members.map((tx) => {
        const allocated = tx.allocations.reduce((sum, item) => sum + item.amount_cents, 0)
        return {
          transaction_id: tx.transaction_id,
          account_id: ACCOUNT_ID,
          booked_on: tx.booked_on,
          amount_cents: tx.amount_cents,
          residual_cents: tx.amount_cents - allocated,
          total_out_of_range: false,
          version: tx.version,
          status: statusOf(tx),
          allocations: tx.allocations.map((item) => ({
            ...item, beneficiary: 'unknown', cadence: 'unknown', flexibility: 'unknown', acquisition: 'unknown',
          })),
          history: tx.history,
          suggestions: tx.suggestions.map((item) => ({ ...item, total_out_of_range: false })),
          evaluation: {
            version: tx.version,
            human_decision_protected: tx.allocations.some((item) => item.origin === 'manual'),
            conflict_rule_ids: [],
            candidates: tx.suggestions
              .filter((item) => item.status === 'review')
              .map((item, position) => ({
                source: item.source, priority: position + 1, category_id: item.category_id, flow_kind: item.flow_kind,
                amount_cents: item.amount_cents, total_out_of_range: false, confidence: item.confidence,
                rule_id: null, rule_priority: null, suggestion_id: item.suggestion_id,
              })),
          },
        }
      }),
    }
  }

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : undefined
    const headers = Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>))
    const call: Call = { method, path: url.pathname, search: url.search, body, headers }
    calls.push(call)

    const failureIndex = failures.findIndex((item) => item.match(call))
    if (failureIndex >= 0) {
      const failure = failures[failureIndex]!
      if (failure.once !== false) failures.splice(failureIndex, 1)
      return error(failure.status, failure.code, failure.message, failure.details ?? null)
    }

    if (url.pathname === '/api/v1/categories') {
      return respond({ items: CATEGORIES, taxonomy_version: 1, taxonomy_sha256: 'x'.repeat(64), dimensions: DIMENSIONS })
    }
    if (url.pathname === '/api/v1/accounts') {
      return respond({ items: [{ account_id: ACCOUNT_ID, alias: 'Compte synthétique', account_role: 'current', is_active: true, version: 1 }] })
    }
    const labelMatch = /^\/api\/v1\/transactions\/([^/]+)$/.exec(url.pathname)
    if (labelMatch && method === 'GET') {
      const tx = txs.find((item) => item.transaction_id === labelMatch[1])
      return tx ? respond({ transaction_id: tx.transaction_id, raw_label: tx.label }) : error(404, 'NOT_FOUND')
    }
    if (url.pathname === '/api/v1/review/queue' && method === 'GET') {
      const all = groups(url.searchParams)
      const size = options.pageSize ?? 50
      const start = url.searchParams.get('cursor') ? Number(atob(url.searchParams.get('cursor')!)) : 0
      const items = all.slice(start, start + size)
      const next = start + size < all.length ? btoa(String(start + size)) : null
      return respond({ items, page: { limit: size, next_cursor: next, snapshot_at: '2026-10-11T10:00:00Z' } })
    }
    const groupMatch = /^\/api\/v1\/review\/queue\/([^/]+)$/.exec(url.pathname)
    if (groupMatch && method === 'GET') {
      const found = detail(decodeURIComponent(groupMatch[1]!))
      return found ? respond(found) : error(404, 'REVIEW_GROUP_NOT_FOUND')
    }
    if (url.pathname === '/api/v1/review/decisions' && method === 'POST') {
      const request = body as {
        transaction_id: string; expected_version: number; action: string; scope: string
        allocations: Array<{ category_id: string; flow_kind: string; amount_cents: number }>
        selection?: Array<{ transaction_id: string; expected_version: number; allocations: Array<{ category_id: string; flow_kind: string; amount_cents: number }> }>
        rule?: unknown
      }
      if (request.scope === 'future_rule') {
        return respond({
          simulation: {
            scanned_count: 6, matched_count: 3, rows: [], conflict_transaction_ids: [], human_decision_ignored_ids: [],
            already_classified_ids: [], ambiguous_transaction_ids: [], truncated: false, database_changed: false,
            totals_by_category: { groceries_supermarket: { transaction_count: 3, amount_cents: -4233, total_out_of_range: false } },
          },
          rule: { rule_id: '44444444-4444-4444-8444-444444444444', version: 1, status: 'draft', application_scope: 'future_only' },
          applied: false,
        })
      }
      const targets = [
        { transaction_id: request.transaction_id, expected_version: request.expected_version, allocations: request.allocations },
        ...(request.scope === 'selection' ? request.selection ?? [] : []),
      ]
      const staleTargets = targets.filter((item) => txs.find((tx) => tx.transaction_id === item.transaction_id)?.version !== item.expected_version)
      if (staleTargets.length) return error(409, request.scope === 'selection' ? 'REVIEW_SELECTION_CONFLICT' : 'VERSION_CONFLICT')
      if (request.action === 'ignore' || request.action === 'mark_for_review') {
        const tx = txs.find((item) => item.transaction_id === request.transaction_id)!
        tx.ignored = request.action === 'ignore'
        tx.version += 1
        decisionCounter += 1
        tx.history.push({ decision_id: `reject-${decisionCounter}`, action: 'reject', actor: 'utilisateur_local', created_at_utc: '2026-10-11T10:00:00Z', undone: false })
        return respond({ scope: request.scope, items: [{ transaction_id: tx.transaction_id, decision_id: `reject-${decisionCounter}`, version: tx.version, status: statusOf(tx) }], status: request.action })
      }
      const items = targets.map((target) => {
        const tx = txs.find((item) => item.transaction_id === target.transaction_id)!
        tx.allocations = target.allocations.map((part) => ({ ...part, origin: 'manual' }))
        tx.version += 1
        decisionCounter += 1
        const decisionId = `decision-${decisionCounter}`
        tx.history.push({ decision_id: decisionId, action: target.allocations.length > 1 ? 'split' : 'confirm', actor: 'utilisateur_local', created_at_utc: `2026-10-11T10:00:0${decisionCounter % 10}Z`, undone: false })
        return { transaction_id: tx.transaction_id, decision_id: decisionId, version: tx.version, status: 'complete', residual_cents: 0, total_out_of_range: false }
      })
      return respond({ scope: request.scope, items, status: 'confirmed' })
    }
    const undoMatch = /^\/api\/v1\/review\/decisions\/([^/]+)\/undo$/.exec(url.pathname)
    if (undoMatch && method === 'POST') {
      const tx = txs.find((item) => item.history.some((entry) => entry.decision_id === undoMatch[1]))
      if (!tx) return error(404, 'DECISION_NOT_FOUND')
      const entry = tx.history.find((item) => item.decision_id === undoMatch[1])!
      entry.undone = true
      tx.allocations = []
      tx.version += 1
      decisionCounter += 1
      tx.history.push({ decision_id: `undo-${decisionCounter}`, action: 'undo', actor: 'utilisateur_local', created_at_utc: '2026-10-11T10:05:00Z', undone: false })
      return respond({ decision_id: `undo-${decisionCounter}`, undone_decision_id: undoMatch[1], transaction_id: tx.transaction_id, version: tx.version, status: 'unclassified', idempotent: false })
    }
    if (url.pathname === '/api/v1/review/refund-links' && method === 'POST') {
      return respond({ transaction_link_id: '55555555-5555-4555-8555-555555555555', status: 'proposed', version: 1, amount_cents: (body as { amount_cents: number }).amount_cents })
    }
    return error(404, 'NOT_FOUND', `Route de test absente : ${method} ${url.pathname}`)
  })
  return { fetchMock, calls, txs }
}

export const decisionCalls = (calls: Call[]) => calls.filter((call) => call.method === 'POST' && call.path === '/api/v1/review/decisions')
export const undoCalls = (calls: Call[]) => calls.filter((call) => call.method === 'POST' && call.path.endsWith('/undo'))
