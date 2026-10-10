import { describe, expect, it } from 'vitest'
import { formatDateFr, formatMonthFr, parseIsoDate } from '../../src/lib/dates'
import { parseFilters, serializeFilters, type CommonFilters } from '../../src/lib/filters'

describe('dates françaises', () => {
  it('formate une date ISO sans dépendre du fuseau local', () => {
    expect(formatDateFr('2026-10-03')).toBe('03/10/2026')
    expect(formatMonthFr('2026-10')).toBe('octobre 2026')
  })

  it.each(['2026-02-30', '2026-2-03', '03/10/2026'])('rejette la date invalide %s', (date) => {
    expect(() => formatDateFr(date)).toThrow(RangeError)
  })

  it('valide le calendrier grégorien sans parser une chaîne avec Date', () => {
    expect(parseIsoDate('2024-02-29')).not.toBeNull()
    expect(parseIsoDate('2026-02-29')).toBeNull()
    expect(parseIsoDate('2026-13-01')).toBeNull()
  })
})

describe('filtres URL', () => {
  it('sérialise et relit une sélection canonique sans perdre les filtres communs', () => {
    const filters = parseFilters('period_start=2026-01-01&period_end=2026-10-31&account_ids=b&account_ids=a&category_ids=groceries&flow_kind=expense&mode=family_life&personal_mode=observed&status=unclassified')
    expect(filters).toEqual({ periodStart: '2026-01-01', periodEnd: '2026-10-31', accountIds: ['a', 'b'], categoryIds: ['groceries'], flowKinds: ['expense'], status: ['unclassified'], mode: 'family_life', personalMode: 'observed' })
    const query = serializeFilters(filters)
    expect(query).toContain('flow_kind=expense')
    expect(query).toContain('personal_mode=observed')
    expect(parseFilters(query)).toEqual(filters)
  })

  it('ignore les périodes mal formées et conserve les valeurs par défaut', () => {
    expect(parseFilters('period_start=2026-02-30&mode=invalid&personal_mode=invalid')).toEqual({ accountIds: [], categoryIds: [], flowKinds: [], status: [], mode: 'treasury', personalMode: 'envelope' })
  })

  it('rejette les enums inconnues et les bornes inversées sans perdre les autres filtres', () => {
    expect(parseFilters('period_start=2026-12-01&period_end=2026-01-01&account_ids=compte-1&flow_kind=n_importe_quoi&mode=n_importe_quoi&personal_mode=n_importe_quoi')).toEqual({
      accountIds: ['compte-1'], categoryIds: [], flowKinds: [], status: [], mode: 'treasury', personalMode: 'envelope',
    })
  })

  it('ne sérialise pas de filtres invalides fournis au runtime', () => {
    const invalid = {
      periodStart: '2026-12-01', periodEnd: '2026-01-01', accountIds: [], categoryIds: [],
      flowKinds: ['not_a_flow'], status: [], mode: 'unknown', personalMode: 'invalid',
    } as unknown as CommonFilters
    expect(serializeFilters(invalid)).toBe('')
  })
})
