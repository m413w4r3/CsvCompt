import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  formatNullableCents,
  periodCompletenessLabel,
  TransactionsRouteBoundary,
} from './index'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('G3 transaction safeguards', () => {
  it('M-03 never formats an unsafe cent total as a JavaScript number', () => {
    expect(formatNullableCents(null)).toBe('hors plage entière sûre')
    expect(formatNullableCents(Number.MAX_SAFE_INTEGER + 1)).toBe('hors plage entière sûre')
    expect(formatNullableCents(-1234)).toContain('12,34')
  })

  it('M-04 labels a period before the first import neutrally', () => {
    expect(periodCompletenessLabel('not_started')).toBe('Avant le premier import')
  })

  it('M-03 catches a render failure inside the Transactions route boundary', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    function BrokenChild(): never {
      throw new Error('synthetic render failure')
    }

    render(<TransactionsRouteBoundary><BrokenChild /></TransactionsRouteBoundary>)
    expect(await screen.findByRole('alert')).toHaveTextContent('Cette page a rencontré un problème.')
  })
})
