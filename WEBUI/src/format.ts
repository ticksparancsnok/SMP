export const ROLE_LABELS: Record<string, string> = {
  admin: 'Adminisztrátor',
  teacher: 'Tanár',
  student: 'Diák',
}

export const DEVICE_STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: 'Függőben', cls: 'warning' },
  active: { label: 'Aktív', cls: 'ok' },
  retired: { label: 'Kivezetve', cls: 'closed' },
}

export const GROUP_KINDS: Record<string, string> = {
  class: 'Osztály',
  staff: 'Tanári',
  custom: 'Egyéb',
}

export const TARGET_TYPES: Record<string, string> = {
  org: 'Egész iskola',
  room: 'Terem',
  group: 'Csoport',
  device: 'Gép',
  user: 'Felhasználó',
}

export const DAYS = ['H', 'K', 'Sze', 'Cs', 'P', 'Szo', 'V']

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '–'
  return new Date(iso).toLocaleString('hu-HU', { dateStyle: 'short', timeStyle: 'short' })
}

const rtf = new Intl.RelativeTimeFormat('hu', { numeric: 'auto' })

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return 'soha'
  const sec = (new Date(iso).getTime() - Date.now()) / 1000
  const abs = Math.abs(sec)
  if (abs < 60) return rtf.format(Math.round(sec), 'second')
  if (abs < 3600) return rtf.format(Math.round(sec / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(sec / 3600), 'hour')
  return rtf.format(Math.round(sec / 86400), 'day')
}

/** Matches the server's 5-minute "online" window (smp/core/dashboard.py). */
export function isOnline(lastSeen: string | null | undefined): boolean {
  return !!lastSeen && Date.now() - new Date(lastSeen).getTime() < 5 * 60 * 1000
}

/** ISO string -> value for <input type="datetime-local"> (local time). */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** <input type="datetime-local"> value -> ISO string with timezone, or null. */
export function fromLocalInput(v: string): string | null {
  return v ? new Date(v).toISOString() : null
}

export function describeWindows(windows: { days?: number[]; start?: string; end?: string }[]): string {
  return windows
    .map((w) => `${(w.days ?? []).map((d) => DAYS[d]).join(',')} ${w.start}–${w.end}`)
    .join(' · ')
}

/** Mirrors server/smp/core/passwords.py (the server is authoritative). */
export const PASSWORD_RULES =
  'Legalább 8 karakter, betű és szám is legyen benne; ne legyen gyakori jelszó, és ne tartalmazza a felhasználónevet.'
