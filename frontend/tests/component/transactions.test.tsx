import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Transactions } from '../../src/pages/Transactions'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('UI-05 · registre des opérations', () => {
  it('affiche le résumé serveur et envoie les filtres de l’URL', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/accounts') return new Response(JSON.stringify({ items: [] }))
      return new Response(JSON.stringify({
        items: [{ id: 'op-1', account_id: 'a-1', account_alias: 'Compte test', booked_on: '2026-03-12', amount_cents: -1234, raw_label_preview: '<script>synthetique</script>', occurrence_rank: 1, import_count: 2, classification_state: 'unclassified', residual_cents: -1234 }],
        next_cursor: null, total_count: 1,
        summary: { count: 1, debit_total_cents: -1234, credit_total_cents: 0, net_cents: -1234 },
        snapshot_at: '2026-10-10T10:00:00.000Z',
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter initialEntries={['/transactions?period_start=2026-03-01&direction=debit']}><Routes><Route path="/transactions" element={<Transactions />} /></Routes></MemoryRouter>)
    expect(await screen.findByText((_, element) => element?.textContent?.replaceAll('\u00a0', ' ') === 'Débits −12,34 €')).toBeInTheDocument()
    expect(screen.getByText('<script>synthetique</script>')).toBeInTheDocument()
    expect(screen.getByText('Non ventilée')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('period_start=2026-03-01'), expect.anything())
    const user = userEvent.setup()
    await user.click(screen.getByRole('tab', { name: 'Opérations' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  })

  it('retarde les recherches rapides et ignore une réponse obsolète', async () => {
    const transactionRequests: URL[] = []
    let resolveInitial!: (response: Response) => void
    const initialResponse = new Promise<Response>((resolve) => { resolveInitial = resolve })
    const responseFor = (label: string) => new Response(JSON.stringify({
      items: [{ id: label, account_id: 'a-1', account_alias: 'Compte test', booked_on: '2026-03-12', amount_cents: -1234, raw_label_preview: label, occurrence_rank: 1, import_count: 1, classification_state: 'unclassified', residual_cents: -1234 }],
      next_cursor: 'synthetic-cursor', total_count: 2,
      summary: { count: 2, debit_total_cents: -1234, credit_total_cents: 0, net_cents: -1234 },
      snapshot_at: '2026-10-10T10:00:00.000Z',
    }))
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), window.location.origin)
      if (url.pathname === '/api/v1/accounts') return Promise.resolve(new Response(JSON.stringify({ items: [] })))
      transactionRequests.push(url)
      const query = url.searchParams.get('q')
      if (!query) return initialResponse
      return Promise.resolve(responseFor(`résultat ${query}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<MemoryRouter initialEntries={['/transactions']}><Routes><Route path="/transactions" element={<Transactions />} /></Routes></MemoryRouter>)
    await waitFor(() => expect(transactionRequests).toHaveLength(1))

    const search = screen.getByLabelText('Recherche du libellé')
    for (let index = 0; index < 50; index += 1) {
      fireEvent.change(search, { target: { value: `recherche-${index}` } })
    }
    await new Promise((resolve) => window.setTimeout(resolve, 180))
    expect(transactionRequests).toHaveLength(1)
    await new Promise((resolve) => window.setTimeout(resolve, 180))
    expect(transactionRequests).toHaveLength(2)
    expect(transactionRequests[1]?.searchParams.get('q')).toBe('recherche-49')
    expect(await screen.findByText('résultat recherche-49')).toBeInTheDocument()

    resolveInitial(responseFor('résultat initial obsolète'))
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    expect(screen.queryByText('résultat initial obsolète')).not.toBeInTheDocument()
  })
})
