import { Badge, Button } from '../../components/ui'
import {
  categoryLabel,
  flowLabel,
  formatSafeCents,
  originLabel,
  statusLabel,
  type CategoryIndex,
} from '../../components/classification/labels'
import type { ReviewAccount, ReviewTransaction } from '../../types/review'
import {
  actionLabel,
  formatDate,
  formatDateTime,
  isActionable,
  latestUndoableDecision,
  proposalsFor,
  type Proposal,
} from './helpers'
import { useBankLabel } from './useBankLabel'

interface Props {
  transaction: ReviewTransaction
  position: number
  selected: boolean
  active: boolean
  accounts: ReviewAccount[]
  categoryIndex: CategoryIndex | null
  busy: boolean
  onSelect: (shiftKey: boolean) => void
  onActivate: () => void
  onConfirm: (proposal: Proposal) => void
  onClassify: () => void
  onSplit: () => void
  onIgnore: () => void
  onReview: () => void
  onRefund: () => void
  onUndo: () => void
}

export function TransactionCard(props: Props) {
  const { transaction: item, categoryIndex } = props
  const label = useBankLabel(item.transaction_id)
  const proposals = proposalsFor(item)
  const actionable = proposals.filter(isActionable)
  const hasAllocations = item.allocations.length > 0
  const amountKnown = item.amount_cents !== null && !item.total_out_of_range
  const lastDecision = latestUndoableDecision(item.history)
  const fromRule = item.allocations.some((allocation) => allocation.origin === 'rule')
  const canUndo = Boolean(lastDecision) && !fromRule
  const accountAlias = props.accounts.find((account) => account.account_id === item.account_id)?.alias ?? 'Compte'
  const context = `opération du ${formatDate(item.booked_on)}, ${formatSafeCents(item.amount_cents)}`
  const residual = item.residual_cents

  return (
    <article
      className={`card review-transaction${props.active ? ' is-active' : ''}`}
      aria-label={`Opération ${props.position + 1} : ${context}`}
      data-transaction-id={item.transaction_id}
      onFocusCapture={props.onActivate}
    >
      <div className="review-transaction-heading">
        <label className="review-select-transaction">
          <input
            type="checkbox"
            checked={props.selected}
            aria-label={`Sélectionner l’${context}`}
            onChange={(event) => props.onSelect(Boolean((event.nativeEvent as MouseEvent).shiftKey))}
          />
          <span>Opération {props.position + 1}</span>
        </label>
        <Badge>{statusLabel(item.status)}</Badge>
      </div>

      <p className="review-bank-label" data-testid="bank-label">
        {label.status === 'ready' ? label.text : label.status === 'loading' ? 'Libellé en cours de chargement…' : 'Libellé indisponible'}
      </p>
      <div className="review-transaction-summary">
        <time dateTime={item.booked_on}>{formatDate(item.booked_on)}</time>
        <strong>{formatSafeCents(item.amount_cents)}</strong>
        <span>{accountAlias}</span>
      </div>
      {item.total_out_of_range && (
        <p className="review-warning" role="note">
          Un montant dépasse la plage entière sûre : les actions qui en dépendent sont bloquées.
        </p>
      )}

      {hasAllocations && (
        <div className="review-current-allocations">
          <h3>Ventilation actuelle</h3>
          <ul>
            {item.allocations.map((allocation, position) => (
              <li key={`${allocation.category_id}-${position}`}>
                {categoryLabel(categoryIndex, allocation.category_id)} · {formatSafeCents(allocation.amount_cents)} ·{' '}
                {flowLabel(allocation.flow_kind)} · <span className="review-origin">{originLabel(allocation.origin)}</span>
              </li>
            ))}
          </ul>
          {residual !== null && residual !== 0 && (
            <p className="review-warning" role="note">Reste non ventilé : {formatSafeCents(residual)}.</p>
          )}
        </div>
      )}

      {item.evaluation.human_decision_protected && (
        <p className="muted" role="note">Décision humaine protégée : aucune règle ni suggestion ne la remplace.</p>
      )}
      {item.evaluation.conflict_rule_ids.length > 0 && (
        <p className="review-warning" role="note">
          Conflit entre {item.evaluation.conflict_rule_ids.length} règles : aucune proposition automatique n’est retenue.
        </p>
      )}

      {proposals.length > 0 && (
        <section className="review-suggestions" aria-label="Propositions à vérifier">
          <h3>Propositions à vérifier</h3>
          <ul>
            {proposals.map((proposal) => (
              <li key={proposal.key} className="review-suggestion">
                <span>
                  {categoryLabel(categoryIndex, proposal.category_id)} · {flowLabel(proposal.flow_kind)} ·{' '}
                  {formatSafeCents(proposal.amount_cents)} · provenance <strong className="review-origin">{originLabel(proposal.source)}</strong>
                  {proposal.confidence ? ` · confiance ${proposal.confidence}` : ''}
                </span>
                <Button
                  onClick={() => props.onConfirm(proposal)}
                  disabled={props.busy || !isActionable(proposal)}
                  aria-label={`Confirmer la proposition ${originLabel(proposal.source)} — ${context}`}
                >
                  Confirmer
                </Button>
              </li>
            ))}
          </ul>
          {actionable.length === 0 && <p className="muted">Aucune proposition complète : classez l’opération à la main.</p>}
        </section>
      )}

      <div className="review-actions">
        <Button variant="primary" onClick={props.onClassify} disabled={props.busy || !amountKnown} aria-label={`${hasAllocations ? 'Corriger' : 'Classer'} — ${context}`}>
          {hasAllocations ? 'Corriger' : 'Classer'}
        </Button>
        <Button onClick={props.onSplit} disabled={props.busy || !amountKnown} aria-label={`Séparer — ${context}`}>
          Séparer
        </Button>
        <Button onClick={props.onIgnore} disabled={props.busy || hasAllocations || item.status === 'ignored'} aria-label={`Ignorer — ${context}`}>
          Ignorer
        </Button>
        <Button onClick={props.onReview} disabled={props.busy || hasAllocations || item.status === 'review'} aria-label={`À revoir — ${context}`}>
          À revoir
        </Button>
        <Button onClick={props.onRefund} disabled={props.busy || !amountKnown} aria-label={`Lier un remboursement — ${context}`}>
          Lier un remboursement
        </Button>
        <Button
          onClick={props.onUndo}
          disabled={props.busy || !canUndo}
          aria-label={`Annuler la dernière décision — ${context}`}
        >
          Annuler
        </Button>
      </div>
      {fromRule && lastDecision && (
        <p className="muted">
          Cette décision vient d’une règle : l’API de détail ne fournit pas le lot nécessaire pour l’annuler (manque d’API).
        </p>
      )}
      {hasAllocations && (
        <p className="muted">« Ignorer » et « À revoir » ne s’appliquent qu’aux opérations non ventilées : corrigez d’abord la ventilation.</p>
      )}

      <details className="review-history-inline">
        <summary>Historique de cette opération ({item.history.length})</summary>
        {item.history.length > 0 ? (
          <ol>
            {item.history.map((entry) => (
              <li key={entry.decision_id}>
                <time dateTime={entry.created_at_utc}>{formatDateTime(entry.created_at_utc)}</time> · {actionLabel(entry.action)} ·{' '}
                {entry.actor}
                {entry.undone ? ' · annulée' : ''}
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">Aucune décision enregistrée.</p>
        )}
      </details>
    </article>
  )
}
