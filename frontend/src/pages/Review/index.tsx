import { Component, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  AllocationEditor,
  type EditorMode,
  type EditorResult,
  type EditorSeed,
  type EditorSubmit,
} from '../../components/classification/AllocationEditor'
import {
  buildCategoryIndex,
  formatSafeCents,
  qualityFlagLabel,
  statusLabel,
} from '../../components/classification/labels'
import { ReviewDialog } from '../../components/classification/ReviewDialog'
import { Badge, Button, Card, EmptyState, PageHeader } from '../../components/ui'
import {
  confirmReviewRefundLink,
  createReviewDecision,
  createReviewRefundLink,
  getReviewAccounts,
  getReviewCategories,
  getReviewGroup,
  getReviewQueue,
  isStaleStateError,
  newIdempotencyKey,
  REVIEW_ACTOR,
  reviewErrorMessage,
  undoReviewDecision,
  type ReviewQueueFilters,
} from '../../lib/api/review'
import { ApiError } from '../../lib/api/client'
import type {
  FutureRuleResponse,
  ReviewAccount,
  ReviewCategories,
  ReviewDecisionRequest,
  ReviewGroup,
  ReviewQueueItem,
  ReviewScope,
  ReviewStatus,
  ReviewTransaction,
} from '../../types/review'
import { HelpDialog } from './HelpDialog'
import {
  formatDate,
  formatDateTime,
  isActionable,
  isActivatableTarget,
  isTypingTarget,
  latestUndoableDecision,
  mergeById,
  proposalsFor,
  actionLabel,
  sortGroups,
  type Proposal,
  type SortMode,
} from './helpers'
import { RefundLinkDialog, type RefundLinkProposal, type RefundValues } from './RefundLinkDialog'
import { TransactionCard } from './TransactionCard'
import { useBankLabel } from './useBankLabel'
import './review.css'

const DEFAULT_START = '2026-01-01'
const DEFAULT_END = '2026-12-31'
const CARD_PAGE = 20
const STATUS_OPTIONS: Array<{ value: ReviewStatus; label: string }> = [
  { value: 'unclassified', label: 'Non ventilées' },
  { value: 'suggested', label: 'Suggestions à vérifier' },
  { value: 'rule', label: 'Règles' },
  { value: 'memory', label: 'Mémoire' },
  { value: 'human', label: 'Décisions humaines' },
  { value: 'ignored', label: 'Ignorées' },
  { value: 'review', label: 'À revoir' },
]

function filtersFrom(params: URLSearchParams): ReviewQueueFilters {
  const direction = params.get('direction')
  const status = params.get('status')
  return {
    period_start: params.get('period_start') || DEFAULT_START,
    period_end: params.get('period_end') || DEFAULT_END,
    account_id: params.get('account_id') || undefined,
    direction: direction === 'debit' || direction === 'credit' ? direction : undefined,
    status: STATUS_OPTIONS.some((item) => item.value === status) ? (status as ReviewStatus) : undefined,
  }
}

interface EditorState {
  mode: EditorMode
  transactionId: string
  seed: EditorSeed | null
  scope: ReviewScope
}

type UrlChange = {
  group?: string | null
  sort?: SortMode
  account_id?: string
  period_start?: string
  period_end?: string
  direction?: string
  status?: string
}

export function Review() {
  return (
    <ReviewRouteBoundary>
      <ReviewPageContent />
    </ReviewRouteBoundary>
  )
}

function ReviewPageContent() {
  const [searchParams, setSearchParams] = useSearchParams()
  const effective = filtersFrom(searchParams)
  // Clé calculée sur les valeurs effectives (défauts inclus) : la normalisation de l'URL ne relance pas la file.
  const filterKey = [effective.period_start, effective.period_end, effective.account_id, effective.direction, effective.status]
    .map((value) => value ?? '')
    .join('|')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const filters = useMemo(() => effective, [filterKey])
  const sortMode: SortMode = searchParams.get('sort') === 'recent' ? 'recent' : 'impact'
  const selectedGroupId = searchParams.get('group')

  const [queueItems, setQueueItems] = useState<ReviewQueueItem[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loadingQueue, setLoadingQueue] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [queueError, setQueueError] = useState<string | null>(null)
  const [reloadRevision, setReloadRevision] = useState(0)

  const [detail, setDetail] = useState<ReviewGroup | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailRevision, setDetailRevision] = useState(0)
  const [visibleCards, setVisibleCards] = useState(CARD_PAGE)

  const [categories, setCategories] = useState<ReviewCategories | null>(null)
  const [accounts, setAccounts] = useState<ReviewAccount[]>([])
  const [catalogError, setCatalogError] = useState<string | null>(null)

  const [editor, setEditor] = useState<EditorState | null>(null)
  const [editorError, setEditorError] = useState<string | null>(null)
  const [refundOpen, setRefundOpen] = useState(false)
  const [refundError, setRefundError] = useState<string | null>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  const [overrideTarget, setOverrideTarget] = useState<ReviewTransaction | null>(null)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const lastCheckedIndex = useRef<number | null>(null)
  const preferredIndex = useRef(0)
  const loadedFilterKey = useRef<string | null>(null)
  const focusDetailRef = useRef(false)
  const listRef = useRef<HTMLUListElement>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const pendingKey = useRef<{ fingerprint: string; key: string } | null>(null)

  const categoryIndex = useMemo(() => (categories ? buildCategoryIndex(categories.items) : null), [categories])

  const setUrl = useCallback(
    (change: UrlChange, replace = false) => {
      const params = new URLSearchParams(searchParams)
      if ('group' in change) {
        if (change.group) params.set('group', change.group)
        else params.delete('group')
      }
      if ('sort' in change) {
        if (!change.sort || change.sort === 'impact') params.delete('sort')
        else params.set('sort', change.sort)
      }
      for (const key of ['account_id', 'period_start', 'period_end', 'direction', 'status'] as const) {
        if (key in change) {
          const value = change[key]
          if (value) params.set(key, value)
          else params.delete(key)
        }
      }
      // Aucun libellé ni terme de recherche ne transite par l'URL.
      params.delete('q')
      setSearchParams(params, { replace })
    },
    [searchParams, setSearchParams],
  )

  // Normalise l'URL : période par défaut 2026, jamais de `q`.
  useEffect(() => {
    const params = new URLSearchParams(searchParams)
    let changed = false
    if (params.has('q')) {
      params.delete('q')
      changed = true
    }
    if (!params.has('period_start')) {
      params.set('period_start', DEFAULT_START)
      changed = true
    }
    if (!params.has('period_end')) {
      params.set('period_end', DEFAULT_END)
      changed = true
    }
    if (changed) setSearchParams(params, { replace: true })
  }, [searchParams, setSearchParams])

  useEffect(() => {
    const controller = new AbortController()
    void Promise.all([getReviewCategories(controller.signal), getReviewAccounts(controller.signal)])
      .then(([tree, accountList]) => {
        setCategories(tree)
        setAccounts(accountList)
        setCatalogError(null)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setCatalogError(reviewErrorMessage(error))
      })
    return () => controller.abort()
  }, [])

  // File : premier chargement ou changement de filtre = remplacement ; simple rechargement = lecture douce.
  useEffect(() => {
    const controller = new AbortController()
    const replace = loadedFilterKey.current !== filterKey
    if (replace) {
      setQueueItems([])
      setNextCursor(null)
    }
    setQueueError(null)
    if (filters.period_start && filters.period_end && filters.period_end < filters.period_start) {
      setQueueError('La date de fin précède la date de début : corrigez la période.')
      setLoadingQueue(false)
      return
    }
    setLoadingQueue(true)
    void getReviewQueue(filters, null, controller.signal)
      .then((page) => {
        loadedFilterKey.current = filterKey
        setQueueItems(Array.isArray(page?.items) ? page.items : [])
        setNextCursor(page?.page?.next_cursor ?? null)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setQueueError(reviewErrorMessage(error))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingQueue(false)
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, reloadRevision])

  const sortedItems = useMemo(() => sortGroups(queueItems, sortMode), [queueItems, sortMode])
  const selectedItem = sortedItems.find((item) => item.review_group_id === selectedGroupId) ?? null
  const selectedPosition = sortedItems.findIndex((item) => item.review_group_id === selectedGroupId)

  useEffect(() => {
    if (selectedPosition >= 0) preferredIndex.current = selectedPosition
  }, [selectedPosition])

  // Sélection automatique : le groupe voisin (même rang) après une décision, sinon le premier.
  useEffect(() => {
    if (loadingQueue || queueError) return
    if (sortedItems.length === 0) {
      if (selectedGroupId) setUrl({ group: null }, true)
      return
    }
    if (selectedItem) return
    if (selectedGroupId && nextCursor) return
    const target = sortedItems[Math.min(preferredIndex.current, sortedItems.length - 1)]
    if (target) setUrl({ group: target.review_group_id }, true)
  }, [loadingQueue, queueError, sortedItems, selectedItem, selectedGroupId, nextCursor, setUrl])

  const detailGroupRef = useRef<string | null>(null)
  useEffect(() => {
    if (!selectedGroupId) {
      detailGroupRef.current = null
      setDetail(null)
      setDetailError(null)
      setLoadingDetail(false)
      return
    }
    const sameGroup = detailGroupRef.current === selectedGroupId
    if (!sameGroup) {
      setDetail(null)
      setSelectedIds([])
      setActiveId(null)
      setVisibleCards(CARD_PAGE)
      lastCheckedIndex.current = null
    }
    detailGroupRef.current = selectedGroupId
    setDetailError(null)
    setLoadingDetail(true)
    const controller = new AbortController()
    void getReviewGroup(selectedGroupId, controller.signal)
      .then((result) => {
        setDetail(result)
        const ids = new Set(result.transactions.map((item) => item.transaction_id))
        setSelectedIds((current) => current.filter((id) => ids.has(id)))
        setActiveId((current) => (current && ids.has(current) ? current : (result.transactions[0]?.transaction_id ?? null)))
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setDetail(null)
        setDetailError(reviewErrorMessage(error))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingDetail(false)
      })
    return () => controller.abort()
  }, [selectedGroupId, detailRevision])

  // Après un clic sur un groupe, le focus rejoint le titre du détail (utile en mise en page empilée).
  useEffect(() => {
    if (detail && focusDetailRef.current) {
      focusDetailRef.current = false
      headingRef.current?.focus()
    }
  }, [detail])

  const transactions = detail?.transactions ?? []
  const activeTransaction = transactions.find((item) => item.transaction_id === activeId) ?? transactions[0] ?? null
  const chosen = transactions.filter((item) => selectedIds.includes(item.transaction_id))
  const modalOpen = Boolean(editor || refundOpen || helpOpen || overrideTarget)
  const editorTransaction = editor ? transactions.find((item) => item.transaction_id === editor.transactionId) ?? null : null
  const editorLabel = useBankLabel(editorTransaction?.transaction_id ?? null)

  const announce = useCallback((message: string) => setAnnouncement(message), [])

  const reloadQueue = useCallback(() => setReloadRevision((value) => value + 1), [])
  const reloadDetail = useCallback(() => setDetailRevision((value) => value + 1), [])
  const afterWrite = useCallback(() => {
    reloadQueue()
    reloadDetail()
    requestAnimationFrame(() => listRef.current?.focus())
  }, [reloadDetail, reloadQueue])

  /** Une clé d'idempotence par corps : un nouvel essai identique rejoue, un corps modifié repart à neuf. */
  function keyFor(payload: unknown): string {
    const fingerprint = JSON.stringify(payload)
    if (pendingKey.current?.fingerprint !== fingerprint) pendingKey.current = { fingerprint, key: newIdempotencyKey() }
    return pendingKey.current.key
  }

  async function submitDecision(value: EditorSubmit): Promise<EditorResult> {
    if (busy || !editor) return undefined
    const anchor = value.items[0]!
    const mode = editor.mode
    const body: ReviewDecisionRequest = {
      transaction_id: anchor.transaction.transaction_id,
      expected_version: anchor.transaction.version,
      actor: REVIEW_ACTOR,
      action: mode === 'confirm' ? 'confirm' : 'classify',
      scope: value.scope,
      allocations: value.scope === 'future_rule' ? [] : anchor.allocations,
      ...(value.scope === 'selection'
        ? {
            selection: value.items.slice(1).map((item) => ({
              transaction_id: item.transaction.transaction_id,
              expected_version: item.transaction.version,
              allocations: item.allocations,
            })),
          }
        : {}),
      ...(value.suggestion_id ? { suggestion_id: value.suggestion_id } : {}),
      ...(value.rule ? { rule: value.rule } : {}),
    }
    setBusy(true)
    setEditorError(null)
    try {
      const result = await createReviewDecision(body, keyFor(body))
      pendingKey.current = null
      if ('simulation' in result) {
        announce(
          `Brouillon de règle créé, inactif. Simulation : ${result.simulation.matched_count} opération(s) correspondante(s). Aucune opération n’a été modifiée.`,
        )
        return result as FutureRuleResponse
      }
      announce(
        value.scope === 'selection'
          ? `${result.items.length} opérations classées. La décision est conservée et annulable.`
          : 'Décision enregistrée. Elle est conservée après rechargement et annulable.',
      )
      setEditor(null)
      setSelectedIds([])
      afterWrite()
      return result
    } catch (error) {
      setEditorError(reviewErrorMessage(error))
      if (isStaleStateError(error)) reloadDetail()
      return undefined
    } finally {
      setBusy(false)
    }
  }

  async function markTransaction(transaction: ReviewTransaction, action: 'ignore' | 'mark_for_review') {
    if (busy) return
    const body: ReviewDecisionRequest = {
      transaction_id: transaction.transaction_id,
      expected_version: transaction.version,
      actor: REVIEW_ACTOR,
      action,
      scope: 'this_transaction',
    }
    setBusy(true)
    setActionError(null)
    try {
      await createReviewDecision(body, keyFor(body))
      pendingKey.current = null
      announce(action === 'ignore' ? 'Opération ignorée. Classez-la pour la sortir de cet état.' : 'Opération marquée à revoir.')
      afterWrite()
    } catch (error) {
      setActionError(reviewErrorMessage(error))
      if (isStaleStateError(error)) reloadDetail()
    } finally {
      setBusy(false)
    }
  }

  async function undoTransaction(transaction: ReviewTransaction, confirmOverride = false) {
    const decision = latestUndoableDecision(transaction.history)
    if (!decision || busy) return
    const body = { expected_version: transaction.version, actor: REVIEW_ACTOR, ...(confirmOverride ? { confirm_human_override: true } : {}) }
    setBusy(true)
    setActionError(null)
    try {
      await undoReviewDecision(decision.decision_id, body, keyFor({ decision: decision.decision_id, ...body }))
      pendingKey.current = null
      setOverrideTarget(null)
      announce('Décision annulée. L’opération bancaire d’origine n’a pas changé.')
      afterWrite()
    } catch (error) {
      if (error instanceof ApiError && error.code === 'UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED' && !confirmOverride) {
        setOverrideTarget(transaction)
      } else {
        setOverrideTarget(null)
        setActionError(reviewErrorMessage(error))
        if (isStaleStateError(error)) reloadDetail()
      }
    } finally {
      setBusy(false)
    }
  }

  async function createRefund(values: RefundValues) {
    setRefundError(null)
    try {
      const body = { ...values, actor: REVIEW_ACTOR }
      const result = await createReviewRefundLink(body, keyFor(body))
      pendingKey.current = null
      announce('Proposition de remboursement enregistrée. Confirmez le lien après vérification.')
      return { transaction_link_id: result.transaction_link_id, version: result.version, status: result.status } satisfies RefundLinkProposal
    } catch (error) {
      setRefundError(reviewErrorMessage(error))
      if (isStaleStateError(error)) reloadDetail()
      return undefined
    }
  }

  async function confirmRefund(link: RefundLinkProposal) {
    setRefundError(null)
    try {
      const body = { expected_version: link.version, actor: REVIEW_ACTOR }
      await confirmReviewRefundLink(link.transaction_link_id, body, keyFor({ link: link.transaction_link_id, ...body }))
      pendingKey.current = null
      announce('Lien de remboursement confirmé.')
      setRefundOpen(false)
      afterWrite()
      return true
    } catch (error) {
      setRefundError(reviewErrorMessage(error))
      return false
    }
  }

  const openEditor = useCallback(
    (mode: EditorMode, transaction: ReviewTransaction | null, seed: EditorSeed | null = null, scope: ReviewScope = 'this_transaction') => {
      if (!transaction) return
      if (!categories) {
        announce(catalogError ?? 'La taxonomie des catégories est en cours de chargement.')
        return
      }
      if (transaction.amount_cents === null || transaction.total_out_of_range) {
        setActionError('Le montant de cette opération dépasse la plage entière sûre : elle ne peut pas être ventilée ici.')
        return
      }
      setActionError(null)
      setEditorError(null)
      setActiveId(transaction.transaction_id)
      setEditor({ mode, transactionId: transaction.transaction_id, seed, scope })
    },
    [announce, catalogError, categories],
  )

  const confirmProposal = useCallback(
    (transaction: ReviewTransaction, proposal: Proposal) => {
      openEditor(
        'confirm',
        transaction,
        {
          category_id: proposal.category_id,
          flow_kind: proposal.flow_kind,
          amount_cents: proposal.amount_cents,
          suggestion_id: proposal.suggestion_id,
          source: proposal.source,
        },
      )
    },
    [openEditor],
  )

  const openTopProposal = useCallback(() => {
    if (!activeTransaction) return
    const proposal = proposalsFor(activeTransaction).find(isActionable)
    if (!proposal) {
      announce('Aucune proposition complète à confirmer pour cette opération : utilisez « Classer » pour choisir une catégorie.')
      return
    }
    confirmProposal(activeTransaction, proposal)
  }, [activeTransaction, announce, confirmProposal])

  function toggleSelection(index: number, transaction: ReviewTransaction, shiftKey: boolean) {
    const visible = transactions.slice(0, visibleCards)
    setSelectedIds((current) => {
      const checked = !current.includes(transaction.transaction_id)
      const anchor = lastCheckedIndex.current
      lastCheckedIndex.current = index
      const range =
        shiftKey && anchor !== null
          ? visible.slice(Math.min(anchor, index), Math.max(anchor, index) + 1).map((item) => item.transaction_id)
          : [transaction.transaction_id]
      const next = new Set(current)
      range.forEach((id) => (checked ? next.add(id) : next.delete(id)))
      return [...next]
    })
  }

  const moveGroup = useCallback(
    (delta: -1 | 1) => {
      if (sortedItems.length === 0) return
      const current = sortedItems.findIndex((item) => item.review_group_id === selectedGroupId)
      const target = sortedItems[Math.min(sortedItems.length - 1, Math.max(0, (current < 0 ? 0 : current) + delta))]
      if (target && target.review_group_id !== selectedGroupId) setUrl({ group: target.review_group_id })
    },
    [selectedGroupId, setUrl, sortedItems],
  )

  const undoActive = useCallback(() => {
    if (!activeTransaction) return
    if (!latestUndoableDecision(activeTransaction.history)) {
      announce('Aucune décision annulable sur cette opération.')
      return
    }
    void undoTransaction(activeTransaction)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTransaction, announce, busy])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (modalOpen || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return
      if (isTypingTarget(event.target)) return
      if (event.key === 'Enter') {
        if (isActivatableTarget(event.target)) return
        event.preventDefault()
        openTopProposal()
        return
      }
      const key = event.key === '?' ? '?' : event.key.toLowerCase()
      if (key === 'j') {
        event.preventDefault()
        moveGroup(1)
      } else if (key === 'k') {
        event.preventDefault()
        moveGroup(-1)
      } else if (key === 'c') {
        event.preventDefault()
        openEditor('classify', activeTransaction)
      } else if (key === 's') {
        event.preventDefault()
        openEditor('split', activeTransaction)
      } else if (key === 'u') {
        event.preventDefault()
        undoActive()
      } else if (key === '?') {
        event.preventDefault()
        setHelpOpen(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [activeTransaction, modalOpen, moveGroup, openEditor, openTopProposal, undoActive])

  async function loadMore() {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    setQueueError(null)
    try {
      const page = await getReviewQueue(filters, nextCursor)
      setQueueItems((current) => mergeById(current, page.items))
      setNextCursor(page.page.next_cursor)
    } catch (error) {
      setQueueError(reviewErrorMessage(error))
      if (error instanceof ApiError && (error.code === 'CURSOR_STALE' || error.code === 'CURSOR_INVALID')) setNextCursor(null)
    } finally {
      setLoadingMore(false)
    }
  }

  function onListKeyDown(event: ReactKeyboardEvent<HTMLUListElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      moveGroup(1)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      moveGroup(-1)
    }
  }

  function onListClick(event: React.MouseEvent<HTMLUListElement>) {
    const option = (event.target as HTMLElement).closest<HTMLElement>('[data-review-group-id]')
    const id = option?.dataset.reviewGroupId
    if (!id) return
    focusDetailRef.current = id !== selectedGroupId
    setUrl({ group: id })
    setActionError(null)
  }

  const allVisibleSelected = transactions.length > 0 && transactions.slice(0, visibleCards).every((item) => selectedIds.includes(item.transaction_id))
  const groupTotal = selectedItem ? (selectedItem.total_out_of_range ? 'hors plage entière sûre' : formatSafeCents(selectedItem.amount_cents)) : null
  const flags = selectedItem?.quality_flags ?? []
  const ambiguous = flags.includes('ambiguous_merchant')

  return (
    <>
      <PageHeader
        eyebrow="DÉCISIONS"
        title="Revue"
        description="Classez, séparez, corrigez et annulez vos décisions. Les propositions restent à confirmer par une personne."
      />

      <Card className="review-filter-card">
        <form className="review-filters" aria-label="Filtres de la file de revue" onSubmit={(event) => event.preventDefault()}>
          <label>
            Du
            <input type="date" value={filters.period_start ?? ''} onChange={(event) => setUrl({ period_start: event.target.value, group: null })} />
          </label>
          <label>
            Au
            <input type="date" value={filters.period_end ?? ''} onChange={(event) => setUrl({ period_end: event.target.value, group: null })} />
          </label>
          <label>
            Compte
            <select value={filters.account_id ?? ''} onChange={(event) => setUrl({ account_id: event.target.value, group: null })}>
              <option value="">Tous les comptes</option>
              {accounts.filter((item) => item.is_active).map((item) => (
                <option key={item.account_id} value={item.account_id}>{item.alias}</option>
              ))}
            </select>
          </label>
          <label>
            Statut
            <select value={filters.status ?? ''} onChange={(event) => setUrl({ status: event.target.value, group: null })}>
              <option value="">Tous les statuts</option>
              {STATUS_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label>
            Sens
            <select value={filters.direction ?? ''} onChange={(event) => setUrl({ direction: event.target.value, group: null })}>
              <option value="">Tous</option>
              <option value="debit">Débits</option>
              <option value="credit">Crédits</option>
            </select>
          </label>
          <fieldset className="review-sort">
            <legend>Trier les groupes chargés par</legend>
            <label>
              <input type="radio" name="review-sort" checked={sortMode === 'impact'} onChange={() => setUrl({ sort: 'impact' })} /> Impact
            </label>
            <label>
              <input type="radio" name="review-sort" checked={sortMode === 'recent'} onChange={() => setUrl({ sort: 'recent' })} /> Nouveauté
            </label>
          </fieldset>
        </form>
      </Card>

      {catalogError && (
        <p className="review-notice" role="status">
          Les catégories ou les comptes n’ont pas pu être chargés ; les actions de ventilation sont indisponibles. {catalogError}
        </p>
      )}
      {queueError && (
        <div className="review-error-state" role="alert">
          <p>{queueError}</p>
          <Button onClick={reloadQueue}>Recharger la file</Button>
        </div>
      )}

      {loadingQueue && queueItems.length === 0 ? (
        <div className="review-loading" role="status">
          <span className="spinner" aria-hidden="true" /> Chargement de la file de revue…
        </div>
      ) : sortedItems.length === 0 && !queueError ? (
        <EmptyState
          title="La file de revue est vide"
          description="Aucune opération ne correspond à ces filtres. Après un import, les opérations non ventilées et les suggestions à vérifier apparaîtront ici ; rien n’est appliqué automatiquement."
        />
      ) : (
        <div className="review-layout">
          <section className="review-groups" aria-label="File des groupes à revoir" aria-busy={loadingQueue}>
            <div className="review-groups-heading">
              <h2 id="review-groups-title">À traiter</h2>
              <span>{queueItems.length} groupe(s) chargé(s)</span>
            </div>
            {sortMode === 'recent' && nextCursor && (
              <p className="muted">Le tri par nouveauté s’applique aux groupes déjà chargés ; chargez la suite pour les inclure.</p>
            )}
            {/* Le composant liste suit le motif listbox : un seul arrêt de tabulation, flèches / j / k pour naviguer. */}
            <ul
              ref={listRef}
              className="review-group-list"
              role="listbox"
              tabIndex={0}
              aria-labelledby="review-groups-title"
              aria-activedescendant={selectedItem ? `review-group-${selectedItem.review_group_id}` : undefined}
              onKeyDown={onListKeyDown}
              onClick={onListClick}
            >
              {sortedItems.map((item) => {
                const isSelected = item.review_group_id === selectedGroupId
                return (
                  <li
                    key={item.review_group_id}
                    id={`review-group-${item.review_group_id}`}
                    role="option"
                    aria-selected={isSelected}
                    data-review-group-id={item.review_group_id}
                    className={`review-group-card${isSelected ? ' is-active' : ''}`}
                  >
                    <span className="review-group-top">
                      <strong>{item.counterparty ?? 'Opération sans contrepartie validée'}</strong>
                      <Badge>{statusLabel(item.status)}</Badge>
                    </span>
                    <span className="review-group-total">
                      {item.total_out_of_range ? 'hors plage entière sûre' : formatSafeCents(item.amount_cents)}{' '}
                      <small>· {item.transaction_count} opération(s)</small>
                    </span>
                    <span className="review-group-foot">
                      <span>Impact {item.impact_cents === null || item.total_out_of_range ? 'hors plage' : formatSafeCents(item.impact_cents)}</span>
                      <time dateTime={item.latest_booked_on}>{formatDate(item.latest_booked_on)}</time>
                    </span>
                    {item.quality_flags.length > 0 && (
                      <span className="review-flags">
                        {item.quality_flags.map((flag) => (
                          <span key={flag} className={`review-flag${flag === 'ambiguous_merchant' ? ' is-ambiguous' : ''}`}>
                            {qualityFlagLabel(flag)}
                          </span>
                        ))}
                      </span>
                    )}
                  </li>
                )
              })}
            </ul>
            {nextCursor && (
              <div className="review-load-more">
                <Button onClick={() => void loadMore()} disabled={loadingMore}>
                  {loadingMore ? 'Chargement…' : 'Charger plus'}
                </Button>
              </div>
            )}
          </section>

          <section className="review-detail" aria-label="Détail du groupe" aria-busy={loadingDetail}>
            {!selectedGroupId && !loadingDetail && (
              <Card className="review-empty">
                <h2>Choisissez un groupe</h2>
                <p>Le détail des opérations, les propositions et l’historique apparaîtront ici.</p>
              </Card>
            )}
            {selectedGroupId && loadingDetail && !detail && (
              <div className="review-loading" role="status">
                <span className="spinner" aria-hidden="true" /> Chargement du détail…
              </div>
            )}
            {detailError && (
              <div className="review-error-state" role="alert">
                <p>{detailError}</p>
                <Button onClick={reloadDetail}>Recharger le détail</Button>
              </div>
            )}
            {actionError && (
              <div className="review-error-state" role="alert">
                <p>{actionError}</p>
                <Button onClick={() => setActionError(null)}>Fermer le message</Button>
              </div>
            )}
            {detail && (
              <>
                <Card className="review-detail-heading">
                  <div>
                    <p className="eyebrow">GROUPE · {detail.transaction_count} OPÉRATION(S)</p>
                    <h2 ref={headingRef} tabIndex={-1}>{selectedItem?.counterparty ?? 'Opération sans contrepartie validée'}</h2>
                    <p>
                      {statusLabel(detail.status)}
                      {groupTotal ? ` · total ${groupTotal}` : ''}
                      {selectedItem ? ` · dernière opération ${formatDate(selectedItem.latest_booked_on)}` : ''}
                    </p>
                  </div>
                  {flags.length > 0 && (
                    <ul className="review-quality-notes">
                      {flags.map((flag) => (
                        <li key={flag}>{qualityFlagLabel(flag)}</li>
                      ))}
                    </ul>
                  )}
                  {ambiguous && (
                    <p className="review-warning" role="note">
                      <strong>Contrepartie ambiguë.</strong> Amazon, Vinted, PayPal et assimilés recouvrent plusieurs vendeurs ou
                      usages : aucune règle globale n’est proposée, classez les opérations une par une ou par sélection.
                    </p>
                  )}
                </Card>

                <div className="review-selection-actions">
                  <label className="review-select-all">
                    <input
                      type="checkbox"
                      checked={allVisibleSelected}
                      onChange={(event) =>
                        setSelectedIds(event.target.checked ? transactions.slice(0, visibleCards).map((item) => item.transaction_id) : [])
                      }
                    />{' '}
                    Tout sélectionner
                  </label>
                  <span>
                    {chosen.length > 0 ? `${chosen.length} opération(s) sélectionnée(s)` : 'Cochez des opérations (Maj-clic pour une plage)'}
                  </span>
                  <Button
                    onClick={() => openEditor('classify', chosen[0] ?? null, null, 'selection')}
                    disabled={chosen.length < 2 || !categories || busy}
                  >
                    Classer la sélection
                  </Button>
                  {chosen.length > 0 && <Button onClick={() => setSelectedIds([])}>Effacer la sélection</Button>}
                </div>

                <div className="review-transactions">
                  {transactions.slice(0, visibleCards).map((transaction, position) => (
                    <TransactionCard
                      key={transaction.transaction_id}
                      transaction={transaction}
                      position={position}
                      selected={selectedIds.includes(transaction.transaction_id)}
                      active={transaction.transaction_id === activeTransaction?.transaction_id}
                      accounts={accounts}
                      categoryIndex={categoryIndex}
                      busy={busy}
                      onSelect={(shift) => toggleSelection(position, transaction, shift)}
                      onActivate={() => setActiveId(transaction.transaction_id)}
                      onConfirm={(proposal) => confirmProposal(transaction, proposal)}
                      onClassify={() => openEditor('classify', transaction)}
                      onSplit={() => openEditor('split', transaction)}
                      onIgnore={() => void markTransaction(transaction, 'ignore')}
                      onReview={() => void markTransaction(transaction, 'mark_for_review')}
                      onRefund={() => {
                        setActiveId(transaction.transaction_id)
                        setRefundError(null)
                        setRefundOpen(true)
                      }}
                      onUndo={() => void undoTransaction(transaction)}
                    />
                  ))}
                </div>
                {transactions.length > visibleCards && (
                  <div className="review-load-more">
                    <Button onClick={() => setVisibleCards((count) => count + CARD_PAGE)}>
                      Afficher plus d’opérations ({transactions.length - visibleCards} restantes)
                    </Button>
                  </div>
                )}

                <Card className="review-history">
                  <h3>Historique du groupe</h3>
                  {transactions.every((item) => item.history.length === 0) ? (
                    <p className="muted">Aucune décision enregistrée.</p>
                  ) : (
                    <ol>
                      {transactions
                        .flatMap((item) => item.history.map((entry) => ({ ...entry, bookedOn: item.booked_on })))
                        .sort((left, right) => left.created_at_utc.localeCompare(right.created_at_utc))
                        .map((entry) => (
                          <li key={entry.decision_id}>
                            <time dateTime={entry.created_at_utc}>{formatDateTime(entry.created_at_utc)}</time> · {actionLabel(entry.action)} ·{' '}
                            {entry.actor} · opération du {formatDate(entry.bookedOn)}
                            {entry.undone ? ' · annulée' : ''}
                          </li>
                        ))}
                    </ol>
                  )}
                </Card>
              </>
            )}
          </section>
        </div>
      )}

      <div className="review-footer-actions">
        <Button onClick={() => setHelpOpen(true)}>Raccourcis clavier (?)</Button>
        <Button onClick={() => { reloadQueue(); reloadDetail() }}>Actualiser la file</Button>
      </div>
      <p className="review-keyboard-hint">
        Clavier : <kbd>j</kbd>/<kbd>k</kbd> groupe suivant/précédent · <kbd>Entrée</kbd> confirmer la proposition · <kbd>c</kbd>{' '}
        classer · <kbd>s</kbd> séparer · <kbd>u</kbd> annuler · <kbd>?</kbd> aide. Suspendus dans les champs de saisie et les
        fenêtres.
      </p>
      <div className="visually-hidden" role="status" aria-live="polite" aria-atomic="true" data-testid="review-live">
        {announcement}
      </div>

      {editor && editorTransaction && categories && (
        <AllocationEditor
          key={`${editor.transactionId}-${editor.mode}-${editor.scope}`}
          transaction={editorTransaction}
          selection={chosen.some((item) => item.transaction_id === editorTransaction.transaction_id) ? chosen : []}
          group={selectedItem}
          categories={categories.items}
          dimensions={categories.dimensions}
          accounts={accounts}
          mode={editor.mode}
          seed={editor.seed}
          bankLabel={editorLabel.status === 'ready' ? editorLabel.text : null}
          initialScope={editor.scope}
          busy={busy}
          error={editorError}
          onClose={() => {
            setEditor(null)
            setEditorError(null)
            pendingKey.current = null
          }}
          onSubmit={submitDecision}
        />
      )}
      {refundOpen && transactions.length > 0 && (
        <RefundLinkDialog
          transactions={transactions}
          activeTransactionId={activeTransaction?.transaction_id ?? null}
          accounts={accounts}
          error={refundError}
          onClose={() => setRefundOpen(false)}
          onCreate={createRefund}
          onConfirm={confirmRefund}
        />
      )}
      {overrideTarget && (
        <ReviewDialog title="Remplacer une décision humaine ?" onClose={() => setOverrideTarget(null)} closeDisabled={busy}>
          <p>
            Une décision humaine plus récente existe sur cette opération. L’annuler la remplacera. Cette confirmation est
            enregistrée dans l’historique.
          </p>
          <div className="review-dialog-actions">
            <button className="button" type="button" onClick={() => setOverrideTarget(null)} disabled={busy}>Garder la décision actuelle</button>
            <button className="button button--primary" type="button" disabled={busy} onClick={() => void undoTransaction(overrideTarget, true)}>
              Annuler et remplacer
            </button>
          </div>
        </ReviewDialog>
      )}
      {helpOpen && <HelpDialog onClose={() => setHelpOpen(false)} />}
    </>
  )
}

class ReviewRouteBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  render() {
    if (this.state.failed) {
      return (
        <section className="page-error" role="alert">
          <h1>Cette page a rencontré un problème.</h1>
          <p>Rechargez la page pour reprendre la revue ; vos décisions déjà enregistrées sont conservées.</p>
          <Button onClick={() => window.location.reload()}>Recharger la page</Button>
        </section>
      )
    }
    return this.props.children
  }
}
