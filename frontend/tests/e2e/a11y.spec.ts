import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

test('shell sans violation axe aux largeurs et thèmes pris en charge', async ({ page }) => {
  await page.route('**/api/v1/health', (route) => route.fulfill({ json: { status: 'ok', api_version: '1', storage_status: 'ok', llm_enabled: false } }))

  for (const width of [375, 768, 1280]) {
    for (const colorScheme of ['light', 'dark'] as const) {
      await page.setViewportSize({ width, height: 900 })
      await page.emulateMedia({ colorScheme })
      await page.goto('/')
      await expect(page.getByRole('heading', { name: 'Vue d’ensemble' })).toBeVisible()
      if (width < 760) {
        await expect(page.locator('.mobile-health').getByText('Serveur local connecté')).toBeVisible()
      }
      const widthOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
      expect(widthOverflow, `débordement horizontal à ${width}px, thème ${colorScheme}`).toBe(false)

      const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze()
      expect(results.violations, `violations axe à ${width}px, thème ${colorScheme}`).toEqual([])
    }
  }
})

test('santé compacte visible dans la topbar, y compris les états d’attente et dégradés', async ({ page }) => {
  let response: { status: string; storage_status: string } | null = { status: 'ok', storage_status: 'ok' }
  let holdResponse = false
  const pendingRoutes: Array<import('@playwright/test').Route> = []
  await page.route('**/api/v1/health', async (route) => {
    if (holdResponse) {
      pendingRoutes.push(route)
    } else if (response) {
      await route.fulfill({ json: { ...response, api_version: '1', llm_enabled: false } })
    } else {
      await route.abort()
    }
  })
  await page.setViewportSize({ width: 375, height: 812 })

  const cases = [
    [{ status: 'ok', storage_status: 'ok' }, 'Serveur local connecté'],
    [{ status: 'degraded', storage_status: 'degraded' }, 'Base locale à initialiser ou à migrer'],
    [{ status: 'unavailable', storage_status: 'unavailable' }, 'Base locale indisponible'],
  ] as const
  for (const [health, label] of cases) {
    response = health
    holdResponse = false
    await page.goto('/')
    await expect(page.locator('.mobile-health').getByText(label)).toBeVisible()
  }

  response = null
  await page.goto('/')
  await expect(page.locator('.mobile-health').getByText('Serveur local hors ligne')).toBeVisible()

  holdResponse = true
  await page.goto('/')
  await expect(page.locator('.mobile-health').getByText('Vérification…')).toBeVisible()
  holdResponse = false
  await Promise.all(pendingRoutes.map((route) => route.fulfill({ json: { status: 'ok', api_version: '1', storage_status: 'ok', llm_enabled: false } })))
})

test('menu mobile gère inert, focus initial, piège de tabulation et fermeture Échap', async ({ page }) => {
  await page.route('**/api/v1/health', (route) => route.fulfill({ json: { status: 'ok', api_version: '1', storage_status: 'ok', llm_enabled: false } }))
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/')

  const sidebar = page.locator('.sidebar')
  const toggle = page.getByRole('button', { name: 'Ouvrir le menu' })
  await expect(sidebar).toHaveAttribute('inert', '')
  await expect(sidebar).toHaveAttribute('aria-hidden', 'true')
  await toggle.click()
  await expect(sidebar).not.toHaveAttribute('inert', '')
  const links = sidebar.getByRole('link')
  await expect(links.first()).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(links.last()).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(links.first()).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(toggle).toBeFocused()
  await expect(sidebar).toHaveAttribute('inert', '')
})
