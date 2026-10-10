import { Component, useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError } from '../../lib/api/client'
import { getTransactions, getTransaction, type TransactionFilters } from '../../lib/api/transactions'
import { cancelImportPreview, commitImport, createImportPreview, getImportPreview, listImportRejections, listImports, resolveMultiplicity, updateAccountMapping, updateCoverage } from '../../lib/api/imports'
import { formatCentsToEuro, parseEuroInputToCents } from '../../lib/money'
import { formatDateFr } from '../../lib/dates'
import { Badge, Button, Card, EmptyState, ErrorState, PageHeader, Spinner } from '../../components/ui'
import type { ImportListItem, ImportPreview, OutOfPeriodCandidateGroup, PeriodCompleteness, TransactionDetail, TransactionItem, TransactionPage } from '../../types/transactions'

type Account = { account_id: string; alias: string; account_role: string; is_active: boolean }
type ImportDiagnostic = { code: string; message: string; resolution_status: 'pending' | 'resolved' | 'not_required' }
const codeMessages: Record<string, string> = {
  AMOUNT_OUT_OF_RANGE: 'Le montant dépasse la limite autorisée. Ce relevé ne peut pas être importé.',
  MISSING_ACCOUNT_NUMBER: 'Le numéro de compte manque dans le fichier. Demandez un nouvel export à la banque.',
  OUT_OF_PERIOD: 'Cette opération est en dehors de la période annoncée. Choisissez comment la rapprocher.',
  DUPLICATE_ACCOUNT_SECTION: 'Plusieurs sections désignent le même compte. Le fichier doit être clarifié.',
  SECTION_RANGE_REQUIRED: 'La période de couverture manque. Indiquez les dates couvertes par cette section.',
  PROVISIONAL_DAY: 'Journée provisoire : refaites un export qui la chevauche pour compléter la couverture.',
  ACCOUNT_MAPPING_REQUIRED: 'Associez cette section à un compte pour continuer.',
  LABEL_TOO_LONG: 'Un libellé dépasse la longueur acceptée. Ce relevé ne peut pas être importé.',
  ZERO_AMOUNT: 'Une opération sans montant a été détectée. Ce relevé ne peut pas être importé.',
  MULTIPLICITY_AMBIGUOUS: 'Choisissez si cette ligne est une nouvelle occurrence ou une source déjà connue.',
}

function humanError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Une erreur inattendue est survenue.'
  if (error.code === 'NETWORK_ERROR' || error.code === 'TIMEOUT') return 'Le serveur local ne répond pas. Vérifiez qu’il est démarré puis réessayez.'
  if (error.status === 503) return 'La base locale est occupée ou indisponible. Réessayez dans un instant.'
  if (error.code === 'TOO_MANY_OPERATIONS') return 'Un fichier peut contenir au plus 4 000 opérations. Réduisez-le en plusieurs relevés puis réessayez.'
  if (error.code === 'FILE_TOO_LARGE') return 'Le fichier dépasse la taille maximale autorisée.'
  if (error.status === 415) return 'Ce fichier n’est pas reconnu comme un CSV texte.'
  if (error.status === 410) return 'Cet aperçu a expiré. Sélectionnez à nouveau le fichier.'
  if (error.status === 409 && error.code === 'ACCOUNT_ALIAS_CONFLICT') return 'Cet alias de compte est déjà utilisé. Modifiez l’association puis réessayez.'
  if (error.status === 409 && error.code === 'ACCOUNT_FINGERPRINT_CONFLICT') return 'Ce compte a été associé entre-temps. Rechargez l’aperçu avant de continuer.'
  if (error.status === 409 && error.code === 'IMPORT_COMMIT_IN_PROGRESS') return 'Le commit de cet aperçu est en cours. Attendez sa fin avant de modifier l’aperçu.'
  if (error.status === 409 && error.code === 'VERSION_CONFLICT') return 'L’aperçu a changé. Son état le plus récent a été rechargé.'
  return error.message
}

function TransactionsPageContent() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [tab, setTab] = useState<'operations' | 'imports'>('operations')
  const [page, setPage] = useState<TransactionPage | null>(null)
  const [transactions, setTransactions] = useState<TransactionItem[]>([])
  const [detail, setDetail] = useState<TransactionDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [q, setQ] = useState('')
  const [minimumInput, setMinimumInput] = useState(() => centsParamToInput(searchParams.get('amount_min_cents')))
  const [maximumInput, setMaximumInput] = useState(() => centsParamToInput(searchParams.get('amount_max_cents')))
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [uploadMessage, setUploadMessage] = useState('')
  const [imports, setImports] = useState<ImportListItem[]>([])
  const [importMore, setImportMore] = useState<string | null>(null)
  const [importError, setImportError] = useState('')
  const [rejections, setRejections] = useState<Record<string, ImportDiagnostic[] | null>>({})
  const [accounts, setAccounts] = useState<Account[]>([])
  const [mapping, setMapping] = useState<Record<number, { alias: string; role: string }>>({})
  const [coverage, setCoverage] = useState<Record<number, { from: string; through: string }>>({})
  const pickerRef = useRef<HTMLInputElement>(null)
  const idempotencyKey = useRef<string | null>(null)
  const drawerRef = useRef<HTMLElement | null>(null)
  const previousDrawerFocus = useRef<HTMLElement | null>(null)

  const filters = useMemo(() => ({
    account_ids: searchParams.getAll('account_ids'),
    period_start: searchParams.get('period_start') ?? undefined,
    period_end: searchParams.get('period_end') ?? undefined,
    direction: (searchParams.get('direction') as 'debit' | 'credit' | null) ?? undefined,
    amount_min_cents: parseAmountParam(searchParams.get('amount_min_cents')),
    amount_max_cents: parseAmountParam(searchParams.get('amount_max_cents')),
    import_file_id: searchParams.get('import_file_id') ?? undefined,
    classification: (searchParams.get('classification') as 'all' | 'unclassified' | null) ?? 'all',
  }), [searchParams])
  const apiFilters = useMemo<TransactionFilters>(() => filters, [filters])
  const periodNotStarted = Boolean(filters.period_end && filters.period_end < '2026-01-01')

  const load = useCallback(async (cursor?: string | null, append = false) => {
    setLoading(true); setError('')
    try {
      const result = await getTransactions({ ...apiFilters, q: q || undefined }, cursor)
      const items = Array.isArray(result.items) ? result.items : []
      setPage({ ...result, items, summary: result.summary ?? { count: 0, debit_total_cents: 0, credit_total_cents: 0, net_cents: 0 }, total_count: result.total_count ?? 0 })
      setTransactions((current) => append ? [...current, ...items] : items)
    } catch (cause) { setError(humanError(cause)) }
    finally { setLoading(false) }
  }, [apiFilters, q])
  useEffect(() => { void load() }, [load]) // URL-backed filters reload the first page.

  useEffect(() => {
    void fetch('/api/v1/accounts').then((response) => response.ok ? response.json() : null)
      .then((data: { items?: Account[] } | null) => setAccounts((data?.items ?? []).filter((item) => item.is_active)))
      .catch(() => setAccounts([]))
  }, [])

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value); else next.delete(key)
    setSearchParams(next, { replace: true })
  }
  const handleUpload = async (file?: File) => {
    if (!file) return
    setBusy(true); setPreview(null); setUploadMessage('')
    try { const result = await createImportPreview(file); setPreview(result); setMapping({}); setCoverage({}); idempotencyKey.current = null }
    catch (cause) { setUploadMessage(humanError(cause)) }
    finally { setBusy(false) }
  }
  const withVersion = async (operation: (p: ImportPreview) => Promise<ImportPreview>) => {
    if (!preview) return
    setBusy(true); setUploadMessage('')
    try { setPreview(await operation(preview)) }
    catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.code === 'VERSION_CONFLICT') {
        try { setPreview(await getImportPreview(preview.preview_id)); setUploadMessage(humanError(cause)) }
        catch (reloadError) { setUploadMessage(humanError(reloadError)) }
      } else setUploadMessage(humanError(cause))
    } finally { setBusy(false) }
  }
  const performCommit = async () => {
    if (!preview?.can_commit || !preview.preview_id) return
    setBusy(true); setUploadMessage('')
    idempotencyKey.current ??= crypto.randomUUID()
    try {
      const result = await commitImport(preview.preview_id, preview.version, idempotencyKey.current)
      const resultSections = Array.isArray(result.sections) ? result.sections as Array<{ coverage_from?: string; coverage_through?: string }> : []
      const ranges = resultSections.map((section) => section.coverage_from && section.coverage_through ? `${formatDateFr(section.coverage_from)}–${formatDateFr(section.coverage_through)}` : '').filter(Boolean)
      setUploadMessage(result.status === 'no_op'
        ? 'Ce fichier avait déjà été importé : 0 opération et 0 provenance supplémentaire (no_op).'
        : `Import terminé : ${String(result.created_transaction_count ?? 0)} opération(s) créée(s), ${String(result.new_source_row_count ?? 0)} provenance(s) ajoutée(s)${ranges.length ? ` · couverture ${ranges.join(', ')}` : ''}.`)
      setPreview(null); idempotencyKey.current = null
      await load()
    } catch (cause) { setUploadMessage(humanError(cause)) }
    finally { setBusy(false) }
  }
  const cancelPreview = async () => {
    if (!preview) return
    setBusy(true)
    try { await cancelImportPreview(preview.preview_id); setPreview(null); setUploadMessage('Aperçu annulé.') }
    catch (cause) { setUploadMessage(humanError(cause)) }
    finally { setBusy(false) }
  }
  const loadHistory = async (cursor?: string | null, append = false) => {
    setImportError('')
    try {
      const data = await listImports(cursor)
      setImports((current) => append ? [...current, ...data.items] : data.items)
      setImportMore(data.page.next_cursor)
    } catch (cause) { setImportError(humanError(cause)) }
  }
  useEffect(() => { if (tab === 'imports') void loadHistory() }, [tab])
  const showRejections = async (id: string) => {
    if (rejections[id]) { setRejections((current) => { const copy = { ...current }; delete copy[id]; return copy }); return }
    try {
      const data = await listImportRejections(id)
      setRejections((current) => ({ ...current, [id]: data.items as unknown as ImportDiagnostic[] }))
    }
    catch (cause) { setImportError(humanError(cause)) }
  }
  const openDetail = async (row: TransactionItem) => {
    previousDrawerFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    try { setDetail(await getTransaction(row.id)) }
    catch (cause) { setError(humanError(cause)) }
  }
  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, current: 'operations' | 'imports') => {
    const next = event.key === 'ArrowRight' || event.key === 'Home'
      ? (event.key === 'Home' ? 'operations' : current === 'operations' ? 'imports' : 'operations')
      : event.key === 'ArrowLeft' || event.key === 'End'
        ? (event.key === 'End' ? 'imports' : current === 'imports' ? 'operations' : 'imports')
        : null
    if (next) {
      event.preventDefault()
      setTab(next)
      document.getElementById(`transactions-tab-${next}`)?.focus()
    }
  }
  const handleDrawerKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      setDetail(null)
      return
    }
    if (event.key !== 'Tab') return
    const drawer = drawerRef.current
    const focusable = drawer?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
    if (!focusable?.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (!first || !last) return
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }, [])

  useEffect(() => {
    if (!detail) return
    const drawer = drawerRef.current
    drawer?.querySelector<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')?.focus()
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      handleDrawerKeyDown(event as unknown as KeyboardEvent<HTMLElement>)
    }
    drawer?.addEventListener('keydown', onKeyDown)
    return () => {
      drawer?.removeEventListener('keydown', onKeyDown)
      previousDrawerFocus.current?.focus()
      previousDrawerFocus.current = null
    }
  }, [detail, handleDrawerKeyDown])

  return <>
    <PageHeader eyebrow="JOURNAL" title="Opérations" description="Consultez les lignes brutes importées, leur provenance et leur état de ventilation." />
    <div className="transaction-tabs" role="tablist" aria-label="Opérations et imports">
      <button id="transactions-tab-operations" role="tab" aria-selected={tab === 'operations'} aria-controls="transactions-panel-operations" tabIndex={tab === 'operations' ? 0 : -1} onKeyDown={(event) => handleTabKeyDown(event, 'operations')} onClick={() => setTab('operations')}>Opérations</button>
      <button id="transactions-tab-imports" role="tab" aria-selected={tab === 'imports'} aria-controls="transactions-panel-imports" tabIndex={tab === 'imports' ? 0 : -1} onKeyDown={(event) => handleTabKeyDown(event, 'imports')} onClick={() => setTab('imports')}>Imports</button>
    </div>
    {tab === 'operations' ? <section id="transactions-panel-operations" role="tabpanel" aria-labelledby="transactions-tab-operations" tabIndex={0}>
      <Card className="transaction-filters">
        <label>Du<input type="date" value={filters.period_start ?? ''} onChange={(event) => setFilter('period_start', event.target.value)} /></label>
        <label>Au<input type="date" value={filters.period_end ?? ''} onChange={(event) => setFilter('period_end', event.target.value)} /></label>
        <label>Compte<select aria-label="Compte" value={filters.account_ids[0] ?? ''} onChange={(event) => { const next = new URLSearchParams(searchParams); next.delete('account_ids'); if (event.target.value) next.set('account_ids', event.target.value); setSearchParams(next, { replace: true }) }}><option value="">Tous les comptes</option>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.alias}</option>)}</select></label>
        <label>Sens<select value={filters.direction ?? ''} onChange={(event) => setFilter('direction', event.target.value)}><option value="">Tous</option><option value="debit">Débits</option><option value="credit">Crédits</option></select></label>
        <label>Montant minimum<input inputMode="decimal" value={minimumInput} onChange={(event) => setMinimumInput(event.target.value)} onBlur={() => updateAmountFilter('amount_min_cents', minimumInput, setFilter)} placeholder="0,00" /></label>
        <label>Montant maximum<input inputMode="decimal" value={maximumInput} onChange={(event) => setMaximumInput(event.target.value)} onBlur={() => updateAmountFilter('amount_max_cents', maximumInput, setFilter)} placeholder="0,00" /></label>
        <label>Import d’origine<input value={filters.import_file_id ?? ''} onChange={(event) => setFilter('import_file_id', event.target.value)} placeholder="Identifiant d’import" /></label>
        <label>Recherche du libellé<input value={q} onChange={(event) => setQ(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void load() }} placeholder="Saisir des mots…" /></label>
        <label>Ventilation<select value={filters.classification ?? 'all'} onChange={(event) => setFilter('classification', event.target.value)}><option value="all">Toutes</option><option value="unclassified">Non ventilées</option></select></label>
        <Button variant="primary" onClick={() => void load()}>Appliquer</Button>
      </Card>
      {periodNotStarted && <p className="muted" role="status">{periodCompletenessLabel('not_started')} : aucune alerte de couverture pour cette période.</p>}
      <p className="classification-note"><strong>Non ventilées :</strong> les opérations affichées restent au montant bancaire brut. Leur ventilation sera proposée dans la future Revue.</p>
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {page && <Card className="transaction-summary" aria-live="polite"><strong>{page.summary.count} opérations</strong><span>Débits {formatNullableCents(page.summary.debit_total_cents)}</span><span>Crédits {formatNullableCents(page.summary.credit_total_cents)}</span><span>Net {formatNullableCents(page.summary.net_cents)}</span>{page.summary.total_out_of_range && <span role="status">Un total dépasse la plage entière sûre et n’est pas affiché.</span>}<span className="muted">Filtre complet · {transactions.length} affichée(s) sur {page.total_count}</span></Card>}
      {loading && <div className="transaction-loading"><Spinner label="Chargement des opérations" /> Chargement des opérations…</div>}
      {!loading && transactions.length === 0 && !error && <EmptyState title="Aucune opération" description="Aucune ligne ne correspond à ces filtres. Vous pouvez charger un relevé depuis l’onglet Imports." action={false} />}
      {transactions.length > 0 && <div className="transaction-table-wrap"><table className="transaction-table"><caption className="visually-hidden">Opérations bancaires brutes</caption><thead><tr><th scope="col">Date <span aria-hidden="true">↓</span></th><th scope="col">Compte</th><th scope="col">Libellé</th><th scope="col" className="numeric">Débit</th><th scope="col" className="numeric">Crédit</th><th scope="col">État</th><th scope="col" className="numeric">Sources</th></tr></thead><tbody>{transactions.map((row) => <tr key={row.id}><td><button className="transaction-open" onClick={() => void openDetail(row)} aria-label={`Détail opération du ${formatDateFr(row.booked_on)}`}>{formatDateFr(row.booked_on)}</button></td><td>{row.account_alias}</td><td className="label-cell">{row.raw_label_preview}</td><td className="numeric">{row.amount_cents === null ? 'hors plage entière sûre' : row.amount_cents < 0 ? formatCentsToEuro(row.amount_cents) : '—'}</td><td className="numeric">{row.amount_cents === null ? 'hors plage entière sûre' : row.amount_cents > 0 ? formatCentsToEuro(row.amount_cents) : '—'}</td><td><Badge>Non ventilée</Badge></td><td className="numeric">{row.import_count}</td></tr>)}</tbody></table></div>}
      {page?.next_cursor && <div className="load-more"><Button onClick={() => void load(page.next_cursor, true)} disabled={loading}>Charger plus</Button><span>{transactions.length} sur {page.total_count} opérations</span></div>}
      {detail && <div className="drawer-backdrop"><button className="drawer-scrim" aria-label="Fermer le détail" onClick={() => setDetail(null)} /><section ref={drawerRef} className="transaction-drawer" role="dialog" tabIndex={-1} aria-modal="true" aria-labelledby="detail-title"><Button className="drawer-close" aria-label="Fermer le détail" onClick={() => setDetail(null)}>Fermer</Button><h2 id="detail-title">Détail de l’opération</h2><p>{formatDateFr(detail.booked_on)} · {detail.account_alias}</p><Badge>Non ventilée</Badge><h3>Libellé brut</h3><pre>{detail.raw_label}</pre><dl><dt>Libellé d’identité</dt><dd>{detail.identity_label}</dd><dt>Rang d’occurrence</dt><dd>{detail.occurrence_rank}</dd><dt>État</dt><dd>Non ventilée</dd></dl><h3>Sources d’import ({detail.sources.length})</h3><ul>{detail.sources.map((source, index) => <li key={`${source.import_file_id}-${index}`}>Import du {formatDateFr(source.imported_at.slice(0, 10))} · section {source.section} · ligne {source.logical_row_idx}</li>)}</ul></section></div>}
    </section> : <section id="transactions-panel-imports" role="tabpanel" aria-labelledby="transactions-tab-imports" tabIndex={0} className="imports-panel">
      <Card className="upload-card" onDragOver={(event) => event.preventDefault()} onDrop={(event: DragEvent<HTMLElement>) => { event.preventDefault(); void handleUpload(event.dataTransfer.files[0]) }}>
        <h2>Importer un relevé CSV</h2><p>Le fichier est analysé localement avant toute écriture. Rien n’est ajouté tant que vous n’avez pas confirmé l’import. Maximum : 4 000 opérations par fichier.</p>
        <input ref={pickerRef} type="file" aria-label="Choisir un fichier CSV à analyser" accept=".csv,text/csv" className="visually-hidden" onChange={(event: ChangeEvent<HTMLInputElement>) => void handleUpload(event.target.files?.[0])} />
        <Button variant="primary" disabled={busy} onClick={() => pickerRef.current?.click()}>{busy ? 'Analyse en cours…' : 'Choisir un fichier CSV'}</Button><span className="drop-hint">ou déposez le fichier ici</span>
      </Card>
      {busy && !preview && <p role="status"><Spinner label="Analyse en cours" /> Analyse du fichier en cours…</p>}
      {uploadMessage && <p className="notice" role="status">{uploadMessage}</p>}
      {preview && <ImportPreviewView preview={preview} accounts={accounts} mapping={mapping} setMapping={setMapping} coverage={coverage} setCoverage={setCoverage} busy={busy} onMapping={(section, body) => withVersion((p) => updateAccountMapping(p.preview_id, section, { expected_version: p.version, ...body }))} onCoverage={(section) => withVersion((p) => updateCoverage(p.preview_id, section, { expected_version: p.version, coverage_from: coverage[section]?.from, coverage_through: coverage[section]?.through }))} onMultiplicity={(row, action, transactionId, sectionIndex) => withVersion((p) => resolveMultiplicity(p.preview_id, row, { expected_version: p.version, action, ...(transactionId ? { transaction_id: transactionId } : {}), ...(sectionIndex ? { section_index: sectionIndex } : {}) }))} onCommit={() => void performCommit()} onCancel={() => void cancelPreview()} />}
      <div className="history-heading"><h2>Historique des imports</h2><Button onClick={() => void loadHistory()}>Actualiser</Button></div>
      {importError && <ErrorState message={importError} onRetry={() => void loadHistory()} />}
      {imports.length === 0 && !importError ? <p className="muted">Aucun import enregistré.</p> : <ul className="import-history">{imports.map((item) => <li key={item.import_file_id}><div><strong>{formatDateFr(item.created_at_utc.slice(0, 10))}</strong><Badge>{statusLabel(item.status)}</Badge><span>{item.operation_count} opération(s) · {item.created_transaction_count} créée(s) · {item.source_row_count} source(s)</span><small>Empreinte {item.file_sha256.slice(0, 12)}…</small></div><Button onClick={() => void showRejections(item.import_file_id)}>{Object.hasOwn(rejections, item.import_file_id) ? 'Masquer les rejets et diagnostics' : 'Afficher les rejets et diagnostics'}</Button>{Object.hasOwn(rejections, item.import_file_id) && (rejections[item.import_file_id]?.length ? rejections[item.import_file_id]?.map((line, i) => <p key={i}><strong>{line.resolution_status === 'resolved' ? 'Résolu' : line.resolution_status === 'pending' ? 'À traiter' : 'Information'}</strong> · {line.code} · {line.message}</p>) : <p>Aucun rejet ni diagnostic enregistré pour cet import.</p>)}</li>)}</ul>}
      {importMore && <Button onClick={() => void loadHistory(importMore, true)}>Charger plus d’imports</Button>}
    </section>}
  </>
}

export class TransactionsRouteBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (this.state.failed) {
      return <section className="page-error" role="alert"><h1>Cette page a rencontré un problème.</h1><p>Rechargez la page pour reprendre la consultation.</p><Button onClick={() => window.location.reload()}>Recharger la page</Button></section>
    }
    return this.props.children
  }
}

export function TransactionsRouteErrorElement() {
  return <section className="page-error" role="alert"><h1>Cette page a rencontré un problème.</h1><p>Rechargez la page pour reprendre la consultation.</p><Button onClick={() => window.location.reload()}>Recharger la page</Button></section>
}

export function Transactions() {
  return <TransactionsRouteBoundary><TransactionsPageContent /></TransactionsRouteBoundary>
}

export function formatNullableCents(value: number | null): string {
  return value === null || !Number.isSafeInteger(value) ? 'hors plage entière sûre' : formatCentsToEuro(value)
}

export function periodCompletenessLabel(value: PeriodCompleteness): string {
  if (value === 'not_started') return 'Avant le premier import'
  if (value === 'partial') return 'Couverture partielle'
  if (value === 'unknown') return 'Couverture inconnue'
  return 'Couverture complète'
}

function parseAmountParam(value: string | null): number | undefined { if (!value || !/^\d+$/.test(value)) return undefined; const amount = Number(value); return Number.isSafeInteger(amount) ? amount : undefined }
function centsParamToInput(value: string | null): string {
  if (!value || !/^\d+$/.test(value)) return ''
  const cents = BigInt(value)
  return `${cents / 100n},${(cents % 100n).toString().padStart(2, '0')}`
}
function updateAmountFilter(key: string, value: string, setFilter: (key: string, value: string) => void) {
  const cents = parseEuroInputToCents(value)
  setFilter(key, cents === null || cents < 0n || cents > BigInt(Number.MAX_SAFE_INTEGER) ? '' : cents.toString())
}
function statusLabel(status: ImportListItem['status']) { return ({ committed: 'Importé', no_op: 'Déjà importé · no_op', rejected: 'Rejeté', previewed: 'Aperçu' })[status] }

function ImportPreviewView(props: {
  preview: ImportPreview; accounts: Account[]; mapping: Record<number, { alias: string; role: string }>; setMapping: (value: Record<number, { alias: string; role: string }>) => void
  coverage: Record<number, { from: string; through: string }>; setCoverage: (value: Record<number, { from: string; through: string }>) => void; busy: boolean
  onMapping: (section: number, body: Record<string, unknown>) => void; onCoverage: (section: number) => void
  onMultiplicity: (row: number, action: 'create_occurrence' | 'link_existing', transactionId?: string, sectionIndex?: number) => void; onCommit: () => void; onCancel: () => void
}) {
  const { preview } = props
  const [multiplicityChoices, setMultiplicityChoices] = useState<Record<string, string>>({})
  const [createConfirmed, setCreateConfirmed] = useState<Record<string, boolean>>({})
  const blockers = preview.rejections.filter((item) => item.severity === 'blocking' || item.code !== 'PROVISIONAL_DAY')
  const mapAccount = (section: number, target: string) => {
    if (!target.startsWith('new:')) { props.onMapping(section, { account_id: target }); return }
    const value = props.mapping[section]
    if (value?.alias.trim()) props.onMapping(section, { new_account: { alias: value.alias.trim(), account_role: value.role } })
    else document.getElementById(`new-account-alias-${section}`)?.focus()
  }
  return <Card className="preview-card" aria-label="Aperçu du relevé">
    <div className="preview-heading"><div><p className="eyebrow">APERÇU · VERSION {preview.version}</p><h2>Vérifiez avant d’importer</h2></div>{preview.already_imported && <Badge>Déjà importé</Badge>}</div>
    <p>{preview.operation_count} opération(s) · généré le {formatDateFr(preview.generated_on)}{preview.already_imported ? ' · ce fichier existe déjà dans l’historique.' : ''}</p>
    {preview.sections.map((section) => <section className="preview-section" key={section.section_index}><h3>Section {section.section_index} · {section.account_alias ?? (section.account_resolution === 'known' ? 'Compte reconnu' : 'Compte à associer')}</h3><p>Période déclarée : {section.period_from ? formatDateFr(section.period_from) : 'inconnue'} – {section.period_to ? formatDateFr(section.period_to) : 'inconnue'} · {section.operation_count} opérations</p><p>Débits {formatNullableCents(section.debit_total_cents)} · Crédits {formatNullableCents(section.credit_total_cents)} · Net {formatNullableCents(section.net_cents)}</p>{section.total_out_of_range && <p role="status">Un total de cette section dépasse la plage entière sûre et n’est pas affiché.</p>}
      {section.provisional_days.map((day) => <p className="warning-text" key={day}>Journée du {formatDateFr(day)} incomplète : refaites un export qui la chevauche.</p>)}
      {section.account_resolution !== 'known' && <div className="resolution-box"><label>Associer à un compte<select value="" onChange={(event) => mapAccount(section.section_index, event.target.value)}><option value="">Choisir un compte ou créer…</option>{props.accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.alias}</option>)}<option value="new:create">Créer un compte après résolution</option></select></label><div className="new-account-fields"><label>Alias<input id={`new-account-alias-${section.section_index}`} value={props.mapping[section.section_index]?.alias ?? ''} onChange={(event) => props.setMapping({ ...props.mapping, [section.section_index]: { alias: event.target.value, role: props.mapping[section.section_index]?.role ?? 'current' } })} /></label><label>Rôle<select value={props.mapping[section.section_index]?.role ?? 'current'} onChange={(event) => props.setMapping({ ...props.mapping, [section.section_index]: { alias: props.mapping[section.section_index]?.alias ?? '', role: event.target.value } })}><option value="current">Courant</option><option value="savings">Épargne</option><option value="pea">PEA</option><option value="personal">Personnel</option><option value="cash">Espèces</option><option value="other">Autre</option></select></label><Button onClick={() => props.onMapping(section.section_index, { new_account: { alias: props.mapping[section.section_index]?.alias ?? '', account_role: props.mapping[section.section_index]?.role ?? 'current' } })}>Créer et associer</Button></div></div>}
    </section>)}
    {preview.warnings.length + preview.rejections.length > 0 && <div className="anomaly-list"><h3>Anomalies et avertissements</h3>{[...preview.rejections, ...preview.warnings].map((item, index) => <p key={`${item.code}-${index}`} className={item.severity === 'blocking' ? 'warning-text' : 'muted'}><strong>{item.code}</strong> · {codeMessages[item.code] ?? item.message}</p>)}</div>}
    {preview.rejections.filter((item) => item.code === 'SECTION_RANGE_REQUIRED' && item.section).map((item) => { const section = item.section!; return <div className="resolution-box" key={`coverage-${section}`}><strong>Définir la couverture de la section {section}</strong><label>Du<input type="date" value={props.coverage[section]?.from ?? ''} onChange={(event) => props.setCoverage({ ...props.coverage, [section]: { from: event.target.value, through: props.coverage[section]?.through ?? '' } })} /></label><label>Au<input type="date" value={props.coverage[section]?.through ?? ''} onChange={(event) => props.setCoverage({ ...props.coverage, [section]: { from: props.coverage[section]?.from ?? '', through: event.target.value } })} /></label><Button onClick={() => props.onCoverage(section)}>Enregistrer la couverture</Button></div> })}
    {preview.rejections.filter((item) => item.code === 'OUT_OF_PERIOD' && item.logical_row_idx).map((item, index) => {
      const row = item.logical_row_idx!
      const section = item.section ?? 0
      const key = `${section}:${row}`
      const candidates = preview.out_of_period_candidates.find((group: OutOfPeriodCandidateGroup) => group.section_index === section && group.logical_row_idx === row)?.candidates ?? []
      const defaultChoice = candidates[0] ? `link:${candidates[0].transaction_id}` : 'create'
      const storedChoice = multiplicityChoices[key]
      const choice = storedChoice === 'create' || candidates.some((candidate) => storedChoice === `link:${candidate.transaction_id}`) ? storedChoice : defaultChoice
      const selectedCandidate = candidates.find((candidate) => choice === `link:${candidate.transaction_id}`)
      const needsConfirmation = choice === 'create' && candidates.length > 0
      return <div className="resolution-box" key={`multiplicity-${key}-${index}`}><strong>Ligne {row} hors période · section {item.section ?? 'inconnue'}</strong><p>Créer une occurrence crée toujours une nouvelle opération (rang n+1).</p><label>Décision<select aria-label={`Décision ligne ${row}, section ${item.section ?? 'inconnue'}`} value={choice} onChange={(event) => setMultiplicityChoices({ ...multiplicityChoices, [key]: event.target.value })}>{candidates.map((candidate) => <option key={candidate.transaction_id} value={`link:${candidate.transaction_id}`}>Lier à l’opération existante (rang {candidate.occurrence_rank}) · import du {formatDateFr(candidate.imported_at_utc.slice(0, 10))}</option>)}<option value="create">Créer une nouvelle opération (rang n+1)</option></select></label>{needsConfirmation && <label><input type="checkbox" checked={createConfirmed[key] ?? false} onChange={(event) => setCreateConfirmed({ ...createConfirmed, [key]: event.target.checked })} /> Je confirme la création d’une nouvelle opération pour cette identité déjà importée.</label>}<Button disabled={needsConfirmation && !createConfirmed[key]} onClick={() => selectedCandidate ? props.onMultiplicity(row, 'link_existing', selectedCandidate.transaction_id, item.section ?? undefined) : props.onMultiplicity(row, 'create_occurrence', undefined, item.section ?? undefined)}>{selectedCandidate ? `Lier à l’opération existante (rang ${selectedCandidate.occurrence_rank})` : 'Créer une nouvelle opération (rang n+1)'}</Button></div>
    })}
    {blockers.length > 0 && <p className="blocking-note">Import bloqué : {blockers.map((item) => codeMessages[item.code] ?? item.message).join(' ')}</p>}
    <div className="preview-actions"><Button onClick={props.onCancel} disabled={props.busy}>Annuler</Button><Button variant="primary" onClick={props.onCommit} disabled={!preview.can_commit || props.busy}>{props.busy ? 'Traitement…' : preview.already_imported ? 'Confirmer le no_op' : 'Importer'}</Button></div>
  </Card>
}
