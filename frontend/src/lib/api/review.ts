import { ApiError, request } from './client'
import { getTransaction } from './transactions'
import type {
  CategoryNode,
  FutureRuleResponse,
  RefundLinkResponse,
  ReviewAccount,
  ReviewCategories,
  ReviewDecisionRequest,
  ReviewDecisionResponse,
  ReviewGroup,
  ReviewQueuePage,
  ReviewStatus,
  UndoResponse,
} from '../../types/review'

/** Application locale mono-utilisateur : l'acteur journalisé est constant. */
export const REVIEW_ACTOR = 'utilisateur_local'

/**
 * L'API T21 ne propose aucun endpoint d'approbation, d'application ni de suppression de brouillon
 * de règle (voir docs/MANUEL_REVUE.md, « Manques d'API »). Tant que ce drapeau est faux, l'interface
 * explique la limite au lieu de simuler une action qui n'existe pas.
 */
export const RULE_APPROVAL_API_AVAILABLE = false

export interface ReviewQueueFilters {
  period_start?: string
  period_end?: string
  account_id?: string
  direction?: 'debit' | 'credit'
  status?: ReviewStatus
}

export function getReviewQueue(filters: ReviewQueueFilters, cursor?: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value)
  }
  if (cursor) params.set('cursor', cursor)
  const query = params.toString()
  return request<ReviewQueuePage>(`/review/queue${query ? `?${query}` : ''}`, { signal })
}

export function getReviewGroup(groupId: string, signal?: AbortSignal) {
  return request<ReviewGroup>(`/review/queue/${encodeURIComponent(groupId)}`, { signal })
}

export function getReviewCategories(signal?: AbortSignal) {
  return request<ReviewCategories>('/categories', { signal })
}

export async function getReviewAccounts(signal?: AbortSignal) {
  const response = await request<{ items: ReviewAccount[] }>('/accounts', { signal })
  return response.items
}

/** Le détail de revue n'expose pas le libellé : il est lu à la demande via le registre des opérations. */
export async function getReviewTransactionLabel(transactionId: string, signal?: AbortSignal) {
  const detail = await getTransaction(transactionId, signal)
  return detail.raw_label
}

function jsonHeaders(idempotencyKey: string) {
  return { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }
}

export function newIdempotencyKey() {
  return crypto.randomUUID()
}

export function createReviewDecision(body: ReviewDecisionRequest, idempotencyKey: string = newIdempotencyKey()) {
  return request<ReviewDecisionResponse | FutureRuleResponse>('/review/decisions', {
    method: 'POST',
    headers: jsonHeaders(idempotencyKey),
    body: JSON.stringify(body),
  })
}

export function undoReviewDecision(
  decisionId: string,
  body: { expected_version: number; actor: string; confirm_human_override?: boolean },
  idempotencyKey: string = newIdempotencyKey(),
) {
  return request<UndoResponse>(`/review/decisions/${encodeURIComponent(decisionId)}/undo`, {
    method: 'POST',
    headers: jsonHeaders(idempotencyKey),
    body: JSON.stringify(body),
  })
}

export function createReviewRefundLink(
  body: {
    source_transaction_id: string
    target_transaction_id: string
    amount_cents: number
    actor: string
    expected_source_version: number
    expected_target_version: number
  },
  idempotencyKey: string = newIdempotencyKey(),
) {
  return request<RefundLinkResponse>('/review/refund-links', {
    method: 'POST',
    headers: jsonHeaders(idempotencyKey),
    body: JSON.stringify(body),
  })
}

export function confirmReviewRefundLink(
  linkId: string,
  body: { expected_version: number; actor: string },
  idempotencyKey: string = newIdempotencyKey(),
) {
  return request<RefundLinkResponse>(`/review/refund-links/${encodeURIComponent(linkId)}/confirm`, {
    method: 'POST',
    headers: jsonHeaders(idempotencyKey),
    body: JSON.stringify(body),
  })
}

export function flattenCategories(items: CategoryNode[]): Array<CategoryNode & { display_label: string }> {
  const result: Array<CategoryNode & { display_label: string }> = []
  const visit = (nodes: CategoryNode[], parents: string[]) => {
    for (const node of nodes) {
      const path = [...parents, node.label]
      if (node.is_active) result.push({ ...node, display_label: path.join(' › ') })
      visit(node.children ?? [], path)
    }
  }
  visit(items, [])
  return result
}

const CODE_MESSAGES: Record<string, string> = {
  CURSOR_STALE: 'La file a changé ou son curseur a expiré. Rechargez la file pour continuer.',
  CURSOR_INVALID: 'Le curseur de la file est invalide. Rechargez la file pour continuer.',
  REVIEW_SELECTION_CONFLICT: 'Une ou plusieurs opérations ont changé. La sélection n’a pas été appliquée ; rechargez le détail.',
  VERSION_CONFLICT: 'Cette opération a été modifiée depuis son chargement. Le détail a été rechargé ; vérifiez-le avant de recommencer.',
  IDEMPOTENCY_CONFLICT: 'Cette requête a déjà été envoyée avec un contenu différent. Rouvrez la fenêtre et recommencez.',
  UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED: 'Une décision humaine plus récente existe. Confirmez explicitement son remplacement pour annuler.',
  UNDO_REFUND_LINK_ACTIVE: 'Cette opération est liée à un remboursement actif et ne peut pas être annulée.',
  UNDO_STATE_DIVERGED: 'L’état de l’opération a changé depuis cette décision : elle ne peut plus être annulée telle quelle. Corrigez l’opération par une nouvelle décision.',
  RULE_BATCH_UNDO_REQUIRED: 'Cette décision vient de l’application d’une règle : elle s’annule avec son lot, ce que l’API de détail ne permet pas encore.',
  HUMAN_DECISION_PROTECTED: 'Une décision humaine protège cette opération. Ouvrez son détail et corrigez-la explicitement.',
  ALLOCATION_TOTAL_MISMATCH: 'La somme des parts doit être exactement égale au montant de l’opération.',
  ALLOCATION_TOTAL_EXCEEDED: 'La somme des parts dépasse le montant de l’opération.',
  ALLOCATION_CATEGORY_FLOW_INVALID: 'Ce flux n’est pas compatible avec la catégorie choisie.',
  ALLOCATION_SIGN_INVALID: 'Le signe de la part ne correspond pas au sens de l’opération.',
  AMBIGUOUS_MARKETPLACE_GLOBAL_SCOPE: 'Cette contrepartie est ambiguë : aucune règle globale n’est autorisée.',
  REFUND_LINK_ALREADY_EXISTS: 'Un lien actif existe déjà pour ce couple d’opérations.',
  REFUND_LINK_INVALID: 'Ce lien de remboursement est invalide. Vérifiez les opérations et le montant.',
  REVIEW_GROUP_NOT_FOUND: 'Ce groupe n’existe plus (il a probablement changé de statut). Rechargez la file.',
  TRANSACTION_NOT_FOUND: 'Cette opération n’existe plus. Rechargez la file.',
  DECISION_NOT_FOUND: 'Cette décision n’existe plus. Rechargez le détail.',
  ORIGIN_NOT_ALLOWED: 'Cette page n’est pas servie depuis une origine locale autorisée ; les écritures sont refusées.',
}

export function reviewErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Une erreur inattendue est survenue. Réessayez.'
  const known = CODE_MESSAGES[error.code]
  if (known) return known
  if (error.status === 0) return error.message
  if (error.status === 409) return 'Cette opération a été modifiée depuis son chargement. Le détail a été rechargé ; vérifiez-le avant de recommencer.'
  if (error.status === 422) return 'La demande est incomplète ou incohérente. Vérifiez les catégories, montants et champs obligatoires.'
  if (error.status === 503) return 'La base locale est occupée ou temporairement indisponible. Réessayez dans un instant.'
  if (error.status === 410) return 'Cette ressource a expiré. Rechargez la file avant de continuer.'
  if (error.status === 404) return 'Cette ressource n’existe plus. Rechargez la file.'
  return error.message || 'La requête de revue a échoué.'
}

/** Vrai pour les erreurs qui signalent un état serveur plus récent (détail à recharger). */
export function isStaleStateError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 409 || error.status === 404)
}
