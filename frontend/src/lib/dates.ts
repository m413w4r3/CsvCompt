export type IsoDate = `${number}-${number}-${number}`

function parseIsoDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
    ? date
    : null
}

export function formatDateFr(value: string): string {
  const date = parseIsoDate(value)
  if (!date) throw new RangeError('Date attendue au format YYYY-MM-DD.')
  return new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'UTC' }).format(date)
}

export function formatMonthFr(value: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(value)
  if (!match) throw new RangeError('Mois attendu au format YYYY-MM.')
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1))
  if (date.getUTCFullYear() !== Number(match[1]) || date.getUTCMonth() !== Number(match[2]) - 1) {
    throw new RangeError('Mois invalide.')
  }
  return new Intl.DateTimeFormat('fr-FR', { month: 'long', year: 'numeric', timeZone: 'UTC' }).format(date)
}
