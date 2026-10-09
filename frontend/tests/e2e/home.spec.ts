import { expect, test } from '@playwright/test'

test('la page d’accueil expose le shell et ses destinations', async ({ page }) => {
  await page.route('**/api/v1/health', (route) => route.fulfill({ json: { status: 'ok', api_version: '1', storage_status: 'ok', llm_enabled: false } }))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Vue d’ensemble' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Pages' }).getByRole('link')).toHaveCount(10)
  await expect(page.getByText('Serveur local connecté')).toBeVisible()
})
