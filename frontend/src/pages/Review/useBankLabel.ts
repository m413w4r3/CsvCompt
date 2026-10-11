import { useEffect, useState } from 'react'
import { getReviewTransactionLabel } from '../../lib/api/review'

export type BankLabelState = { status: 'loading' } | { status: 'ready'; text: string } | { status: 'unavailable' }

/** Libellé bancaire d'une opération, lu à la demande ; toujours rendu comme texte. */
export function useBankLabel(transactionId: string | null): BankLabelState {
  const [state, setState] = useState<BankLabelState>({ status: 'loading' })
  useEffect(() => {
    if (!transactionId) return
    const controller = new AbortController()
    setState({ status: 'loading' })
    void getReviewTransactionLabel(transactionId, controller.signal)
      .then((text) => setState({ status: 'ready', text }))
      .catch(() => {
        if (!controller.signal.aborted) setState({ status: 'unavailable' })
      })
    return () => controller.abort()
  }, [transactionId])
  return state
}
