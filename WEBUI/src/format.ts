// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

// Labels and formatting. Everything here follows the selected UI language, so these
// are functions (called during render) rather than constant tables.
import { intlLocale, locale, t, tOr, type Locale } from './i18n'

/** Roles, most powerful first. */
export const ROLES = ['admin', 'unit_manager', 'manager', 'teacher', 'student'] as const
export type RoleName = (typeof ROLES)[number]
export const roleLabel = (role: string | null | undefined) => tOr(`role.${role}`, role ?? '')

export type ScopeKind = 'org' | 'building' | 'room'

/** The role table, mirroring server/smp/core/roles.py (the server is authoritative; the
 *  UI uses this to offer only what the server will accept).
 *  - scopes: where the role may be assigned (org = the whole school)
 *  - grants: the roles a holder may give to others (inside their own scope)
 *  - managesTokens: holders may create API tokens, so losing the role (at school scope)
 *    deletes the tokens they made (server/smp/core/api/users.py) */
export const ROLE_TABLE: Record<
  RoleName,
  { scopes: readonly ScopeKind[]; grants: readonly RoleName[]; managesTokens: boolean }
> = {
  admin: { scopes: ['org'], grants: ['admin', 'unit_manager', 'manager', 'teacher', 'student'], managesTokens: true },
  unit_manager: { scopes: ['org'], grants: ['manager', 'teacher', 'student'], managesTokens: true },
  manager: { scopes: ['building'], grants: ['teacher', 'student'], managesTokens: false },
  teacher: { scopes: ['org', 'building', 'room'], grants: [], managesTokens: false },
  student: { scopes: ['org', 'building'], grants: [], managesTokens: false },
}

/** Roles that may be class teachers of a group (they include the teacher's powers). */
export const TEACHING_ROLES = ['teacher', 'manager', 'unit_manager'] as const

/** Where a role assignment applies, as text: the building's or room's name, '' for the
 *  whole school or a name we can't look up (the person may not be allowed to list it). */
export function roleScopeName(
  r: { room_id?: string | null; building_id?: string | null },
  buildings: { id: string; name: string }[],
  rooms: { id: string; name: string }[],
): string {
  if (r.room_id) return rooms.find((x) => x.id === r.room_id)?.name ?? ''
  if (r.building_id) return buildings.find((x) => x.id === r.building_id)?.name ?? ''
  return ''
}

export const DEVICE_STATUSES = ['pending', 'active', 'retired'] as const
const DEVICE_STATUS_CLASS: Record<string, string> = { pending: 'warning', active: 'ok', retired: 'closed' }
export const deviceStatusLabel = (s: string) => tOr(`device_status.${s}`, s)
export const deviceStatusClass = (s: string) => DEVICE_STATUS_CLASS[s] ?? ''

export const GROUP_KINDS = ['class', 'staff', 'custom'] as const
export const groupKindLabel = (kind: string) => tOr(`group_kind.${kind}`, kind)

export const TARGET_TYPES = ['org', 'building', 'room', 'group', 'device', 'user'] as const
export const targetTypeLabel = (type: string) => tOr(`target.${type}`, type)

/** Monday = 0 … Sunday = 6, as in schedule windows. */
export const DAY_INDEXES = [0, 1, 2, 3, 4, 5, 6] as const
export const dayLabel = (d: number) => tOr(`day.${d}`, String(d))

/** An agent key fingerprint as people compare it: the first 80 bits, "3F2A-91C4-...". */
export function fmtFingerprint(fp: string | null | undefined): string {
  if (!fp) return '–'
  const head = fp.slice(0, 20).toUpperCase()
  return head.match(/.{1,4}/g)!.join('-')
}

/** A module's title in the UI language (falls back to the manifest's English title). */
export const moduleTitle = (m: { name: string; title: string }) => tOr(`module.${m.name}`, m.title)

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '–'
  return new Date(iso).toLocaleString(intlLocale(), { dateStyle: 'short', timeStyle: 'short' })
}

export function fmtTime(d: Date): string {
  return d.toLocaleTimeString(intlLocale())
}

const rtfCache = new Map<Locale, Intl.RelativeTimeFormat>()
function rtf(): Intl.RelativeTimeFormat {
  let f = rtfCache.get(locale.value)
  if (!f) rtfCache.set(locale.value, (f = new Intl.RelativeTimeFormat(locale.value, { numeric: 'auto' })))
  return f
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return t('time.never')
  const sec = (new Date(iso).getTime() - Date.now()) / 1000
  const abs = Math.abs(sec)
  if (abs < 60) return rtf().format(Math.round(sec), 'second')
  if (abs < 3600) return rtf().format(Math.round(sec / 60), 'minute')
  if (abs < 86400) return rtf().format(Math.round(sec / 3600), 'hour')
  return rtf().format(Math.round(sec / 86400), 'day')
}

/** Online = the agent reported within 90 s (same window as smp/core/dashboard.py). */
export function isOnline(lastSeen: string | null | undefined): boolean {
  return !!lastSeen && Date.now() - new Date(lastSeen).getTime() < 90 * 1000
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
    .map((w) => `${(w.days ?? []).map(dayLabel).join(',')} ${w.start}–${w.end}`)
    .join(' · ')
}

/** Mirrors server/smp/core/passwords.py (the server is authoritative). */
export const passwordRules = () => t('password.rules')
