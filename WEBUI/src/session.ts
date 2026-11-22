// Who is logged in, what they may do, and which modules are enabled.
import { reactive } from 'vue'
import { api, ApiError } from './api/client'
import type { Manifest, Me, UiRoute } from './api/types'

export const session = reactive({
  me: null as Me | null,
  modules: [] as Manifest[],
  loaded: false,
})

export async function loadSession(): Promise<void> {
  try {
    session.me = await api.get<Me>('/auth/me')
    session.modules = await api.get<Manifest[]>('/modules')
  } catch (e) {
    if (!(e instanceof ApiError) || e.status !== 401) throw e
    session.me = null
    session.modules = []
  } finally {
    session.loaded = true
  }
}

export async function logout(): Promise<void> {
  try {
    await api.post('/auth/logout')
  } catch {
    // Network error etc.: still log out locally. The server accepts expired sessions.
  } finally {
    session.me = null
    session.modules = []
  }
}

/** Org-wide permission, or (with roomId) org-wide or granted for that room. Mirrors Principal.can(). */
export function can(perm: string, roomId?: string | null): boolean {
  const me = session.me
  if (!me) return false
  if (me.permissions.includes(perm)) return true
  return !!roomId && (me.room_permissions[roomId] ?? []).includes(perm)
}

/** Granted org-wide or for at least one room (e.g. a teacher assigned to some rooms). */
export function canAnywhere(perm: string): boolean {
  const me = session.me
  if (!me) return false
  return can(perm) || Object.values(me.room_permissions).some((p) => p.includes(perm))
}

/** Admins and teachers. Students only get their own pages (home, account, short links). */
export function isStaff(): boolean {
  return (session.me?.roles ?? []).some((r) => r.role === 'admin' || r.role === 'teacher')
}

export function hasModule(name: string): boolean {
  return session.modules.some((m) => m.name === name)
}

// ---------------------------------------------------------------- navigation

export const SECTIONS: Record<string, string> = {
  overview: 'Áttekintés',
  account: 'Fiók',
  admin: 'Adminisztráció',
  services: 'Szolgáltatások',
}

/** Services we plan to build. Shown greyed out until the backend module exists. */
export const PLANNED: (UiRoute & { module: string; blurb: string })[] = [
  {
    module: 'classroom',
    path: '/classroom',
    title: 'Monitorozás',
    icon: '🖥️',
    permission: null,
    section: 'services',
    blurb: 'A tanulói gépek képernyőjének megtekintése és zárolása termenként.',
  },
  {
    module: 'software',
    path: '/software',
    title: 'Programtelepítés',
    icon: '⏬',
    permission: null,
    section: 'services',
    blurb: 'Programok telepítése és eltávolítása Windows 10/11 gépekre, termenként.',
  },
]

export interface NavEntry extends UiRoute {
  planned: boolean
}

export function navSections(): { title: string; entries: NavEntry[] }[] {
  const entries: NavEntry[] = []
  for (const m of session.modules) {
    for (const r of m.ui_routes) {
      if (!r.permission || canAnywhere(r.permission)) entries.push({ ...r, planned: false })
    }
  }
  // "Coming soon" entries are for staff only; students shouldn't see admin tooling.
  for (const p of PLANNED) {
    if (!hasModule(p.module) && isStaff()) entries.push({ ...p, planned: true })
  }
  const order = Object.keys(SECTIONS)
  const bySection = new Map<string, NavEntry[]>()
  for (const e of entries) bySection.set(e.section, [...(bySection.get(e.section) ?? []), e])
  return [...bySection.entries()]
    .sort(([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
    .map(([s, list]) => ({ title: SECTIONS[s] ?? s, entries: list }))
}
