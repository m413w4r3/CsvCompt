import { expect, test } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { resolve } from 'node:path'

const fixtures = resolve(process.cwd(), '../backend/tests/fixtures')
test.describe.configure({ mode: 'serial' })

test('UI-05 importe les chevauchements et trace un nouvel essai en no_op', async ({ page }) => {
  await page.goto('/transactions')
  await page.getByRole('tab', { name: 'Imports' }).click()
  const picker = page.locator('input[type="file"]')
  await picker.setInputFiles(resolve(fixtures, 'overlap_A.csv'))
  await expect(page.getByRole('heading', { name: 'Vérifiez avant d’importer' })).toBeVisible()
  await page.getByLabel('Associer à un compte').selectOption('new:create')
  await page.getByLabel('Alias').fill('Compte e2e synthétique')
  await page.getByRole('button', { name: 'Créer et associer' }).click()
  await expect(page.getByRole('button', { name: 'Importer' })).toBeEnabled()
  await page.getByRole('button', { name: 'Importer' }).click()
  await expect(page.locator('.notice')).toContainText('Import terminé')

  await picker.setInputFiles(resolve(fixtures, 'overlap_B.csv'))
  await expect(page.getByRole('heading', { name: 'Vérifiez avant d’importer' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Importer' })).toBeEnabled()
  await page.getByRole('button', { name: 'Importer' }).click()
  await expect(page.locator('.notice')).toContainText('Import terminé')

  await picker.setInputFiles(resolve(fixtures, 'overlap_A.csv'))
  await expect(page.getByText(/Déjà importé/).first()).toBeVisible()
  await page.getByRole('button', { name: 'Confirmer le no_op' }).click()
  await expect(page.locator('.notice')).toContainText('no_op')

  await page.getByRole('tab', { name: 'Opérations' }).click()
  await expect(page.getByRole('table', { name: 'Opérations bancaires brutes' })).toBeVisible()
  const debit = await page.locator('.transaction-table tbody tr td:nth-child(4)').allInnerTexts()
  const credit = await page.locator('.transaction-table tbody tr td:nth-child(5)').allInnerTexts()
  const summary = await page.locator('.transaction-summary span').allInnerTexts()
  expect(debit.filter((value) => value !== '—').reduce((sum, value) => sum + cents(value), 0n)).toBe(
    cents(summary.find((value) => value.startsWith('Débits')) ?? ''),
  )
  expect(credit.filter((value) => value !== '—').reduce((sum, value) => sum + cents(value), 0n)).toBe(
    cents(summary.find((value) => value.startsWith('Crédits')) ?? ''),
  )
})

test('UI-05 reste accessible sur la page et pendant l’aperçu en clair et sombre', async ({ page }) => {
  await page.goto('/transactions')
  for (const theme of ['clair', 'sombre']) {
    if (theme === 'sombre') await page.getByRole('button', { name: /Activer le thème sombre/ }).click()
    for (const width of [375, 768, 1280]) {
      await page.setViewportSize({ width, height: 900 })
      const results = await new AxeBuilder({ page }).analyze()
      expect(results.violations).toEqual([])
    }
  }
  await page.getByRole('tab', { name: 'Imports' }).click()
  await page.locator('input[type="file"]').setInputFiles(resolve(fixtures, 'overlap_A.csv'))
  await expect(page.getByRole('heading', { name: 'Vérifiez avant d’importer' })).toBeVisible()
  const previewAudit = await new AxeBuilder({ page }).analyze()
  expect(previewAudit.violations).toEqual([])
})

function cents(value: string): bigint {
  const normalized = value.replace(/^.*?([−-]?\d[\d\u00a0\u202f ]*,\d{2}).*$/, '$1').replace(/[\u00a0\u202f ]/g, '')
  const match = /^([−-]?)(\d+),(\d{2})$/.exec(normalized)
  if (!match) throw new Error(`Montant affiché illisible: ${value}`)
  const amount = BigInt(match[2]!) * 100n + BigInt(match[3]!)
  return match[1] ? -amount : amount
}
