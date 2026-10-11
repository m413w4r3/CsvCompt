import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../src/lib/api/client'
import { reviewErrorMessage } from '../../src/lib/api/review'
import { parseEuroInputToCents } from '../../src/lib/money'
import { Review } from '../../src/pages/Review/index'
import {
  decisionCalls,
  defaultTransactions,
  installFakeApi,
  PAYEE_MARKET,
  tid,
  undoCalls,
  type FakeApiOptions,
} from './reviewFakeApi'

function Probe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}{location.search}</output>
}

function renderReview(options: FakeApiOptions = {}, path = '/review') {
  const api = installFakeApi(options)
  vi.stubGlobal('fetch', api.fetchMock)
  const view = render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/review" element={<><Review /><Probe /></>} />
      </Routes>
    </MemoryRouter>,
  )
  return { ...api, ...view }
}

const money = (value: string) => value.replace(/[\u00a0\u202f]/g, ' ')

async function openMarketGroup() {
  const list = await screen.findByRole('listbox')
  const option = await within(list).findByRole('option', { name: /Marché synthétique/ })
  return { list, option }
}

beforeEach(() => {
  if (!globalThis.crypto?.randomUUID) {
    vi.stubGlobal('crypto', { randomUUID: () => `00000000-0000-4000-8000-${String(Math.floor(Math.random() * 1e12)).padStart(12, '0')}` })
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('UI-REVIEW-01 · file groupée par contrepartie', () => {
  it('affiche contrepartie, nombre et euros, trie par impact et sélectionne le premier groupe', async () => {
    renderReview()
    const list = await screen.findByRole('listbox')
    const options = await within(list).findAllByRole('option')
    expect(options).toHaveLength(3)
    // Impact décroissant : Amazon 120,00 € > Marché 42,33 € > occurrence isolée 3,00 €.
    expect(options[0]).toHaveTextContent('Amazon')
    expect(options[1]).toHaveTextContent('Marché synthétique')
    expect(options[1]).toHaveTextContent('3 opération(s)')
    expect(money(options[1]!.textContent ?? '')).toContain('−42,33 €')
    expect(options[2]).toHaveTextContent('Opération sans contrepartie validée')
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('heading', { level: 2, name: 'Amazon' })).toBeInTheDocument()
  })

  it('affiche un état de chargement, puis un état vide avec lien d’import', async () => {
    renderReview({ txs: [] })
    expect(screen.getByText(/Chargement de la file de revue/)).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'La file de revue est vide' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Importer un relevé/ })).toHaveAttribute('href', '/transactions')
  })

  it('affiche une erreur actionnable (503) et recharge la file', async () => {
    const { calls } = renderReview({ failures: [{ match: (call) => call.path === '/api/v1/review/queue', status: 503, code: 'DATABASE_BUSY' }] })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('La base locale est occupée')
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Recharger la file' }))
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    expect(calls.filter((call) => call.path === '/api/v1/review/queue').length).toBeGreaterThanOrEqual(2)
  })

  it('charge la suite avec le curseur et conserve les groupes déjà affichés', async () => {
    const { calls } = renderReview({ pageSize: 2 })
    const list = await screen.findByRole('listbox')
    expect(await within(list).findAllByRole('option')).toHaveLength(2)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Charger plus' }))
    await waitFor(() => expect(within(screen.getByRole('listbox')).getAllByRole('option')).toHaveLength(3))
    const queueCalls = calls.filter((call) => call.path === '/api/v1/review/queue')
    expect(queueCalls.at(-1)!.search).toContain('cursor=')
    expect(screen.queryByRole('button', { name: 'Charger plus' })).not.toBeInTheDocument()
  })
})

describe('UI-REVIEW-02 · filtres dans l’URL, sans libellé', () => {
  it('écrit les filtres en paramètres et ne laisse jamais q ni libellé dans l’URL', async () => {
    const { calls } = renderReview({}, '/review?q=SECRET-LIBELLE&status=unclassified')
    await screen.findByRole('listbox')
    const location = screen.getByTestId('location').textContent ?? ''
    expect(location).not.toContain('q=')
    expect(location).not.toContain('SECRET')
    expect(location).toContain('period_start=2026-01-01')
    expect(location).toContain('period_end=2026-12-31')
    const queue = calls.filter((call) => call.path === '/api/v1/review/queue')[0]!
    expect(queue.search).not.toContain('SECRET')
    expect(queue.search).toContain('status=unclassified')
    await userEvent.setup().selectOptions(screen.getByLabelText('Sens'), 'debit')
    await waitFor(() => expect(screen.getByTestId('location').textContent).toContain('direction=debit'))
  })

  it('refuse une période inversée sans interroger le serveur', async () => {
    const { calls } = renderReview({}, '/review?period_start=2026-06-01&period_end=2026-01-01')
    expect(await screen.findByRole('alert')).toHaveTextContent('La date de fin précède la date de début')
    expect(calls.filter((call) => call.path === '/api/v1/review/queue')).toHaveLength(0)
  })
})

describe('RULE-05 · correction d’une seule occurrence', () => {
  it('RULE-05 classe une opération sans toucher aux voisines du groupe', async () => {
    const user = userEvent.setup()
    const { calls, txs } = renderReview()
    await openMarketGroup()
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    const second = await screen.findByRole('article', { name: /Opération 2/ })
    await user.click(within(second).getByRole('button', { name: /^Classer/ }))

    const dialog = await screen.findByRole('dialog', { name: 'Classer cette opération' })
    // La portée n'est jamais préchoisie sur « futures correspondances ».
    expect(within(dialog).getByRole('radio', { name: /Cette opération/ })).toBeChecked()
    expect(within(dialog).getByRole('radio', { name: /Futures correspondances/ })).not.toBeChecked()
    await user.selectOptions(within(dialog).getByLabelText('Catégorie'), 'groceries')
    await user.selectOptions(within(dialog).getByLabelText('Sous-catégorie'), 'groceries_supermarket')
    expect(within(dialog).getByLabelText('Flux')).toHaveValue('expense')
    await user.click(within(dialog).getByRole('button', { name: 'Classer' }))

    await waitFor(() => expect(decisionCalls(calls)).toHaveLength(1))
    const call = decisionCalls(calls)[0]!
    expect(call.body).toMatchObject({
      transaction_id: tid(2),
      expected_version: 1,
      scope: 'this_transaction',
      action: 'classify',
      actor: 'utilisateur_local',
      allocations: [{ category_id: 'groceries_supermarket', flow_kind: 'expense', amount_cents: -2000 }],
    })
    expect((call.body as { selection?: unknown }).selection).toBeUndefined()
    expect(call.headers['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    // Les voisines restent strictement inchangées (version, allocations).
    expect(txs.find((tx) => tx.transaction_id === tid(1))).toMatchObject({ version: 1, allocations: [] })
    expect(txs.find((tx) => tx.transaction_id === tid(3))).toMatchObject({ version: 1, allocations: [] })
    expect(txs.find((tx) => tx.transaction_id === tid(2))).toMatchObject({ version: 2 })
    expect(await screen.findByText(/Décision enregistrée/)).toBeInTheDocument()
  })
})

describe('RULE-06 · séparer avec clavier, somme exacte', () => {
  it('RULE-06 refuse tant que la somme diffère du total, affiche le reste, puis envoie des centimes entiers', async () => {
    const user = userEvent.setup()
    const { calls } = renderReview()
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    const first = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(first).getByRole('button', { name: /^Séparer/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Séparer cette opération' })
    const submit = within(dialog).getByRole('button', { name: 'Enregistrer la séparation' })
    expect(submit).toBeDisabled()

    const amounts = within(dialog).getAllByLabelText(/Montant/)
    expect(amounts).toHaveLength(2)
    const selects = within(dialog).getAllByLabelText('Catégorie')
    await user.selectOptions(selects[0]!, 'groceries')
    await user.selectOptions(selects[1]!, 'health')
    await user.type(amounts[0]!, '0,10')
    await user.type(amounts[1]!, '0,20')
    const remainder = within(dialog).getByTestId('split-remainder')
    expect(remainder).toHaveAttribute('aria-live', 'polite')
    // Total 12,34 € ; 0,10 + 0,20 = 0,30 exactement (aucun flottant) ; reste 12,04 €.
    expect(money(remainder.textContent ?? '')).toContain('Il reste 12,04 €')
    expect(money(within(dialog).getByTestId('split-allocated').textContent ?? '')).toBe('0,30 €')
    expect(submit).toBeDisabled()

    await user.clear(amounts[1]!)
    await user.type(amounts[1]!, '12,25')
    expect(money(remainder.textContent ?? '')).toContain('Dépassement de 0,01 €')
    expect(submit).toBeDisabled()

    await user.clear(amounts[1]!)
    await user.type(amounts[1]!, '12,24')
    expect(remainder).toHaveTextContent('Somme exacte')
    expect(submit).toBeEnabled()
    await user.click(submit)

    await waitFor(() => expect(decisionCalls(calls)).toHaveLength(1))
    const body = decisionCalls(calls)[0]!.body as { allocations: Array<{ amount_cents: number; category_id: string }> }
    expect(body.allocations.map((part) => part.amount_cents)).toEqual([-10, -1224])
    expect(body.allocations.reduce((sum, part) => sum + part.amount_cents, 0)).toBe(-1234)
    expect(body.allocations.every((part) => Number.isInteger(part.amount_cents))).toBe(true)
  })

  it('RULE-06 permet d’attribuer le reste à une part au clavier', async () => {
    const user = userEvent.setup()
    renderReview()
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    const first = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(first).getByRole('button', { name: /^Séparer/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Séparer cette opération' })
    const amounts = within(dialog).getAllByLabelText(/Montant/)
    await user.type(amounts[0]!, '5')
    await user.click(within(dialog).getAllByRole('button', { name: 'Attribuer le reste à cette part' })[1]!)
    expect(amounts[1]).toHaveValue('7,34')
    expect(within(dialog).getByTestId('split-remainder')).toHaveTextContent('Somme exacte')
  })

  it('UI-REVIEW-03 parse les euros en centimes entiers sans flottant', () => {
    expect(parseEuroInputToCents('0,10')).toBe(10n)
    expect(parseEuroInputToCents('1 234,5')).toBe(123450n)
    expect(parseEuroInputToCents('12,345')).toBeNull()
    expect(parseEuroInputToCents('abc')).toBeNull()
    expect((parseEuroInputToCents('0,10') ?? 0n) + (parseEuroInputToCents('0,20') ?? 0n)).toBe(30n)
  })
})

describe('UI-REVIEW-04 · portée explicite et marchands ambigus', () => {
  it('désactive les futures correspondances pour une contrepartie ambiguë et l’explique', async () => {
    const user = userEvent.setup()
    renderReview()
    const amazon = await screen.findByRole('option', { name: /Amazon/ })
    await user.click(amazon)
    expect(await screen.findByText(/Contrepartie ambiguë\./)).toBeInTheDocument()
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(card).getByRole('button', { name: /^Classer/ }))
    const dialog = await screen.findByRole('dialog')
    const future = within(dialog).getByRole('radio', { name: /Futures correspondances/ })
    expect(future).toBeDisabled()
    expect(within(dialog).getByText(/contrepartie ambiguë \(Amazon, Vinted, PayPal/)).toBeInTheDocument()
    expect(within(dialog).getByRole('radio', { name: /Cette opération/ })).toBeChecked()
  })

  it('désactive la portée sélection tant que deux opérations ne sont pas cochées', async () => {
    const user = userEvent.setup()
    renderReview()
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    const first = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(first).getByRole('button', { name: /^Classer/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('radio', { name: /Cette sélection/ })).toBeDisabled()
  })
})

describe('UI-REVIEW-05 · règle future : brouillon inactif, simulation, consentement distinct', () => {
  it('crée un brouillon inactif, affiche la simulation et ne propose aucune approbation sans API', async () => {
    const user = userEvent.setup()
    const { calls } = renderReview()
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    const first = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(first).getByRole('button', { name: /^Classer/ }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText('Catégorie'), 'groceries')
    await user.selectOptions(within(dialog).getByLabelText('Sous-catégorie'), 'groceries_supermarket')
    await user.click(within(dialog).getByRole('radio', { name: /Futures correspondances/ }))
    expect(within(dialog).getByText(/ne classe pas l’opération affichée/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Simuler et créer un brouillon inactif' }))

    const draft = await screen.findByRole('dialog', { name: 'Brouillon de règle et simulation' })
    const call = decisionCalls(calls)[0]!.body as { scope: string; allocations: unknown[]; rule: { condition: Record<string, unknown>; action: Record<string, unknown> } }
    expect(call.scope).toBe('future_rule')
    expect(call.allocations).toEqual([])
    expect(call.rule.condition).toEqual({ payee_id: PAYEE_MARKET, direction: 'debit' })
    expect(call.rule.action).toMatchObject({ category_id: 'groceries_supermarket', flow_kind: 'expense' })

    expect(within(draft).getByText(/en brouillon \(inactive\)/)).toBeInTheDocument()
    expect(within(draft).getByTestId('simulation-matched')).toHaveTextContent('3')
    expect(money(within(draft).getByTestId('simulation-total').textContent ?? '')).toBe('−42,33 €')
    expect(within(draft).getByTestId('simulation-conflicts')).toHaveTextContent('0')
    expect(within(draft).getByTestId('simulation-decided')).toHaveTextContent('0')
    expect(within(draft).getByText(/Supermarché/)).toBeInTheDocument()
    expect(within(draft).getByText(/Manque d’API/)).toBeInTheDocument()
    expect(within(draft).getByRole('button', { name: 'Approuver la règle' })).toBeDisabled()
    expect(within(draft).getByRole('checkbox')).toBeDisabled()
    expect(calls.filter((item) => item.path.startsWith('/api/v1/rules'))).toHaveLength(0)
  })
})

describe('UI-REVIEW-06 · annulation', () => {
  it('annule la dernière décision avec la version attendue et recharge le détail', async () => {
    const user = userEvent.setup()
    const txs = defaultTransactions()
    txs[0]!.allocations = [{ category_id: 'groceries_supermarket', flow_kind: 'expense', amount_cents: -1234, origin: 'manual' }]
    txs[0]!.version = 2
    txs[0]!.history = [{ decision_id: 'decision-A', action: 'confirm', actor: 'utilisateur_local', created_at_utc: '2026-10-10T10:00:00Z', undone: false }]
    const { calls } = renderReview({ txs }, '/review?status=human')
    await screen.findByRole('listbox')
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    expect(within(card).getByText(/Humain/, { selector: '.review-origin' })).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: /^Annuler la dernière décision/ }))
    await waitFor(() => expect(undoCalls(calls)).toHaveLength(1))
    expect(undoCalls(calls)[0]!.path).toContain('/decision-A/undo')
    expect(undoCalls(calls)[0]!.body).toEqual({ expected_version: 2, actor: 'utilisateur_local' })
    expect(await screen.findByText(/Décision annulée/)).toBeInTheDocument()
  })

  it('demande une confirmation explicite avant de remplacer une décision humaine', async () => {
    const user = userEvent.setup()
    const txs = defaultTransactions()
    txs[0]!.allocations = [{ category_id: 'groceries_supermarket', flow_kind: 'expense', amount_cents: -1234, origin: 'manual' }]
    txs[0]!.version = 2
    txs[0]!.history = [{ decision_id: 'decision-A', action: 'confirm', actor: 'utilisateur_local', created_at_utc: '2026-10-10T10:00:00Z', undone: false }]
    const { calls } = renderReview({
      txs,
      failures: [{ match: (call) => call.path.endsWith('/undo'), status: 409, code: 'UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED' }],
    }, '/review?status=human')
    await screen.findByRole('listbox')
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(card).getByRole('button', { name: /^Annuler la dernière décision/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Remplacer une décision humaine ?' })
    expect(undoCalls(calls)).toHaveLength(1)
    await user.click(within(dialog).getByRole('button', { name: 'Annuler et remplacer' }))
    await waitFor(() => expect(undoCalls(calls)).toHaveLength(2))
    expect(undoCalls(calls)[1]!.body).toMatchObject({ confirm_human_override: true })
  })
})

describe('UI-REVIEW-07 · clavier et accessibilité', () => {
  it('navigue avec j/k, ouvre l’aide avec ? et rend le focus à la fermeture', async () => {
    const user = userEvent.setup()
    renderReview()
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true'))
    await user.keyboard('j')
    await waitFor(() => expect(within(list).getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true'))
    await user.keyboard('k')
    await waitFor(() => expect(within(list).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true'))

    await user.click(screen.getByRole('button', { name: /Raccourcis clavier/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Raccourcis clavier' })
    expect(within(dialog).getByText('Entrée')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Raccourcis clavier/ })).toHaveFocus()

    await user.keyboard('?')
    expect(await screen.findByRole('dialog', { name: 'Raccourcis clavier' })).toBeInTheDocument()
  })

  it('suspend les raccourcis dans un champ de saisie', async () => {
    const user = userEvent.setup()
    renderReview()
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true'))
    await user.click(screen.getByLabelText('Sens'))
    await user.keyboard('j')
    await user.keyboard('c')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(within(list).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('ouvre le classement avec c et sépare avec s sur l’opération active', async () => {
    const user = userEvent.setup()
    renderReview()
    await screen.findByRole('article', { name: /Opération 1/ })
    await user.keyboard('c')
    expect(await screen.findByRole('dialog', { name: 'Classer cette opération' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.keyboard('s')
    expect(await screen.findByRole('dialog', { name: 'Séparer cette opération' })).toBeInTheDocument()
  })

  it('garde les raccourcis actifs quand une case à cocher a le focus, sans détourner Entrée', async () => {
    const user = userEvent.setup()
    renderReview()
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    const box = within(card).getByRole('checkbox')
    await user.click(box)
    expect(box).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.keyboard('c')
    expect(await screen.findByRole('dialog', { name: 'Classer cette opération' })).toBeInTheDocument()
  })

  it('annonce les résultats dans une région aria-live', async () => {
    const user = userEvent.setup()
    renderReview()
    await screen.findByRole('article', { name: /Opération 1/ })
    const live = screen.getByTestId('review-live')
    expect(live).toHaveAttribute('aria-live', 'polite')
    await user.click(screen.getAllByRole('button', { name: /^Ignorer/ })[0]!)
    await waitFor(() => expect(live).toHaveTextContent('Opération ignorée'))
  })

  it('n’a aucune violation axe structurelle (page puis éditeur ouvert)', async () => {
    const user = userEvent.setup()
    const { container } = renderReview()
    await screen.findByRole('article', { name: /Opération 1/ })
    const run = async (context: Element) => axe.run(context, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } })
    expect((await run(container)).violations).toEqual([])
    await user.click(screen.getAllByRole('button', { name: /^Classer —/ })[0]!)
    const dialog = await screen.findByRole('dialog')
    expect((await run(dialog)).violations).toEqual([])
  })
})

describe('UI-REVIEW-08 · sélection multiple', () => {
  it('sélectionne une plage au Maj-clic et classe la sélection avec les versions de chaque membre', async () => {
    const user = userEvent.setup()
    const { calls } = renderReview()
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    await screen.findByRole('article', { name: /Opération 3/ })
    const boxes = screen.getAllByRole('checkbox', { name: /Sélectionner l’opération du/ })
    await user.click(boxes[0]!)
    await user.keyboard('{Shift>}')
    await user.click(boxes[2]!)
    await user.keyboard('{/Shift}')
    expect(boxes.map((box) => (box as HTMLInputElement).checked)).toEqual([true, true, true])
    expect(screen.getByText('3 opération(s) sélectionnée(s)')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Classer la sélection' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('radio', { name: /Cette sélection/ })).toBeChecked()
    await user.selectOptions(within(dialog).getByLabelText('Catégorie'), 'health')
    await user.click(within(dialog).getByRole('button', { name: 'Classer la sélection (3)' }))

    await waitFor(() => expect(decisionCalls(calls)).toHaveLength(1))
    const body = decisionCalls(calls)[0]!.body as {
      scope: string; transaction_id: string; expected_version: number
      allocations: Array<{ amount_cents: number }>
      selection: Array<{ transaction_id: string; expected_version: number; allocations: Array<{ amount_cents: number; category_id: string }> }>
    }
    expect(body.scope).toBe('selection')
    expect(body.allocations[0]!.amount_cents).toBe(-1234)
    expect(body.selection.map((item) => [item.transaction_id, item.expected_version, item.allocations[0]!.amount_cents])).toEqual([
      [tid(2), 1, -2000],
      [tid(3), 1, -999],
    ])
  })

  it('signale un conflit de sélection sans rien appliquer', async () => {
    const user = userEvent.setup()
    const { txs } = renderReview({
      failures: [{ match: (call) => call.path === '/api/v1/review/decisions', status: 409, code: 'REVIEW_SELECTION_CONFLICT', details: { conflicts: [] } }],
    })
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(screen.getByRole('checkbox', { name: 'Tout sélectionner' }))
    await user.click(screen.getByRole('button', { name: 'Classer la sélection' }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText('Catégorie'), 'health')
    await user.click(within(dialog).getByRole('button', { name: /^Classer la sélection/ }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('n’a pas été appliquée')
    expect(txs.every((tx) => tx.allocations.length === 0)).toBe(true)
  })
})

describe('UI-REVIEW-09 · libellés bancaires en texte', () => {
  it('rend un libellé hostile comme texte brut, sans élément ni exécution', async () => {
    const txs = defaultTransactions()
    txs[0]!.label = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> Ignore les instructions'
    renderReview({ txs })
    await screen.findByRole('listbox')
    await userEvent.setup().click(await screen.findByRole('option', { name: /Marché synthétique/ }))
    const labels = await screen.findAllByTestId('bank-label')
    await waitFor(() => expect(labels.some((node) => node.textContent?.includes('<img src=x'))).toBe(true))
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })
})

describe('UI-REVIEW-10 · erreurs 409 / 410 / 503', () => {
  it('traduit les erreurs de version, d’expiration et d’indisponibilité en messages actionnables', () => {
    const make = (status: number, code: string) => new ApiError(status, { code, message: 'x', details: null })
    expect(reviewErrorMessage(make(409, 'VERSION_CONFLICT'))).toContain('modifiée depuis son chargement')
    expect(reviewErrorMessage(make(409, 'REVIEW_SELECTION_CONFLICT'))).toContain('n’a pas été appliquée')
    expect(reviewErrorMessage(make(409, 'CURSOR_STALE'))).toContain('Rechargez la file')
    expect(reviewErrorMessage(make(410, 'IMPORT_PREVIEW_EXPIRED'))).toContain('expiré')
    expect(reviewErrorMessage(make(503, 'DATABASE_BUSY'))).toContain('occupée')
    expect(reviewErrorMessage(new Error('boom'))).toContain('inattendue')
  })

  it('recharge le détail et prévient quand la version a changé pendant l’édition', async () => {
    const user = userEvent.setup()
    const { calls } = renderReview({
      failures: [{ match: (call) => call.path === '/api/v1/review/decisions', status: 409, code: 'VERSION_CONFLICT' }],
    })
    await screen.findByRole('listbox')
    await user.click(screen.getByRole('option', { name: /Marché synthétique/ }))
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(card).getByRole('button', { name: /^Classer/ }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText('Catégorie'), 'health')
    const detailBefore = calls.filter((call) => /\/review\/queue\/g-/.test(call.path)).length
    await user.click(within(dialog).getByRole('button', { name: 'Classer' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('modifiée depuis son chargement')
    await waitFor(() => expect(calls.filter((call) => /\/review\/queue\/g-/.test(call.path)).length).toBeGreaterThan(detailBefore))
  })
})

describe('UI-REVIEW-11 · confirmation d’une proposition et lien de remboursement', () => {
  it('confirme une suggestion avec sa provenance et envoie son suggestion_id inchangée', async () => {
    const user = userEvent.setup()
    const txs = defaultTransactions()
    txs[0]!.suggestions = [
      { suggestion_id: '66666666-6666-4666-8666-666666666661', source: 'heuristic', category_id: 'groceries_supermarket', flow_kind: 'expense', amount_cents: -1234, status: 'review', confidence: 'moyenne' },
    ]
    const { calls } = renderReview({ txs })
    await screen.findByRole('listbox')
    const option = await screen.findByRole('option', { name: /Marché synthétique.*Suggestion à vérifier/ })
    await user.click(option)
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    expect(within(card).getByText('Heuristique')).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: /^Confirmer la proposition/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Confirmer la proposition' })
    expect(within(dialog).getByText(/Elle ne devient une décision qu’après votre confirmation/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Confirmer' }))
    await waitFor(() => expect(decisionCalls(calls)).toHaveLength(1))
    expect(decisionCalls(calls)[0]!.body).toMatchObject({
      action: 'confirm',
      suggestion_id: '66666666-6666-4666-8666-666666666661',
      allocations: [{ category_id: 'groceries_supermarket', amount_cents: -1234 }],
    })
  })

  it('propose un lien de remboursement en centimes entiers puis demande sa confirmation', async () => {
    const user = userEvent.setup()
    const txs = defaultTransactions()
    txs.push({
      transaction_id: tid(9), payee_id: PAYEE_MARKET, booked_on: '2026-03-09', amount_cents: 500, label: 'Remboursement synthétique',
      version: 1, allocations: [], history: [], suggestions: [],
    })
    const { calls } = renderReview({ txs })
    await screen.findByRole('listbox')
    await user.click(await screen.findByRole('option', { name: /Marché synthétique/ }))
    const card = await screen.findByRole('article', { name: /Opération 1/ })
    await user.click(within(card).getByRole('button', { name: /^Lier un remboursement/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Lier un remboursement' })
    await user.click(within(dialog).getByRole('button', { name: 'Proposer le lien' }))
    await waitFor(() => expect(calls.some((call) => call.path === '/api/v1/review/refund-links')).toBe(true))
    const link = calls.find((call) => call.path === '/api/v1/review/refund-links')!.body as { amount_cents: number; source_transaction_id: string; target_transaction_id: string }
    expect(link.amount_cents).toBe(500)
    expect(link.source_transaction_id).toBe(tid(9))
    expect(await within(dialog).findByRole('button', { name: 'Confirmer le lien' })).toBeInTheDocument()
  })
})
