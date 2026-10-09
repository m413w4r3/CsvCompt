const GROUP = '\u202f'

function toBigIntCents(cents: bigint | number): bigint {
  if (typeof cents === 'number' && !Number.isSafeInteger(cents)) {
    throw new RangeError('Le montant numérique doit être un entier sûr ; utiliser bigint au-delà.')
  }
  return typeof cents === 'bigint' ? cents : BigInt(cents)
}

export function formatCentsToEuro(cents: bigint | number): string {
  const value = toBigIntCents(cents)
  const negative = value < 0n
  const absolute = negative ? -value : value
  const whole = (absolute / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, GROUP)
  const fraction = (absolute % 100n).toString().padStart(2, '0')
  return `${negative ? '−' : ''}${whole},${fraction}\u00a0€`
}

export function parseEuroInputToCents(input: string): bigint | null {
  const source = input.trim().replace(/[\u00a0\u202f]/g, ' ')
  if (!source) return null

  const match = /^([+\-−]?)(\d{1,3}(?: \d{3})+|\d+)(?:([,.])(\d{1,2}))?$/.exec(source)
  if (!match) return null
  const [, sign, integerPart, , decimalPart] = match
  if (!integerPart) return null
  const whole = BigInt(integerPart.replaceAll(' ', ''))
  const cents = BigInt((decimalPart ?? '').padEnd(2, '0') || '0')
  const result = whole * 100n + cents
  return sign === '-' || sign === '−' ? -result : result
}
