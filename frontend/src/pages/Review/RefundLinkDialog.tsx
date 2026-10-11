import { useMemo, useRef, useState, type FormEvent } from 'react'
import { ReviewDialog } from '../../components/classification/ReviewDialog'
import { formatSafeCents, toSafeNumber } from '../../components/classification/labels'
import { formatCentsToEuro, parseEuroInputToCents } from '../../lib/money'
import type { ReviewAccount, ReviewTransaction } from '../../types/review'
import { formatDate } from './helpers'

export interface RefundLinkProposal {
  transaction_link_id: string
  version: number
  status: string
}

export interface RefundValues {
  source_transaction_id: string
  target_transaction_id: string
  amount_cents: number
  expected_source_version: number
  expected_target_version: number
}

interface Props {
  transactions: ReviewTransaction[]
  activeTransactionId: string | null
  accounts: ReviewAccount[]
  error: string | null
  onClose: () => void
  onCreate: (value: RefundValues) => Promise<RefundLinkProposal | undefined>
  onConfirm: (link: RefundLinkProposal) => Promise<boolean>
}

function describe(item: ReviewTransaction, accounts: ReviewAccount[]) {
  const alias = accounts.find((account) => account.account_id === item.account_id)?.alias ?? 'Compte'
  return `${formatDate(item.booked_on)} · ${formatSafeCents(item.amount_cents)} · ${alias}`
}

/** Lie un crédit (remboursement reçu) à un achat débité du même groupe, sans jamais modifier la banque. */
export function RefundLinkDialog({ transactions, activeTransactionId, accounts, error, onClose, onCreate, onConfirm }: Props) {
  const credits = useMemo(() => transactions.filter((item) => item.amount_cents !== null && item.amount_cents > 0), [transactions])
  const debits = useMemo(() => transactions.filter((item) => item.amount_cents !== null && item.amount_cents < 0), [transactions])
  const initialSource = credits.find((item) => item.transaction_id === activeTransactionId) ?? credits[0]
  const initialTarget = debits.find((item) => item.transaction_id === activeTransactionId) ?? debits[0]
  const [sourceId, setSourceId] = useState(initialSource?.transaction_id ?? '')
  const [targetId, setTargetId] = useState(initialTarget?.transaction_id ?? '')
  const [amount, setAmount] = useState(() => {
    if (!initialSource || !initialTarget) return ''
    const credit = BigInt(initialSource.amount_cents!)
    const debit = -BigInt(initialTarget.amount_cents!)
    const smaller = credit < debit ? credit : debit
    return formatCentsToEuro(smaller).replace(/\s*€$/, '')
  })
  const [busy, setBusy] = useState(false)
  const [link, setLink] = useState<RefundLinkProposal | null>(null)
  const firstRef = useRef<HTMLSelectElement>(null)
  const cents = parseEuroInputToCents(amount)
  const source = credits.find((item) => item.transaction_id === sourceId)
  const target = debits.find((item) => item.transaction_id === targetId)
  const centsNumber = cents !== null && cents > 0n ? toSafeNumber(cents) : null
  const ready = Boolean(source && target && centsNumber !== null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !source || !target || centsNumber === null) return
    setBusy(true)
    try {
      const result = await onCreate({
        source_transaction_id: source.transaction_id,
        target_transaction_id: target.transaction_id,
        amount_cents: centsNumber,
        expected_source_version: source.version,
        expected_target_version: target.version,
      })
      if (result) setLink(result)
    } finally {
      setBusy(false)
    }
  }

  async function confirm() {
    if (!link || busy) return
    setBusy(true)
    try {
      await onConfirm(link)
    } finally {
      setBusy(false)
    }
  }

  return (
    <ReviewDialog title="Lier un remboursement" onClose={onClose} closeDisabled={busy} initialFocus={firstRef}>
      {link ? (
        <div>
          <p role="status">Lien proposé (statut « {link.status} »). Vérifiez les deux opérations avant de le confirmer.</p>
          <p>Montant lié : <strong>{cents === null ? '' : formatCentsToEuro(cents)}</strong></p>
          {error && <p className="review-error" role="alert">{error}</p>}
          <div className="review-dialog-actions">
            <button className="button" type="button" onClick={onClose} disabled={busy}>Plus tard</button>
            <button className="button button--primary" type="button" disabled={busy} onClick={() => void confirm()}>
              Confirmer le lien
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={(event) => void submit(event)} noValidate>
          <p className="review-dialog-intro">
            Le crédit rembourse un achat débité. Les deux opérations doivent déjà être classées. Le montant est saisi en euros et
            envoyé en centimes entiers ; les opérations bancaires ne sont jamais modifiées.
          </p>
          {(credits.length === 0 || debits.length === 0) && (
            <p className="review-warning" role="note">
              Ce groupe doit contenir un crédit et un débit pour proposer un lien. Un remboursement rangé dans un autre groupe
              n’est pas encore sélectionnable ici.
            </p>
          )}
          <label>
            Remboursement reçu (crédit)
            <select ref={firstRef} value={sourceId} onChange={(event) => setSourceId(event.target.value)} required>
              <option value="">Choisir un crédit…</option>
              {credits.map((item) => (
                <option key={item.transaction_id} value={item.transaction_id}>{describe(item, accounts)}</option>
              ))}
            </select>
          </label>
          <label>
            Achat remboursé (débit)
            <select value={targetId} onChange={(event) => setTargetId(event.target.value)} required>
              <option value="">Choisir un débit…</option>
              {debits.map((item) => (
                <option key={item.transaction_id} value={item.transaction_id}>{describe(item, accounts)}</option>
              ))}
            </select>
          </label>
          <label>
            Montant du remboursement (€)
            <input
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              inputMode="decimal"
              autoComplete="off"
              placeholder="0,00"
              aria-invalid={amount !== '' && centsNumber === null}
              required
            />
          </label>
          {amount !== '' && centsNumber === null && (
            <p className="review-error" role="alert">Saisissez un montant strictement positif, par exemple 12,34.</p>
          )}
          {error && <p className="review-error" role="alert">{error}</p>}
          <div className="review-dialog-actions">
            <button className="button" type="button" onClick={onClose} disabled={busy}>Annuler</button>
            <button className="button button--primary" type="submit" disabled={busy || !ready}>
              {busy ? 'Enregistrement…' : 'Proposer le lien'}
            </button>
          </div>
        </form>
      )}
    </ReviewDialog>
  )
}
