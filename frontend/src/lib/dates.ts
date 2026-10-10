export type IsoDate = `${number}-${number}-${number}`

export function parseIsoDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  const maximumDay = daysInMonth[month - 1]
  if (month < 1 || month > 12 || day < 1 || maximumDay === undefined || day > maximumDay) return null

  const date = new Date(0)
  date.setUTCFullYear(year, month - 1, day)
  date.setUTCHours(0, 0, 0, 0)
  return date
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
