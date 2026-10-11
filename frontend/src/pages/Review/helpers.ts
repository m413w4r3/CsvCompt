import type { ReviewCandidate, ReviewHistoryItem, ReviewQueueItem, ReviewSuggestion, ReviewTransaction } from '../../types/review'

export type SortMode = 'impact' | 'recent'

export function formatDate(value: string): string {
  const date = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeZone: 'UTC' }).format(date)
}

export function formatDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'short' }).format(date)
}

function compareImpact(left: number | null, right: number | null): number {
  // Un agrégat hors plage sûre (null) est tenu pour le plus lourd.
  if (left === null || right === null) return Number(right === null) - Number(left === null)
  return right - left
}

export function sortGroups(items: ReviewQueueItem[], mode: SortMode): ReviewQueueItem[] {
  return [...items].sort((left, right) => {
    const byImpact = compareImpact(left.impact_cents, right.impact_cents)
    const byDate = right.latest_booked_on.localeCompare(left.latest_booked_on)
    if (mode === 'recent') return byDate || byImpact || left.review_group_id.localeCompare(right.review_group_id)
    return byImpact || byDate || left.review_group_id.localeCompare(right.review_group_id)
  })
}

export function mergeById(current: ReviewQueueItem[], additions: ReviewQueueItem[]): ReviewQueueItem[] {
  const merged = new Map(current.map((item) => [item.review_group_id, item]))
  additions.forEach((item) => merged.set(item.review_group_id, item))
  return [...merged.values()]
}

const NON_TEXT_INPUTS = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file', 'image'])

/** Vrai si la frappe appartient à une saisie : les raccourcis clavier sont alors suspendus. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  if (target instanceof HTMLInputElement) return !NON_TEXT_INPUTS.has(target.type)
  if (['TEXTAREA', 'SELECT'].includes(target.tagName)) return true
  const role = target.getAttribute('role')
  return role === 'textbox' || role === 'combobox' || role === 'spinbutton'
}

/** Vrai si la touche Entrée y a déjà un sens natif (bouton, lien, résumé de <details>, case, radio). */
export function isActivatableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target instanceof HTMLInputElement) return NON_TEXT_INPUTS.has(target.type)
  return ['BUTTON', 'A', 'SUMMARY'].includes(target.tagName)
}

const UNDOABLE_ACTIONS = new Set(['confirm', 'edit', 'split', 'clear'])

/**
 * Seule la dernière décision peut être annulée : une annulation en chaîne d'une décision plus
 * ancienne échoue côté serveur (UNDO_STATE_DIVERGED). Les marqueurs « ignorée / à revoir » se
 * corrigent en classant l'opération.
 */
export function latestUndoableDecision(history: ReviewHistoryItem[]): ReviewHistoryItem | null {
  const last = history[history.length - 1]
  if (!last || last.undone || !UNDOABLE_ACTIONS.has(last.action)) return null
  return last
}

export interface Proposal {
  key: string
  source: string
  category_id: string | null
  flow_kind: string | null
  amount_cents: number | null
  total_out_of_range: boolean
  confidence: string | null
  suggestion_id: string | null
  rule_id: string | null
  rank: number | null
}

/** Propositions à confirmer : candidats du moteur (hors décision humaine) + suggestions en revue restantes. */
export function proposalsFor(transaction: ReviewTransaction): Proposal[] {
  const fromEngine = transaction.evaluation.candidates
    .filter((candidate: ReviewCandidate) => candidate.source !== 'human')
    .map((candidate, position) => ({
      key: `c-${position}-${candidate.rule_id ?? candidate.suggestion_id ?? candidate.source}`,
      source: candidate.source,
      category_id: candidate.category_id,
      flow_kind: candidate.flow_kind,
      amount_cents: candidate.amount_cents,
      total_out_of_range: candidate.total_out_of_range,
      confidence: candidate.confidence,
      suggestion_id: candidate.suggestion_id,
      rule_id: candidate.rule_id,
      rank: candidate.priority,
    }))
  const known = new Set(fromEngine.map((item) => item.suggestion_id).filter(Boolean))
  const fromSuggestions = transaction.suggestions
    .filter((suggestion: ReviewSuggestion) => suggestion.status === 'review' && !known.has(suggestion.suggestion_id))
    .map((suggestion) => ({
      key: `s-${suggestion.suggestion_id}`,
      source: suggestion.source,
      category_id: suggestion.category_id,
      flow_kind: suggestion.flow_kind,
      amount_cents: suggestion.amount_cents,
      total_out_of_range: suggestion.total_out_of_range,
      confidence: suggestion.confidence,
      suggestion_id: suggestion.suggestion_id,
      rule_id: null,
      rank: null,
    }))
  return [...fromEngine, ...fromSuggestions]
}

export function isActionable(proposal: Proposal): boolean {
  return Boolean(
    proposal.category_id &&
      proposal.flow_kind &&
      proposal.amount_cents !== null &&
      Number.isSafeInteger(proposal.amount_cents) &&
      !proposal.total_out_of_range,
  )
}

const ACTION_LABELS: Record<string, string> = {
  confirm: 'confirmation',
  edit: 'correction',
  split: 'séparation',
  reject: 'ignorée ou marquée à revoir',
  clear: 'ventilation retirée',
  undo: 'annulation',
  rule: 'règle appliquée',
}

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}
