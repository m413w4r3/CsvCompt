import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppShell } from '../../src/components/layout/AppShell'
import { navigation } from '../../src/components/layout/navigation'
import { Overview } from '../../src/pages/Overview/index'
import { Expenses } from '../../src/pages/Expenses/index'
import { Income } from '../../src/pages/Income/index'
import { Family } from '../../src/pages/Family/index'
import { Merchants } from '../../src/pages/Merchants/index'
import { Budgets } from '../../src/pages/Budgets/index'
import { Review } from '../../src/pages/Review/index'
import { Insights } from '../../src/pages/Insights/index'
import { Transactions } from '../../src/pages/Transactions/index'
import { Settings } from '../../src/pages/Settings/index'
import { NotFound } from '../../src/pages/NotFound/index'

const healthOk = () => new Response(JSON.stringify({ status: 'ok', api_version: '1', storage_status: 'ok', llm_enabled: false }), { status: 200, headers: { 'Content-Type': 'application/json' } })
const healthResponse = (status: string, storage_status: string) => new Response(JSON.stringify({ status, api_version: '1', storage_status, llm_enabled: false, version: '1' }), { status: 200, headers: { 'Content-Type': 'application/json' } })

function renderShell(path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Routes><Route element={<AppShell />}>
    <Route path="/" element={<Overview />} /><Route path="/expenses" element={<Expenses />} /><Route path="/income" element={<Income />} />
    <Route path="/family" element={<Family />} /><Route path="/merchants" element={<Merchants />} /><Route path="/budgets" element={<Budgets />} />
    <Route path="/review" element={<Review />} /><Route path="/insights" element={<Insights />} /><Route path="/transactions" element={<Transactions />} />
    <Route path="/settings" element={<Settings />} /><Route path="*" element={<NotFound />} />
  </Route></Routes></MemoryRouter></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear() })

describe('UI-00 · shell et navigation', () => {
  it('affiche la page sans données fabriquées et les dix destinations', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(healthOk()))
    renderShell()
    expect(screen.getByRole('heading', { name: 'Vue d’ensemble' })).toBeInTheDocument()
    expect(screen.getByText('Votre espace commence ici')).toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Pages' })
    expect(within(nav).getAllByRole('link')).toHaveLength(10)
    for (const item of navigation) expect(within(nav).getByRole('link', { name: item.label })).toBeInTheDocument()
    expect(await within(document.querySelector('.sidebar') as HTMLElement).findByText('Serveur local connecté')).toBeInTheDocument()
  })

  it('affiche les pages vides et une page 404 sans données fictives', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(healthOk()))
    const user = userEvent.setup()
    renderShell()
    const nav = screen.getByRole('navigation', { name: 'Pages' })
    for (const item of navigation.slice(1)) {
      await user.click(within(nav).getByRole('link', { name: item.label }))
      expect(await screen.findByRole('heading', { name: item.label })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /Importer un relevé/ })).toHaveAttribute('href', '/transactions')
    }
    expect(navigation).toHaveLength(10)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/adresse-inconnue']}><Routes><Route element={<AppShell />}><Route path="*" element={<NotFound />} /></Route></Routes></MemoryRouter></QueryClientProvider>)
    expect(screen.getByRole('heading', { name: 'Ce chemin n’existe pas.' })).toBeInTheDocument()
  })

  it('gère health hors ligne et erreur HTTP sans bloquer la page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    const { unmount } = renderShell()
    expect(await within(document.querySelector('.sidebar') as HTMLElement).findByText('Serveur local hors ligne')).toBeInTheDocument()
    unmount()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'DATABASE_BUSY', message: 'Base indisponible', details: null } }), { status: 503 })))
    renderShell()
    await waitFor(() => expect(within(document.querySelector('.sidebar') as HTMLElement).getByText('Serveur local en erreur')).toBeInTheDocument())
    expect(screen.getByRole('heading', { name: 'Vue d’ensemble' })).toBeInTheDocument()
  })

  it.each([
    ['degraded', 'degraded', 'Base locale à initialiser ou à migrer', 'La base locale nécessite une initialisation ou une migration avant utilisation complète.', 'warning'],
    ['unavailable', 'unavailable', 'Base locale indisponible', 'La base locale ne peut pas être consultée. Le reste de l’interface demeure disponible.', 'error'],
  ])('explique l’état de la base (%s)', async (status, storage, label, explanation, stateClass) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(healthResponse(status, storage)))
    renderShell()
    const sidebar = document.querySelector('.sidebar') as HTMLElement
    await within(sidebar).findByText(label)
    const health = sidebar.querySelector('.health')
    expect(health).toHaveAttribute('title', explanation)
    expect(health).toHaveClass(`health--${stateClass}`)
    expect(screen.getByRole('heading', { name: 'Vue d’ensemble' })).toBeInTheDocument()
  })

  it('rend le lien d’évitement et permet de piloter le menu au clavier', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(healthOk()))
    const user = userEvent.setup()
    renderShell()
    const skip = screen.getByRole('link', { name: 'Aller au contenu' })
    await user.tab()
    expect(skip).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(document.getElementById('main-content')).toHaveFocus()
    const menu = screen.getByRole('button', { name: 'Ouvrir le menu' })
    expect(menu).toHaveAttribute('aria-expanded', 'false')
    await user.click(menu)
    expect(menu).toHaveAttribute('aria-expanded', 'true')
  })

  it('retire le menu mobile fermé du parcours clavier, piège Tab et ferme avec Échap', async () => {
    vi.spyOn(window, 'matchMedia').mockImplementation((query) => ({
      matches: query === '(max-width: 760px)', media: query, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(healthOk()))
    const user = userEvent.setup()
    renderShell()
    const sidebar = document.querySelector('aside[aria-label="Navigation principale"]')
    const menuButton = screen.getByRole('button', { name: 'Ouvrir le menu' })
    expect(sidebar).toHaveAttribute('inert')
    expect(sidebar).toHaveAttribute('aria-hidden', 'true')

    await user.click(menuButton)
    const links = within(sidebar as HTMLElement).getAllByRole('link')
    expect(menuButton).toHaveAttribute('aria-expanded', 'true')
    expect(links[0]).toHaveFocus()
    fireEvent.keyDown(links[0] as HTMLElement, { key: 'Tab', shiftKey: true })
    expect(links.at(-1)).toHaveFocus()
    fireEvent.keyDown(links.at(-1) as HTMLElement, { key: 'Tab' })
    expect(links[0]).toHaveFocus()

    fireEvent.keyDown(links[0] as HTMLElement, { key: 'Escape' })
    expect(menuButton).toHaveFocus()
    expect(menuButton).toHaveAttribute('aria-expanded', 'false')
    expect(sidebar).toHaveAttribute('inert')
  })
})
