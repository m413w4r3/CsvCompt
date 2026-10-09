import { describe, expect, it } from 'vitest'
import { formatCentsToEuro, parseEuroInputToCents } from '../../src/lib/money'

describe('formatCentsToEuro', () => {
  it.each([
    [0n, '0,00\u00a0€'], [1n, '0,01\u00a0€'], [-1n, '−0,01\u00a0€'],
    [99n, '0,99\u00a0€'], [-99n, '−0,99\u00a0€'], [100n, '1,00\u00a0€'],
    [-100n, '−1,00\u00a0€'], [123456n, '1\u202f234,56\u00a0€'],
    [9007199254740993n, '90\u202f071\u202f992\u202f547\u202f409,93\u00a0€'],
  ])('formate exactement %s centimes', (amount, expected) => {
    expect(formatCentsToEuro(amount)).toBe(expected)
  })

  it('refuse un nombre dont la précision est déjà perdue', () => {
    expect(() => formatCentsToEuro(Number.MAX_SAFE_INTEGER + 1)).toThrow(RangeError)
  })

  it('fait un aller-retour exact sur une large série de valeurs signées', () => {
    for (let cents = -25_000n; cents <= 25_000n; cents += 37n) {
      const displayed = formatCentsToEuro(cents).replace(/\u00a0€$/, '').replaceAll('\u202f', '').replace(',', '.')
      expect(parseEuroInputToCents(displayed)).toBe(cents)
    }
  })

  it.each([
    ['12,5', 1250n], ['12.50', 1250n], ['1 234,56', 123456n], ['−1,01', -101n],
    ['-1,01', -101n], ['+0,09', 9n], ['90071992547409,93', 9007199254740993n],
  ])('parse %s', (input, expected) => {
    expect(parseEuroInputToCents(input)).toBe(expected)
  })

  it.each(['12.345', '1,2,3', 'texte', '1 23,45', '12,', ',50', ''])('rejette %s', (input) => {
    expect(parseEuroInputToCents(input)).toBeNull()
  })
})
