import { expect, test } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { tmpdir } from 'node:os'

const fixtures = resolve(process.cwd(), '../backend/tests/fixtures')
test.describe.configure({ mode: 'serial' })

test('UI-05 importe les chevauchements et trace un nouvel essai en no_op', async ({ page }) => {
  await page.goto('/transactions')
  await page.getByRole('tab', { name: 'Imports' }).click()
  const picker = page.locator('input[type="file"]')
  await picker.setInputFiles(resolve(fixtures, 'overlap_A.csv'))
  await expect(page.getByRole('heading', { name: 'Vérifiez avant d’importer' })).toBeVisible()
  await page.getByLabel('Associer à un compte').selectOption('new:create')
  await expect(page.getByLabel('Rôle').locator('option[value="pea"]')).toHaveCount(1)
  await expect(page.getByLabel('Rôle').locator('option[value="cash"]')).toHaveCount(1)
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

test('UI-05 lie une ligne hors période à son opération sans créer de doublon', async ({ page }) => {
  const source = resolve(fixtures, 'adversarial/out_of_period_line.csv')
  const temporaryDirectory = mkdtempSync(join(tmpdir(), 'comptes-familiaux-g3-'))
  const inPeriodPath = join(temporaryDirectory, 'synthetic-in-period.csv')
  const inPeriod = readFileSync(source, 'latin1').replace('15/03/2026', '20/03/2026')
  writeFileSync(inPeriodPath, inPeriod, 'latin1')

  try {
    await page.goto('/transactions')
    await page.getByRole('tab', { name: 'Imports' }).click()
    const picker = page.locator('input[type="file"]')
    await picker.setInputFiles(inPeriodPath)
    await expect(page.getByRole('heading', { name: 'Vérifiez avant d’importer' })).toBeVisible()

    const accountMapping = page.getByLabel('Associer à un compte')
    if (await accountMapping.count()) {
      await accountMapping.selectOption('new:create')
      await page.getByLabel('Alias').fill('Compte ligne hors période e2e')
      await page.getByRole('button', { name: 'Créer et associer' }).click()
    }
    await page.getByRole('button', { name: 'Importer' }).click()
    await expect(page.locator('.notice')).toContainText('Import terminé')

    await picker.setInputFiles(source)
    await expect(page.getByRole('heading', { name: 'Vérifiez avant d’importer' })).toBeVisible()
    const decision = page.getByLabel('Décision ligne 1, section 1')
    await expect(decision).toHaveValue(/link:/)
    await expect(decision.locator('option:checked')).toContainText('Lier à l’opération existante (rang 1)')
    await expect(page.getByRole('button', { name: 'Lier à l’opération existante (rang 1)' })).toBeVisible()
    await expect(page.getByText(/crée toujours une nouvelle opération \(rang n\+1\)/)).toBeVisible()
    await page.getByRole('button', { name: 'Lier à l’opération existante (rang 1)' }).click()
    await expect(page.getByRole('button', { name: 'Importer' })).toBeEnabled()
    await page.getByRole('button', { name: 'Importer' }).click()
    await expect(page.locator('.notice')).toContainText('Import terminé')

    await page.getByRole('button', { name: 'Actualiser' }).click()
    const linkedImport = page.getByRole('listitem').filter({ hasText: '1 opération(s) · 0 créée(s) · 1 source(s)' })
    await expect(linkedImport).toHaveCount(1)
    const diagnostics = linkedImport.getByRole('button', { name: 'Afficher les rejets et diagnostics' })
    await expect(diagnostics).toBeVisible()
    await diagnostics.click()
    await expect(linkedImport.getByText('Résolu')).toBeVisible()
    await expect(linkedImport.getByText(/OUT_OF_PERIOD/)).toBeVisible()

    await page.getByRole('tab', { name: 'Opérations' }).click()
    const matchingRows = page.getByRole('row').filter({ hasText: 'OPERATION HORS PLAGE' })
    await expect(matchingRows).toHaveCount(1)
    await expect(matchingRows.locator('td').nth(6)).toHaveText('2')
    const opener = page.getByRole('button', { name: /Détail opération du/ }).first()
    await opener.click()
    const dialog = page.getByRole('dialog', { name: 'Détail de l’opération' })
    await expect(dialog).toBeVisible()
    const close = dialog.getByRole('button', { name: 'Fermer' })
    await expect(close).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(close).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(opener).toBeFocused()

    const operationsTab = page.getByRole('tab', { name: 'Opérations' })
    const importsTab = page.getByRole('tab', { name: 'Imports' })
    await expect(operationsTab).toHaveAttribute('aria-controls', 'transactions-panel-operations')
    await expect(page.locator('#transactions-panel-operations')).toHaveAttribute('aria-labelledby', 'transactions-tab-operations')
    await operationsTab.focus()
    await page.keyboard.press('ArrowRight')
    await expect(importsTab).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('ArrowLeft')
    await expect(operationsTab).toHaveAttribute('aria-selected', 'true')
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true })
  }
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
