import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'

import { TransactionsRouteErrorElement } from './index'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Transactions route errorElement', () => {
  it('M-03 contains a route render failure in the transactions page', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    function BrokenRoute(): never {
      throw new Error('synthetic route render failure')
    }
    const router = createMemoryRouter([
      {
        path: '/',
        element: <BrokenRoute />,
        errorElement: <TransactionsRouteErrorElement />,
      },
    ])

    render(<RouterProvider router={router} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Cette page a rencontré un problème.')
    router.dispose()
  })
})
