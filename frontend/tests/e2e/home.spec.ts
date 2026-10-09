import { expect, test } from '@playwright/test'

type HealthResponse = {
  status: string
  api_version: string
  storage_status: string
  llm_enabled: boolean
}

async function openWithHealth(
  page: import('@playwright/test').Page,
  request: import('@playwright/test').APIRequestContext,
  backendUrl: string | undefined,
  expectedStorageStatus: string,
) {
  if (!backendUrl) throw new Error('URL du backend smoke manquante.')
  const backendResponse = await request.get(backendUrl)
  expect(backendResponse.ok()).toBeTruthy()
  const backendHealth = await backendResponse.json() as HealthResponse
  expect(backendHealth.status).toBe('ok')
  expect(backendHealth.api_version).toBe('1')
  expect(backendHealth.storage_status).toBe(expectedStorageStatus)
  await page.route('**/api/v1/health', (route) => route.fulfill({ json: backendHealth }))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Vue d’ensemble' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Pages' }).getByRole('link')).toHaveCount(10)
}

test('CI-01 : le shell signale le backend prêt avec la base migrée', async ({ page, request }) => {
  await openWithHealth(page, request, process.env.BACKEND_URL, 'ok')
  await expect(page.getByText('Serveur local connecté')).toBeVisible()
})

test('CI-01 : le shell signale une base à initialiser sans migration', async ({ page, request }) => {
  await openWithHealth(page, request, process.env.UNMIGRATED_BACKEND_URL, 'degraded')
  await expect(page.getByText('Base locale à initialiser ou à migrer')).toBeVisible()
})
