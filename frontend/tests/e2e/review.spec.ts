import AxeBuilder from '@axe-core/playwright'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * E2E de la page de revue contre le backend réel et une base temporaire synthétique
 * (voir scripts/test_e2e_review.sh). Les données viennent de la fixture
 * `scenario_famille_2026.csv` ; les marchands sont rattachés par scripts/e2e_review_seed.py.
 */
const MAIN_GROUP = 'PHARMACIE DES PLATANES FICTIVE'
const AMBIGUOUS_GROUP = 'AMAZON COMMANDE DEMO'
const PERIOD = 'period_start=2026-01-01&period_end=2026-12-31'

interface Member {
  transaction_id: string
  version: number
  amount_cents: number
  status: string
  allocations: Array<{ category_id: string; amount_cents: number; origin: string }>
  history: Array<{ decision_id: string; action: string; undone: boolean }>
}
interface QueueItem { review_group_id: string; counterparty: string | null; status: string; transaction_count: number; amount_cents: number | null; quality_flags: string[] }

async function queue(request: APIRequestContext, status?: string): Promise<QueueItem[]> {
  const response = await request.get(`/api/v1/review/queue?${PERIOD}&limit=100${status ? `&status=${status}` : ''}`)
  expect(response.ok()).toBe(true)
  return ((await response.json()) as { items: QueueItem[] }).items
}

async function members(request: APIRequestContext, groupId: string): Promise<Member[]> {
  const response = await request.get(`/api/v1/review/queue/${groupId}`)
  expect(response.ok()).toBe(true)
  return ((await response.json()) as { transactions: Member[] }).transactions
}

async function groupOf(request: APIRequestContext, counterparty: string, status: string) {
  const items = await queue(request, status)
  const item = items.find((candidate) => candidate.counterparty === counterparty && candidate.status === status)
  const seen = items.filter((candidate) => candidate.counterparty === counterparty).map((candidate) => `${candidate.status}×${candidate.transaction_count}`)
  expect(item, `groupe ${counterparty} (${status}) ; présents : ${seen.join(', ')}`).toBeTruthy()
  return item!
}

const euros = (cents: number) => `${Math.floor(Math.abs(cents) / 100)},${String(Math.abs(cents) % 100).padStart(2, '0')}`
const normalize = (text: string | null) => (text ?? '').replace(/[  ]/g, ' ')

async function openGroup(page: Page, counterparty: string, status = '') {
  await page.goto(`/review?${PERIOD}${status ? `&status=${status}` : ''}`)
  const list = page.getByRole('listbox', { name: 'À traiter' })
  const options = list.getByRole('option')
  const option = list.getByRole('option', { name: new RegExp(counterparty) }).first()
  await expect(options.first()).toBeVisible()
  // La file est paginée (50 groupes) : « Charger plus » jusqu'à trouver la contrepartie.
  for (let page_index = 0; page_index < 10 && (await option.count()) === 0; page_index += 1) {
    const more = page.getByRole('button', { name: 'Charger plus' })
    if ((await more.count()) === 0) break
    const loaded = await options.count()
    await more.click()
    await expect.poll(() => options.count()).toBeGreaterThan(loaded)
  }
  await expect(option).toBeVisible()
  await option.click()
  await expect(page.getByRole('heading', { level: 2, name: counterparty })).toBeVisible()
}

function card(page: Page, transactionId: string) {
  return page.locator(`[data-transaction-id="${transactionId}"]`)
}

async function pickCategory(dialog: ReturnType<Page['getByRole']>, category: string, sub: string, part = 0) {
  await dialog.getByRole('combobox', { name: 'Catégorie', exact: true }).nth(part).selectOption(category)
  if (sub) await dialog.getByRole('combobox', { name: 'Sous-catégorie', exact: true }).nth(part).selectOption(sub)
}

test.describe.configure({ mode: 'serial' })

let ambiguousBefore: Array<Pick<Member, 'transaction_id' | 'version' | 'status'> & { allocations: number }> = []
let baselineClassified = 0

test('UI-REVIEW-E2E-00 prépare l’état de référence (marchand ambigu voisin)', async ({ request }) => {
  const group = await groupOf(request, AMBIGUOUS_GROUP, 'unclassified')
  expect(group.quality_flags).toContain('ambiguous_merchant')
  ambiguousBefore = (await members(request, group.review_group_id)).map((item) => ({
    transaction_id: item.transaction_id, version: item.version, status: item.status, allocations: item.allocations.length,
  }))
  expect(ambiguousBefore.length).toBeGreaterThanOrEqual(2)
  baselineClassified = (await queue(request, 'human')).reduce((sum, item) => sum + item.transaction_count, 0)
  expect(baselineClassified).toBe(0)
})

test('RULE-05 classe une seule occurrence d’un groupe ; les voisines restent inchangées et la décision persiste', async ({ page, request }) => {
  const group = await groupOf(request, MAIN_GROUP, 'unclassified')
  const before = await members(request, group.review_group_id)
  expect(before.length).toBeGreaterThanOrEqual(3)
  const target = before[1]!

  await openGroup(page, MAIN_GROUP)
  await expect(page.getByRole('article')).toHaveCount(before.length)
  // Le libellé bancaire vient du registre des opérations et s'affiche en texte.
  await expect(card(page, target.transaction_id).getByTestId('bank-label')).toContainText('PHARMACIE')

  await card(page, target.transaction_id).getByRole('button', { name: /^Classer —/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Classer cette opération' })
  await expect(dialog.getByRole('radio', { name: /Cette opération/ })).toBeChecked()
  await expect(dialog.getByRole('radio', { name: /Futures correspondances/ })).not.toBeChecked()
  await pickCategory(dialog, 'health', 'health_pharmacy')
  await dialog.getByRole('button', { name: 'Classer', exact: true }).click()
  await expect(page.getByTestId('review-live')).toContainText('Décision enregistrée')

  // Persistance côté serveur : seule la cible a changé.
  const after = await members(request, group.review_group_id)
  expect(after.map((item) => item.transaction_id).sort()).toEqual(before.filter((item) => item.transaction_id !== target.transaction_id).map((item) => item.transaction_id).sort())
  for (const item of after) {
    const previous = before.find((candidate) => candidate.transaction_id === item.transaction_id)!
    expect(item.version).toBe(previous.version)
    expect(item.allocations).toEqual([])
  }
  const human = await groupOf(request, MAIN_GROUP, 'human')
  const [classified] = await members(request, human.review_group_id)
  expect(classified!.transaction_id).toBe(target.transaction_id)
  expect(classified!.version).toBeGreaterThan(target.version)
  expect(classified!.allocations).toHaveLength(1)
  expect(classified!.allocations[0]).toMatchObject({ category_id: 'health_pharmacy', amount_cents: target.amount_cents, origin: 'manual' })
})

test('RULE-06 sépare une opération au clavier : la validation reste bloquée tant que la somme n’est pas exacte', async ({ page, request }) => {
  const group = await groupOf(request, MAIN_GROUP, 'unclassified')
  const [target] = (await members(request, group.review_group_id)).slice(0, 1)
  const total = Math.abs(target!.amount_cents)
  expect(total).toBeGreaterThan(200)

  await openGroup(page, MAIN_GROUP)
  const split = card(page, target!.transaction_id).getByRole('button', { name: /^Séparer —/ })
  await split.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'Séparer cette opération' })
  await expect(dialog).toBeVisible()
  const submit = dialog.getByRole('button', { name: 'Enregistrer la séparation' })
  await expect(submit).toBeDisabled()

  await pickCategory(dialog, 'health', 'health_pharmacy')
  await pickCategory(dialog, 'groceries', 'groceries_supermarket', 1)

  const amounts = dialog.getByRole('textbox', { name: /^Montant/ })
  await amounts.nth(0).focus()
  await page.keyboard.type('1,00')
  const remainder = dialog.getByTestId('split-remainder')
  await expect(remainder).toHaveAttribute('aria-live', 'polite')
  await expect(remainder).toContainText('Il reste')
  expect(normalize(await remainder.textContent())).toContain(`${euros(total - 100)} €`)
  await expect(submit).toBeDisabled()

  // Un centime de trop est refusé ; l'exactitude seule débloque l'enregistrement.
  await amounts.nth(1).focus()
  await page.keyboard.type(euros(total - 99))
  await expect(remainder).toContainText('Dépassement')
  await expect(submit).toBeDisabled()
  await amounts.nth(1).fill('')
  await page.keyboard.type(euros(total - 100))
  await expect(remainder).toContainText('Somme exacte')
  await expect(submit).toBeEnabled()
  await submit.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('review-live')).toContainText('Décision enregistrée')

  const human = await groupOf(request, MAIN_GROUP, 'human')
  const split2 = (await members(request, human.review_group_id)).find((item) => item.transaction_id === target!.transaction_id)!
  expect(split2.allocations).toHaveLength(2)
  expect(split2.allocations.reduce((sum, part) => sum + part.amount_cents, 0)).toBe(target!.amount_cents)
  expect(split2.allocations.map((part) => part.amount_cents).sort((a, b) => a - b)).toEqual([target!.amount_cents + 100, -100].sort((a, b) => a - b))
  expect(['confirm', 'split']).toContain(split2.history.at(-1)!.action)
})

test('UI-REVIEW-E2E-01 règle future : brouillon inactif, simulation visible, rien n’est classé', async ({ page, request }) => {
  const group = await groupOf(request, MAIN_GROUP, 'unclassified')
  const remaining = await members(request, group.review_group_id)
  const humanBefore = (await queue(request, 'human')).reduce((sum, item) => sum + item.transaction_count, 0)
  const target = remaining[0]!

  await openGroup(page, MAIN_GROUP)
  await card(page, target.transaction_id).getByRole('button', { name: /^Classer —/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Classer cette opération' })
  await pickCategory(dialog, 'health', 'health_pharmacy')
  await dialog.getByRole('radio', { name: /Futures correspondances/ }).check()
  await expect(dialog.getByText(/ne classe pas l’opération affichée/)).toBeVisible()
  await dialog.getByRole('button', { name: 'Simuler et créer un brouillon inactif' }).click()

  const draft = page.getByRole('dialog', { name: 'Brouillon de règle et simulation' })
  await expect(draft).toBeVisible()
  await expect(draft.getByText(/en brouillon \(inactive\)/)).toBeVisible()
  const matched = Number(await draft.getByTestId('simulation-matched').textContent())
  expect(Number.isInteger(matched)).toBe(true)
  expect(matched).toBeGreaterThanOrEqual(1)
  await expect(draft.getByTestId('simulation-total')).toContainText('€')
  await expect(draft.getByTestId('simulation-conflicts')).toBeVisible()
  await expect(draft.getByTestId('simulation-decided')).toBeVisible()
  await expect(draft.getByText(/Manque d’API/)).toBeVisible()
  await expect(draft.getByRole('button', { name: 'Approuver la règle' })).toBeDisabled()
  const draftAxe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze()
  expect(draftAxe.violations, 'brouillon de règle et simulation').toEqual([])
  await draft.getByRole('button', { name: /^Terminer/ }).click()
  await expect(draft).toBeHidden()

  // Aucune opération n'a été classée par la création du brouillon.
  const humanAfter = (await queue(request, 'human')).reduce((sum, item) => sum + item.transaction_count, 0)
  expect(humanAfter).toBe(humanBefore)
  const still = await members(request, group.review_group_id)
  expect(still.map((item) => [item.transaction_id, item.version])).toEqual(remaining.map((item) => [item.transaction_id, item.version]))
})

test('UI-REVIEW-E2E-02 recharge la page : décisions, filtres et groupe persistent ; annulation sans SQL', async ({ page, request }) => {
  await openGroup(page, MAIN_GROUP, 'human')
  const url = new URL(page.url())
  expect(url.searchParams.get('status')).toBe('human')
  expect(url.search).not.toMatch(/PHARMACIE|q=/)
  await page.reload()
  await expect(page.getByRole('heading', { level: 2, name: MAIN_GROUP })).toBeVisible()
  await expect(page.getByRole('article')).toHaveCount(2)

  const human = await groupOf(request, MAIN_GROUP, 'human')
  const [first, second] = await members(request, human.review_group_id)
  // Annule la décision de la première opération (sa dernière décision est annulable).
  const target = [first!, second!].find((item) => item.allocations.length === 1)!
  await card(page, target.transaction_id).getByRole('button', { name: /^Annuler la dernière décision/ }).click()
  await expect(page.getByTestId('review-live')).toContainText('Décision annulée')

  const unclassified = await groupOf(request, MAIN_GROUP, 'unclassified')
  const restored = (await members(request, unclassified.review_group_id)).find((item) => item.transaction_id === target.transaction_id)
  expect(restored).toBeTruthy()
  expect(restored!.allocations).toEqual([])
  expect(restored!.version).toBeGreaterThan(target.version)
  expect(restored!.history.at(-1)!.action).toBe('undo')
  // L'opération bancaire reste identique : même montant.
  expect(restored!.amount_cents).toBe(target.amount_cents)
})

test('RULE-05 le marchand ambigu voisin n’a pas changé et n’offre aucune règle globale', async ({ page, request }) => {
  const group = await groupOf(request, AMBIGUOUS_GROUP, 'unclassified')
  const after = (await members(request, group.review_group_id)).map((item) => ({
    transaction_id: item.transaction_id, version: item.version, status: item.status, allocations: item.allocations.length,
  }))
  expect(after).toEqual(ambiguousBefore)

  await openGroup(page, AMBIGUOUS_GROUP)
  await expect(page.getByText(/Contrepartie ambiguë\./)).toBeVisible()
  await page.getByRole('article').first().getByRole('button', { name: /^Classer —/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('radio', { name: /Futures correspondances/ })).toBeDisabled()
  await expect(dialog.getByText(/contrepartie ambiguë \(Amazon, Vinted, PayPal/)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
})

test('UI-REVIEW-E2E-03 raccourcis clavier : j/k, aide ?, saisie préservée', async ({ page }) => {
  await page.goto(`/review?${PERIOD}`)
  const list = page.getByRole('listbox', { name: 'À traiter' })
  await expect(list.getByRole('option').first()).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('j')
  await expect(list.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('k')
  await expect(list.getByRole('option').first()).toHaveAttribute('aria-selected', 'true')
  await page.getByRole('combobox', { name: 'Statut' }).focus()
  await page.keyboard.press('j')
  await expect(list.getByRole('option').first()).toHaveAttribute('aria-selected', 'true')
  await page.locator('body').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('?')
  await expect(page.getByRole('dialog', { name: 'Raccourcis clavier' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toBeHidden()
})

for (const width of [375, 768, 1280]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test(`UI-REVIEW-E2E-04 axe sans violation à ${width}px en thème ${colorScheme} (page puis éditeur)`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.emulateMedia({ colorScheme })
      await openGroup(page, MAIN_GROUP)
      await expect(page.getByRole('article').first()).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), `débordement horizontal à ${width}px`).toBe(false)
      const tags = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']
      const pageResults = await new AxeBuilder({ page }).withTags(tags).analyze()
      expect(pageResults.violations, `page à ${width}px, ${colorScheme}`).toEqual([])

      await page.getByRole('article').first().getByRole('button', { name: /^Séparer —/ }).click()
      const dialog = page.getByRole('dialog', { name: 'Séparer cette opération' })
      await expect(dialog).toBeVisible()
      await dialog.getByRole('combobox', { name: 'Catégorie', exact: true }).first().selectOption('health')
      const dialogResults = await new AxeBuilder({ page }).withTags(tags).analyze()
      expect(dialogResults.violations, `éditeur à ${width}px, ${colorScheme}`).toEqual([])
      await page.keyboard.press('Escape')
      await expect(dialog).toBeHidden()
    })
  }
}
