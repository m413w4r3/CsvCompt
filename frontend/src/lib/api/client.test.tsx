import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { Transactions } from '../../pages/Transactions'
import { request } from './client'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('T42 local security', () => {
  it('SEC-05 refuses API redirects', async () => {
    const fetchMock = vi.fn(async () => new Response('{"status":"ok"}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await request<{ status: string }>('/health')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/health',
      expect.objectContaining({ redirect: 'error' }),
    )
  })

  it('SEC-03 renders an injected banking label as text', async () => {
    const hostileLabel = '<img src=x onerror=alert(1)>'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/transactions')) {
        return new Response(JSON.stringify({
          items: [{
            id: 'transaction-1',
            account_id: 'account-1',
            account_alias: 'Compte synthétique',
            booked_on: '2026-03-12',
            amount_cents: -1234,
            raw_label_preview: hostileLabel,
            occurrence_rank: 1,
            import_count: 1,
            classification_state: 'unclassified',
            residual_cents: -1234,
          }],
          next_cursor: null,
          total_count: 1,
          summary: { count: 1, debit_total_cents: -1234, credit_total_cents: 0, net_cents: -1234 },
          snapshot_at: '2026-03-12T12:00:00Z',
        }), { status: 200 })
      }
      return new Response(JSON.stringify({ items: [] }), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { container } = render(<MemoryRouter><Transactions /></MemoryRouter>)

    expect(await screen.findByText(hostileLabel)).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
  })

  it('SEC-04 frontend source keeps provider secrets out of the bundle', () => {
    const sourceRoot = join(process.cwd(), 'src')
    const sourceFiles: string[] = []
    const visit = (directory: string) => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = join(directory, entry.name)
        if (entry.isDirectory()) visit(path)
        else if (/\.(?:ts|tsx|js|jsx)$/.test(entry.name) && !/\.test\./.test(entry.name)) {
          sourceFiles.push(path)
        }
      }
    }
    visit(sourceRoot)
    const source = sourceFiles.map((path) => readFileSync(path, 'utf8')).join('\n')

    expect(source).not.toMatch(/\b(?:VITE_)?(?:LLM_)?API_KEY\b/i)
    expect(source).not.toMatch(/\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}\b/)
  })
})
