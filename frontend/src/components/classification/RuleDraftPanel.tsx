import { useId, useState } from 'react'
import { RULE_APPROVAL_API_AVAILABLE } from '../../lib/api/review'
import { formatCentsToEuro } from '../../lib/money'
import type { FutureRuleResponse } from '../../types/review'
import { categoryLabel, type CategoryIndex } from './labels'

interface SummaryProps {
  simulation: FutureRuleResponse['simulation']
  index: CategoryIndex | null
}

function sumTotals(simulation: FutureRuleResponse['simulation']): bigint | null {
  let total = 0n
  for (const entry of Object.values(simulation.totals_by_category)) {
    if (entry.total_out_of_range || entry.amount_cents === null || !Number.isSafeInteger(entry.amount_cents)) return null
    total += BigInt(entry.amount_cents)
  }
  return total
}

/** Résultat d'une simulation de règle : nombre, euros, catégories, conflits, opérations déjà décidées. */
export function RuleSimulationSummary({ simulation, index }: SummaryProps) {
  const total = sumTotals(simulation)
  const categories = Object.entries(simulation.totals_by_category)
  const alreadyDecided = simulation.human_decision_ignored_ids.length + simulation.already_classified_ids.length
  return (
    <section className="review-simulation" aria-label="Résultat de la simulation">
      <h3>Résultat de la simulation</h3>
      <dl>
        <dt>Opérations déjà importées qui correspondent</dt>
        <dd data-testid="simulation-matched">{simulation.matched_count}</dd>
        <dt>Montant concerné</dt>
        <dd data-testid="simulation-total">{total === null ? 'hors plage entière sûre' : formatCentsToEuro(total)}</dd>
        <dt>Conflits avec d’autres règles</dt>
        <dd data-testid="simulation-conflicts">{simulation.conflict_transaction_ids.length}</dd>
        <dt>Déjà décidées (conservées telles quelles)</dt>
        <dd data-testid="simulation-decided">{alreadyDecided}</dd>
        <dt>Opérations analysées</dt>
        <dd>{simulation.scanned_count}</dd>
      </dl>
      <h4>Catégories touchées</h4>
      {categories.length > 0 ? (
        <ul>
          {categories.map(([categoryId, entry]) => (
            <li key={categoryId}>
              {categoryLabel(index, categoryId)} · {entry.transaction_count} opération(s) ·{' '}
              {entry.amount_cents === null || entry.total_out_of_range ? 'hors plage entière sûre' : formatCentsToEuro(entry.amount_cents)}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">Aucune opération déjà importée ne correspond : la règle ne servira qu’aux futurs imports.</p>
      )}
      {simulation.ambiguous_transaction_ids.length > 0 && (
        <p className="review-warning" role="note">
          {simulation.ambiguous_transaction_ids.length} opération(s) concernent une contrepartie ambiguë : vérifiez-les une à une.
        </p>
      )}
      {simulation.truncated && (
        <p className="review-warning" role="note">
          Simulation tronquée au plafond de sécurité : le résultat ne couvre pas tout l’historique.
        </p>
      )}
    </section>
  )
}

interface PanelProps extends SummaryProps {
  rule: FutureRuleResponse['rule']
  ruleName: string
  onDone: () => void
}

/**
 * Brouillon inactif créé par `future_rule` : simulation d'abord, puis consentement explicite distinct
 * avant toute approbation. L'API T21 n'expose pas encore l'approbation ni la suppression : l'interface
 * le dit au lieu de simuler l'action.
 */
export function RuleDraftPanel({ simulation, index, rule, ruleName, onDone }: PanelProps) {
  const consentId = useId()
  const gapId = useId()
  const [consent, setConsent] = useState(false)
  return (
    <div className="review-rule-draft">
      <p className="review-dialog-intro">
        Le brouillon « {ruleName} » est enregistré et <strong>inactif</strong> : il n’a classé aucune opération,
        ni existante ni future.
      </p>
      <RuleSimulationSummary simulation={simulation} index={index} />
      <p role="status" className="review-rule-state">
        Règle {rule.status === 'draft' ? 'en brouillon (inactive)' : rule.status} · version {rule.version} · portée : futurs
        imports uniquement · aucune opération modifiée.
      </p>
      <fieldset className="review-rule-consent">
        <legend>Approbation de la règle</legend>
        <p>
          Approuver une règle est une décision distincte : elle proposera cette classification pour les futures
          opérations de la même contrepartie exacte, toujours à confirmer par une personne.
        </p>
        <label className="review-checkbox" htmlFor={consentId}>
          <input
            id={consentId}
            type="checkbox"
            checked={consent}
            disabled={!RULE_APPROVAL_API_AVAILABLE}
            aria-describedby={gapId}
            onChange={(event) => setConsent(event.target.checked)}
          />{' '}
          J’ai lu la simulation et je consens à approuver cette règle.
        </label>
        <div className="review-dialog-actions">
          <button className="button button--primary" type="button" disabled={!RULE_APPROVAL_API_AVAILABLE || !consent} aria-describedby={gapId}>
            Approuver la règle
          </button>
          <button className="button" type="button" disabled={!RULE_APPROVAL_API_AVAILABLE} aria-describedby={gapId}>
            Supprimer le brouillon
          </button>
        </div>
        {!RULE_APPROVAL_API_AVAILABLE && (
          <p id={gapId} className="review-api-limit" role="note">
            Manque d’API : le serveur ne propose pas encore l’approbation, l’application ni la suppression d’un
            brouillon de règle. Le brouillon reste inactif et sans effet ; il pourra être approuvé ou supprimé dès que
            ces actions existeront.
          </p>
        )}
      </fieldset>
      <div className="review-dialog-actions">
        <button className="button button--primary" type="button" onClick={onDone}>
          Terminer (le brouillon reste inactif)
        </button>
      </div>
    </div>
  )
}
