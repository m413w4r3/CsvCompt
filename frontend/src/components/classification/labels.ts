import { formatCentsToEuro } from '../../lib/money'
import type { CategoryNode, ReviewStatus } from '../../types/review'

const FLOW_LABELS: Record<string, string> = {
  expense: 'Dépense',
  income_recurring: 'Revenu régulier',
  income_other: 'Autre revenu',
  transfer_savings: 'Épargne',
  transfer_owned: 'Virement entre comptes propres',
  allocation_personal: 'Argent personnel',
  loan_disbursement: 'Prêt reçu',
  loan_repayment: 'Remboursement de prêt',
  capital_event: 'Mouvement de capital',
  cash_withdrawal_pending: 'Espèces à ventiler',
  refund: 'Remboursement reçu',
  intermediary_pending: 'Intermédiaire à résoudre',
  reconciliation: 'Régularisation',
  unknown: 'À préciser',
}

export function flowLabel(flow: string | null | undefined): string {
  if (!flow) return 'flux à préciser'
  return FLOW_LABELS[flow] ?? flow
}

export const DIMENSION_FIELDS = [
  ['beneficiary', 'Bénéficiaire'],
  ['cadence', 'Cadence'],
  ['flexibility', 'Flexibilité'],
  ['acquisition', 'Acquisition'],
] as const
export type DimensionField = (typeof DIMENSION_FIELDS)[number][0]

const DIMENSION_LABELS: Record<string, string> = {
  unknown: 'À préciser',
  household: 'Foyer',
  adult_a: 'Adulte A',
  adult_b: 'Adulte B',
  child_a: 'Enfant A',
  child_b: 'Enfant B',
  recurring: 'Récurrent',
  occasional: 'Occasionnel',
  exceptional: 'Exceptionnel',
  fixed: 'Fixe',
  variable_essential: 'Variable essentiel',
  variable_optional: 'Variable facultatif',
  new: 'Neuf',
  second_hand: 'Occasion',
  not_applicable: 'Sans objet',
}

export function dimensionLabel(value: string): string {
  return DIMENSION_LABELS[value] ?? value
}

const ORIGIN_LABELS: Record<string, string> = {
  manual: 'Humain',
  human: 'Humain',
  rule: 'Règle',
  memory: 'Mémoire',
  history: 'Mémoire',
  heuristic: 'Heuristique',
  llm: 'Suggestion IA',
  unclassified: 'Heuristique',
}

/** Provenance d'une allocation, suggestion ou candidat : toujours nommée, jamais masquée. */
export function originLabel(origin: string | null | undefined): string {
  if (!origin) return 'Origine inconnue'
  return ORIGIN_LABELS[origin] ?? origin
}

const STATUS_LABELS: Record<ReviewStatus, string> = {
  unclassified: 'Non ventilée',
  suggested: 'Suggestion à vérifier',
  rule: 'Règle',
  memory: 'Mémoire',
  human: 'Décision humaine',
  ignored: 'Ignorée',
  review: 'À revoir',
}

export function statusLabel(status: ReviewStatus): string {
  return STATUS_LABELS[status] ?? status
}

const QUALITY_FLAG_LABELS: Record<string, string> = {
  ambiguous_merchant: 'Contrepartie ambiguë',
  intermediary_to_review: 'Intermédiaire à vérifier',
  transfer_to_review: 'Virement à vérifier',
  loan_to_review: 'Prêt à vérifier',
  cash_to_review: 'Espèces à vérifier',
}

export function qualityFlagLabel(flag: string): string {
  return QUALITY_FLAG_LABELS[flag] ?? flag
}

export interface CategoryChoice {
  category_id: string
  label: string
  allowed_flow_kinds: string[]
  children: CategoryChoice[]
}

export interface CategoryIndex {
  roots: CategoryChoice[]
  byId: Map<string, CategoryChoice & { parent_id: string | null; path: string }>
}

/** Arbre de taxonomie réduit aux catégories actives, indexé par identifiant. */
export function buildCategoryIndex(items: CategoryNode[]): CategoryIndex {
  const byId: CategoryIndex['byId'] = new Map()
  const convert = (node: CategoryNode, parentId: string | null, parentPath: string): CategoryChoice | null => {
    if (!node.is_active) return null
    const path = parentPath ? `${parentPath} › ${node.label}` : node.label
    const children = (node.children ?? [])
      .map((child) => convert(child, node.category_id, path))
      .filter((child): child is CategoryChoice => child !== null)
    const choice: CategoryChoice = {
      category_id: node.category_id,
      label: node.label,
      allowed_flow_kinds: [...node.allowed_flow_kinds],
      children,
    }
    byId.set(node.category_id, { ...choice, parent_id: parentId, path })
    return choice
  }
  const roots = items
    .map((node) => convert(node, null, ''))
    .filter((node): node is CategoryChoice => node !== null)
  return { roots, byId }
}

export function categoryLabel(index: CategoryIndex | null, categoryId: string | null | undefined): string {
  if (!categoryId) return 'Sans catégorie'
  return index?.byId.get(categoryId)?.path ?? categoryId
}

/** Montant signé en centimes (nombre sûr) → texte FR, ou message explicite hors plage. */
export function formatSafeCents(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isSafeInteger(value)) return 'hors plage entière sûre'
  return formatCentsToEuro(value)
}

/** Montant absolu prérempli dans un champ de saisie (sans symbole monétaire). */
export function formatEuroInput(cents: bigint): string {
  return formatCentsToEuro(cents < 0n ? -cents : cents).replace(/\s*€$/, '')
}

export function toSafeNumber(value: bigint): number | null {
  const number = Number(value)
  return Number.isSafeInteger(number) && BigInt(number) === value ? number : null
}
