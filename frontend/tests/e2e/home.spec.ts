import { expect, test } from '@playwright/test'

async function expectShellHealth(page: import('@playwright/test').Page, storageStatus: string) {
  const healthResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/v1/health'
  })
  await page.goto('/')
  const healthResponse = await healthResponsePromise
  expect(new URL(healthResponse.url()).origin).toBe('http://127.0.0.1:4173')
  expect(healthResponse.ok()).toBeTruthy()
  const health = await healthResponse.json() as {
    status: string
    api_version: string
    storage_status: string
    llm_enabled: boolean
  }
  expect(health.status).toBe('ok')
  expect(health.api_version).toBe('1')
  expect(health.storage_status).toBe(storageStatus)
  await expect(page.getByRole('heading', { name: 'Vue d’ensemble' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Pages' }).getByRole('link')).toHaveCount(10)
}

test('CI-01 : le shell utilise health via le proxy avec la base migrée', async ({ page }) => {
  await expectShellHealth(page, 'ok')
  await expect(page.getByText('Serveur local connecté').first()).toBeVisible()
})

test('CI-01 : le shell signale la base vide sans migration préalable', async ({ page }) => {
  await expectShellHealth(page, 'degraded')
  await expect(page.getByText('Base locale à initialiser ou à migrer').first()).toBeVisible()
})
