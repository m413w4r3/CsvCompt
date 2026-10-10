import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Transactions } from '../../src/pages/Transactions'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

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
})
