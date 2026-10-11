import { useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { formatCentsToEuro, parseEuroInputToCents } from '../../lib/money'
import type {
  CategoryNode,
  FutureRuleDraft,
  FutureRuleResponse,
  ReviewAccount,
  ReviewAllocationInput,
  ReviewDecisionResponse,
  ReviewQueueItem,
  ReviewScope,
  ReviewTransaction,
} from '../../types/review'
import {
  buildCategoryIndex,
  DIMENSION_FIELDS,
  dimensionLabel,
  flowLabel,
  formatEuroInput,
  formatSafeCents,
  toSafeNumber,
  type DimensionField,
} from './labels'
import { ReviewDialog } from './ReviewDialog'
import { RuleDraftPanel } from './RuleDraftPanel'

export type EditorMode = 'classify' | 'split' | 'confirm'

/** Proposition à confirmer (suggestion ou candidat du moteur) préremplissant l'éditeur. */
export interface EditorSeed {
  category_id: string | null
  flow_kind: string | null
  amount_cents: number | null
  suggestion_id: string | null
  source: string
}

export interface EditorSubmit {
  scope: ReviewScope
  /** Première entrée = opération d'ancrage ; une portée `selection` en contient plusieurs. */
  items: Array<{ transaction: ReviewTransaction; allocations: ReviewAllocationInput[] }>
  suggestion_id?: string
  rule?: FutureRuleDraft
}

export type EditorResult = ReviewDecisionResponse | FutureRuleResponse | undefined

interface Props {
  transaction: ReviewTransaction
  /** Opérations cochées dans le détail (peut contenir l'ancrage). */
  selection: ReviewTransaction[]
  group: ReviewQueueItem | null
  categories: CategoryNode[]
  dimensions: Record<string, string[]>
  accounts: ReviewAccount[]
  mode: EditorMode
  seed?: EditorSeed | null
  /** Libellé bancaire de l'opération, affiché en texte brut s'il est connu. */
  bankLabel?: string | null
  initialScope?: ReviewScope
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (value: EditorSubmit) => Promise<EditorResult>
}

interface DraftPart {
  rootId: string
  subId: string
  flow: string
  amount: string
  beneficiary: string
  cadence: string
  flexibility: string
  acquisition: string
}

function emptyPart(): DraftPart {
  return { rootId: '', subId: '', flow: '', amount: '', beneficiary: 'unknown', cadence: 'unknown', flexibility: 'unknown', acquisition: 'unknown' }
}

function isFutureRuleResponse(value: EditorResult): value is FutureRuleResponse {
  return Boolean(value && 'simulation' in value && 'rule' in value)
}

export function AllocationEditor({
  transaction,
  selection,
  group,
  categories,
  dimensions,
  accounts,
  mode,
  seed = null,
  bankLabel = null,
  initialScope = 'this_transaction',
  busy,
  error,
  onClose,
  onSubmit,
}: Props) {
  const index = useMemo(() => buildCategoryIndex(categories), [categories])
  const txCents = Number.isSafeInteger(transaction.amount_cents) && transaction.amount_cents !== null ? BigInt(transaction.amount_cents) : null
  const txIsDebit = txCents !== null && txCents < 0n
  const totalAbs = txCents === null ? null : txIsDebit ? -txCents : txCents
  const sign = txIsDebit ? -1n : 1n

  const [parts, setParts] = useState<DraftPart[]>(() => initialParts(transaction, mode, seed, index, totalAbs))
  const [scope, setScope] = useState<ReviewScope>(initialScope)
  const [ruleName, setRuleName] = useState(() => (group?.counterparty ? `Classer ${group.counterparty}` : ''))
  const [ruleResult, setRuleResult] = useState<FutureRuleResponse | null>(null)
  const firstFieldRef = useRef<HTMLSelectElement>(null)
  const submitRef = useRef<HTMLButtonElement>(null)
  const addRef = useRef<HTMLButtonElement>(null)
  const idPrefix = useId()

  const ambiguous = Boolean(group?.quality_flags.includes('ambiguous_merchant'))
  const futureAllowed = !ambiguous && Boolean(group?.payee_id) && txCents !== null && txCents !== 0n
  const selectionTransactions = selection.some((item) => item.transaction_id === transaction.transaction_id)
    ? selection
    : [transaction, ...selection]
  const selectionAvailable = selection.length > 1
  const isSelection = scope === 'selection'
  const isFuture = scope === 'future_rule'
  const firstPart = parts[0]!

  const amounts = parts.map((part) => parseEuroInputToCents(part.amount))
  const amountsValid = amounts.every((value) => value !== null && value > 0n)
  const allocatedAbs = amounts.reduce<bigint>((sum, value) => sum + (value !== null && value > 0n ? value : 0n), 0n)
  const remainderAbs = totalAbs === null ? null : totalAbs - allocatedAbs

  const partCategory = (part: DraftPart) => {
    const id = part.subId || part.rootId
    return id ? index.byId.get(id) ?? null : null
  }
  const partComplete = (part: DraftPart) => {
    const category = partCategory(part)
    return category !== null && category.allowed_flow_kinds.includes(part.flow)
  }
  const allPartsComplete = parts.every(partComplete)

  const selectionAmountsOk = selectionTransactions.every((item) => Number.isSafeInteger(item.amount_cents) && item.amount_cents !== 0 && item.amount_cents !== null)

  let blocker: string | null = null
  if (txCents === null) blocker = 'Le montant dépasse la plage entière sûre : cette opération ne peut pas être ventilée ici.'
  else if (isFuture) {
    if (!ruleName.trim()) blocker = 'Donnez un nom à la règle.'
    else if (!partComplete(firstPart)) blocker = 'Choisissez une catégorie et un flux compatibles.'
  } else if (isSelection) {
    if (!partComplete(firstPart)) blocker = 'Choisissez une catégorie et un flux compatibles.'
    else if (!selectionAmountsOk) blocker = 'Une opération de la sélection a un montant hors plage : retirez-la.'
    else if (selectionTransactions.length < 2) blocker = 'Cochez au moins deux opérations.'
  } else if (!allPartsComplete) blocker = 'Choisissez une catégorie et un flux compatibles pour chaque part.'
  else if (!amountsValid) blocker = 'Saisissez un montant strictement positif en euros, par exemple 12,34.'
  else if (remainderAbs !== 0n) blocker = 'La somme des parts doit être exactement égale au montant de l’opération.'
  else if (mode === 'split' && parts.length < 2) blocker = 'Ajoutez au moins une deuxième part pour séparer l’opération.'

  const dimensionOptions = (field: DimensionField) => dimensions[field] ?? ['unknown']

  function update(indexToChange: number, change: Partial<DraftPart>) {
    setParts((current) => current.map((part, row) => (row === indexToChange ? { ...part, ...change } : part)))
  }

  function chooseRoot(row: number, rootId: string) {
    const root = index.byId.get(rootId)
    const flows = root?.allowed_flow_kinds ?? []
    update(row, { rootId, subId: '', flow: flows.length === 1 ? flows[0]! : flows.includes('expense') ? 'expense' : '' })
  }

  function chooseSub(row: number, subId: string) {
    const part = parts[row]!
    const category = index.byId.get(subId || part.rootId)
    const flows = category?.allowed_flow_kinds ?? []
    const keep = flows.includes(part.flow) ? part.flow : flows.length === 1 ? flows[0]! : flows.includes('expense') ? 'expense' : ''
    update(row, { subId, flow: keep })
  }

  function allocationFor(part: DraftPart, amountAbs: bigint): ReviewAllocationInput | null {
    const category = partCategory(part)
    const cents = toSafeNumber(amountAbs * sign)
    if (!category || cents === null) return null
    return {
      flow_kind: part.flow,
      category_id: category.category_id,
      amount_cents: cents,
      beneficiary: part.beneficiary,
      cadence: part.cadence,
      flexibility: part.flexibility,
      acquisition: part.acquisition,
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || blocker || txCents === null) return
    const category = partCategory(firstPart)
    if (!category) return

    if (isFuture) {
      const rule: FutureRuleDraft = {
        name: ruleName.trim(),
        condition: { payee_id: group!.payee_id!, direction: txIsDebit ? 'debit' : 'credit' },
        action: {
          flow_kind: firstPart.flow,
          category_id: category.category_id,
          beneficiary: firstPart.beneficiary,
          cadence: firstPart.cadence,
          flexibility: firstPart.flexibility,
          acquisition: firstPart.acquisition,
        },
        priority: 100,
      }
      const result = await onSubmit({ scope, items: [{ transaction, allocations: [] }], rule })
      if (isFutureRuleResponse(result)) setRuleResult(result)
      return
    }

    if (isSelection) {
      const items: EditorSubmit['items'] = []
      for (const item of selectionTransactions) {
        const total = BigInt(item.amount_cents!)
        const allocation = allocationFor(firstPart, total < 0n ? -total : total)
        if (!allocation) return
        // Le signe suit chaque opération, pas l'ancrage.
        allocation.amount_cents = item.amount_cents!
        items.push({ transaction: item, allocations: [allocation] })
      }
      await onSubmit({ scope, items })
      return
    }

    const allocations: ReviewAllocationInput[] = []
    for (const [row, part] of parts.entries()) {
      const allocation = allocationFor(part, amounts[row]!)
      if (!allocation) return
      allocations.push(allocation)
    }
    const unchanged =
      mode === 'confirm' &&
      seed?.suggestion_id &&
      allocations.length === 1 &&
      allocations[0]!.category_id === seed.category_id &&
      allocations[0]!.flow_kind === seed.flow_kind &&
      allocations[0]!.amount_cents === seed.amount_cents
    await onSubmit({
      scope,
      items: [{ transaction, allocations }],
      ...(unchanged ? { suggestion_id: seed!.suggestion_id! } : {}),
    })
  }

  if (ruleResult) {
    return (
      <ReviewDialog title="Brouillon de règle et simulation" onClose={onClose} className="review-dialog--wide">
        <RuleDraftPanel simulation={ruleResult.simulation} index={index} rule={ruleResult.rule} ruleName={ruleName.trim()} onDone={onClose} />
      </ReviewDialog>
    )
  }

  const title = mode === 'split' ? 'Séparer cette opération' : mode === 'confirm' ? 'Confirmer la proposition' : 'Classer cette opération'
  const showAmounts = !isSelection && !isFuture
  const remainderText =
    remainderAbs === null
      ? 'Montant hors plage entière sûre.'
      : remainderAbs === 0n
        ? 'Somme exacte : le total des parts égale le montant de l’opération.'
        : remainderAbs > 0n
          ? `Il reste ${formatCentsToEuro(remainderAbs)} à ventiler.`
          : `Dépassement de ${formatCentsToEuro(-remainderAbs)}.`
  const submitLabel = busy
    ? 'Enregistrement…'
    : isFuture
      ? 'Simuler et créer un brouillon inactif'
      : isSelection
        ? `Classer la sélection (${selectionTransactions.length})`
        : mode === 'confirm'
          ? 'Confirmer'
          : mode === 'split'
            ? 'Enregistrer la séparation'
            : 'Classer'
  const accountAlias = accounts.find((account) => account.account_id === transaction.account_id)?.alias ?? 'Compte'
  const hintId = `${idPrefix}-hint`

  return (
    <ReviewDialog
      title={title}
      onClose={onClose}
      closeDisabled={busy}
      className="review-dialog--wide"
      initialFocus={mode === 'confirm' && blocker === null ? submitRef : firstFieldRef}
    >
      <form onSubmit={(event) => void submit(event)} noValidate>
        <p className="review-dialog-intro">
          <strong>{formatSafeCents(transaction.amount_cents)}</strong> · {transaction.booked_on} · {accountAlias}
          {bankLabel ? <> · <span className="review-bank-label">{bankLabel}</span></> : null}
        </p>
        {mode === 'confirm' && seed && (
          <p className="review-seed" role="note">
            Proposition issue de la source « {sourceName(seed.source)} ». Elle ne devient une décision qu’après votre confirmation.
          </p>
        )}

        <fieldset className="review-scope">
          <legend>Portée de la décision</legend>
          <label className="review-scope-option">
            <input type="radio" name={`${idPrefix}-scope`} checked={scope === 'this_transaction'} onChange={() => setScope('this_transaction')} />
            <strong>Cette opération</strong>
            <small>Seule l’opération affichée est modifiée.</small>
          </label>
          <label className="review-scope-option">
            <input
              type="radio"
              name={`${idPrefix}-scope`}
              checked={scope === 'selection'}
              disabled={!selectionAvailable}
              onChange={() => setScope('selection')}
            />
            <strong>Cette sélection</strong>
            <small>
              {selectionAvailable
                ? `${selectionTransactions.length} opérations cochées, chacune avec son montant exact et une seule catégorie.`
                : 'Cochez au moins deux opérations dans le groupe pour activer cette portée.'}
            </small>
          </label>
          <label className="review-scope-option">
            <input
              type="radio"
              name={`${idPrefix}-scope`}
              checked={scope === 'future_rule'}
              disabled={!futureAllowed}
              onChange={() => setScope('future_rule')}
            />
            <strong>Futures correspondances</strong>
            <small>
              {futureAllowed
                ? 'Crée un brouillon de règle inactif et le simule ; ne classe aucune opération existante.'
                : ambiguous
                  ? 'Indisponible : contrepartie ambiguë (Amazon, Vinted, PayPal…), aucune règle globale.'
                  : 'Indisponible : une règle exige une contrepartie validée.'}
            </small>
          </label>
          {ambiguous && (
            <p className="review-warning" role="note">
              Cette contrepartie peut recouvrir plusieurs vendeurs ou usages : classez les opérations une par une ou par sélection.
            </p>
          )}
        </fieldset>

        {isFuture && (
          <div className="review-rule-fields">
            <label>
              Nom de la règle
              <input value={ruleName} maxLength={120} onChange={(event) => setRuleName(event.target.value)} required />
            </label>
            <p>
              Contrepartie exacte « {group?.counterparty} », {txIsDebit ? 'débits' : 'crédits'} uniquement, futurs imports seulement.
              Cette portée ne classe pas l’opération affichée : classez-la d’abord avec « Cette opération ».
            </p>
          </div>
        )}

        {parts.map((part, row) => {
          const root = part.rootId ? index.byId.get(part.rootId) : undefined
          const subOptions = root?.children ?? []
          const category = partCategory(part)
          const flows = category?.allowed_flow_kinds ?? []
          const subRequired = Boolean(root) && root!.allowed_flow_kinds.length === 0 && subOptions.length > 0
          const partId = `${idPrefix}-part-${row}`
          return (
            <fieldset className="review-allocation-row" key={row}>
              <legend>{parts.length > 1 && showAmounts ? `Part ${row + 1}` : 'Affectation'}</legend>
              <label>
                Catégorie
                <select
                  ref={row === 0 ? firstFieldRef : undefined}
                  value={part.rootId}
                  onChange={(event) => chooseRoot(row, event.target.value)}
                  required
                >
                  <option value="">Choisir une catégorie…</option>
                  {index.roots.map((option) => (
                    <option key={option.category_id} value={option.category_id}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label>
                Sous-catégorie
                <select value={part.subId} onChange={(event) => chooseSub(row, event.target.value)} disabled={subOptions.length === 0} required={subRequired}>
                  <option value="">{subOptions.length === 0 ? 'Aucune sous-catégorie' : subRequired ? 'Choisir une sous-catégorie…' : 'Catégorie générale'}</option>
                  {subOptions.map((option) => (
                    <option key={option.category_id} value={option.category_id}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label>
                Flux
                <select value={part.flow} onChange={(event) => update(row, { flow: event.target.value })} disabled={flows.length === 0} required>
                  <option value="">Choisir un flux…</option>
                  {flows.map((flow) => (
                    <option key={flow} value={flow}>{flowLabel(flow)}</option>
                  ))}
                </select>
              </label>
              {showAmounts && (
                <label>
                  Montant (€), sans signe
                  <input
                    id={`${partId}-amount`}
                    inputMode="decimal"
                    autoComplete="off"
                    placeholder="0,00"
                    value={part.amount}
                    aria-describedby={`${idPrefix}-remainder`}
                    aria-invalid={part.amount !== '' && (amounts[row] === null || amounts[row]! <= 0n)}
                    onChange={(event) => update(row, { amount: event.target.value })}
                    required
                  />
                </label>
              )}
              {DIMENSION_FIELDS.map(([field, label]) => (
                <label key={field}>
                  {label}
                  <select value={part[field]} onChange={(event) => update(row, { [field]: event.target.value } as Partial<DraftPart>)}>
                    {dimensionOptions(field).map((value) => (
                      <option key={value} value={value}>{dimensionLabel(value)}</option>
                    ))}
                  </select>
                </label>
              ))}
              {showAmounts && (
                <div className="review-row-actions">
                  {remainderAbs !== null && remainderAbs > 0n && (
                    <button
                      type="button"
                      className="button"
                      onClick={() => update(row, { amount: formatEuroInput((amounts[row] !== null && amounts[row]! > 0n ? amounts[row]! : 0n) + remainderAbs) })}
                    >
                      Attribuer le reste à cette part
                    </button>
                  )}
                  {parts.length > 1 && (
                    <button
                      type="button"
                      className="button"
                      aria-label={`Retirer la part ${row + 1}`}
                      onClick={() => {
                        setParts((current) => current.filter((_, position) => position !== row))
                        requestAnimationFrame(() => addRef.current?.focus())
                      }}
                    >
                      Retirer
                    </button>
                  )}
                </div>
              )}
            </fieldset>
          )
        })}

        {showAmounts && (
          <div className="review-split-summary">
            <span>Montant de l’opération : <strong>{formatSafeCents(transaction.amount_cents === null ? null : Math.abs(transaction.amount_cents))}</strong></span>
            <span>Somme des parts : <strong data-testid="split-allocated">{formatCentsToEuro(allocatedAbs)}</strong></span>
            <span id={`${idPrefix}-remainder`} role="status" aria-live="polite" data-testid="split-remainder" className={remainderAbs === 0n ? 'is-exact' : 'is-off'}>
              {remainderText}
            </span>
            {(mode === 'split' || parts.length > 1) && parts.length < 20 && (
              <button
                ref={addRef}
                type="button"
                className="button"
                onClick={() => setParts((current) => [...current, emptyPart()])}
              >
                Ajouter une part
              </button>
            )}
          </div>
        )}
        {isSelection && (
          <p className="muted">
            La sélection applique une catégorie unique à chaque opération cochée et conserve son montant exact. Pour séparer une
            opération en plusieurs parts, ouvrez-la seule.
          </p>
        )}

        {error && <p className="review-error" role="alert">{error}</p>}
        {blocker && <p id={hintId} className="review-submit-hint" role="note">{blocker}</p>}
        <div className="review-dialog-actions">
          <button className="button" type="button" onClick={onClose} disabled={busy}>Annuler</button>
          <button
            ref={submitRef}
            className="button button--primary"
            type="submit"
            disabled={busy || blocker !== null}
            aria-describedby={blocker ? hintId : undefined}
          >
            {submitLabel}
          </button>
        </div>
      </form>
    </ReviewDialog>
  )
}

function sourceName(source: string) {
  return ({ manual: 'décision humaine', human: 'décision humaine', rule: 'règle', memory: 'mémoire', history: 'mémoire', heuristic: 'heuristique', llm: 'suggestion IA' })[source] ?? source
}

function initialParts(
  transaction: ReviewTransaction,
  mode: EditorMode,
  seed: EditorSeed | null,
  index: ReturnType<typeof buildCategoryIndex>,
  totalAbs: bigint | null,
): DraftPart[] {
  const locate = (categoryId: string | null) => {
    const found = categoryId ? index.byId.get(categoryId) : undefined
    if (!found) return { rootId: '', subId: '' }
    return found.parent_id ? { rootId: found.parent_id, subId: found.category_id } : { rootId: found.category_id, subId: '' }
  }
  const fromAllocations = transaction.allocations.filter((item) => item.amount_cents !== null && Number.isSafeInteger(item.amount_cents))
  if (mode === 'confirm' && seed?.category_id) {
    const place = locate(seed.category_id)
    const amount = seed.amount_cents !== null && Number.isSafeInteger(seed.amount_cents) ? BigInt(Math.abs(seed.amount_cents)) : totalAbs
    return [{ ...emptyPart(), ...place, flow: seed.flow_kind ?? '', amount: amount === null ? '' : formatEuroInput(amount) }]
  }
  if (fromAllocations.length > 0 && fromAllocations.length === transaction.allocations.length) {
    return fromAllocations.map((item) => ({
      ...emptyPart(),
      ...locate(item.category_id),
      flow: item.flow_kind,
      amount: formatEuroInput(BigInt(item.amount_cents!)),
      beneficiary: item.beneficiary,
      cadence: item.cadence,
      flexibility: item.flexibility,
      acquisition: item.acquisition,
    }))
  }
  if (mode === 'split') return [emptyPart(), emptyPart()]
  return [{ ...emptyPart(), amount: totalAbs === null ? '' : formatEuroInput(totalAbs) }]
}
