import { expect, test } from '@playwright/test'

test('CI-01 : le shell affiche le statut du backend vivant', async ({ page, request }) => {
  const backendUrl = process.env.BACKEND_URL
  if (!backendUrl) throw new Error('BACKEND_URL doit pointer vers le backend smoke.')
  const backendResponse = await request.get(backendUrl)
  expect(backendResponse.ok()).toBeTruthy()
  const backendHealth = await backendResponse.json() as {
    status: string
    version?: string
    database?: { status?: string }
    llm?: { enabled?: boolean }
  }
  expect(backendHealth.status).toBe('ok')
  await page.route('**/api/v1/health', (route) => route.fulfill({
    json: {
      status: backendHealth.status,
      api_version: backendHealth.version ?? '1',
      storage_status: backendHealth.database?.status === 'ok' ? 'ok' : 'degraded',
      llm_enabled: backendHealth.llm?.enabled ?? false,
    },
  }))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Vue d’ensemble' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Pages' }).getByRole('link')).toHaveCount(10)
  await expect(page.getByText('Serveur local connecté')).toBeVisible()
})
