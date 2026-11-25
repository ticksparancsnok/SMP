// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

// Paging shared by every list page. Lists the server pages itself (users, devices,
// DNS blocklist, audit) keep their own offset ref and use usePageSize() + <Pager>;
// small lists the server returns whole use usePage(), so only the visible rows render.
//
// Each list has a key ("users", "dns.blocklist", ...). The rows-per-page chosen for it
// is saved on the user's account (PATCH /auth/preferences), so it follows them to any
// browser; see PreferencesOut in server/smp/core/api/auth.py.
import { computed, ref, shallowRef, toValue, watch, type MaybeRefOrGetter, type Ref, type WatchSource } from 'vue'
import { savePreferences, session } from './session'

export const PAGE_SIZES = [10, 50, 100, 500] as const
export const DNS_PAGE_SIZES = [10, 50, 100] as const
export type PageSize = (typeof PAGE_SIZES)[number]
export const DEFAULT_PAGE_SIZE: PageSize = 50

const isPageSize = (n: unknown, sizes: readonly PageSize[]): n is PageSize => sizes.includes(n as PageSize)

// ---------------------------------------------------------------- saved page sizes

/** Rows per page for one list: starts from the user's saved choice and saves changes. */
export function usePageSize(key: string, sizes: readonly PageSize[] = PAGE_SIZES): Ref<number> {
  const saved = session.me?.preferences.page_size[key]
  const size = ref<number>(isPageSize(saved, sizes) ? saved : DEFAULT_PAGE_SIZE)
  watch(size, (v) => {
    if (isPageSize(v, sizes) && session.me?.preferences.page_size[key] !== v) savePreferences({ page_size: { [key]: v } })
  })
  return size
}

// ---------------------------------------------------------------- client-side paging

export interface ClientPage<T> {
  /** Rows per page (v-model:limit on <Pager>). */
  limit: number
  /** Index of the first visible row (v-model:offset on <Pager>). */
  offset: number
  readonly total: number
  /** The rows on the current page: render these, not the whole list. */
  readonly rows: readonly T[]
}

/** Client-side paging over a list already in memory.
 *  `key`: names the list for the saved page size.
 *  `resetOn`: sources (e.g. a search box) that send the view back to the first page. */
export function usePage<T>(
  items: MaybeRefOrGetter<readonly T[]>,
  { key, resetOn = [] }: { key: string; resetOn?: WatchSource[] },
): ClientPage<T> {
  const source = computed(() => toValue(items))
  const limit = usePageSize(key)
  const offset = ref(0)
  const total = computed(() => source.value.length)
  const rows = computed(() => source.value.slice(offset.value, offset.value + limit.value))

  // After deletes or a narrower filter, stay on the last page that still has rows.
  watch(total, (n) => {
    if (offset.value >= n) offset.value = Math.max(0, Math.floor((n - 1) / limit.value) * limit.value)
  })
  if (resetOn.length) watch(resetOn, () => (offset.value = 0))

  return {
    get limit() {
      return limit.value
    },
    set limit(v: number) {
      limit.value = v
    },
    get offset() {
      return offset.value
    },
    set offset(v: number) {
      offset.value = v
    },
    get total() {
      return total.value
    },
    get rows() {
      return rows.value
    },
  }
}

// ---------------------------------------------------------------- server-side paging

export interface ServerPage<T> extends ClientPage<T> {
  readonly loading: boolean
  readonly error: string
  /** Fetch the current page again (e.g. after a change). */
  reload(): Promise<void>
}

/** Paging done by the server (`fetchPage` gets limit/offset, returns {items, total}).
 *  `resetOn`: filters; a change goes back to the first page after a short pause (typing).
 *  Call reload() for the first load: nothing is fetched until then. */
export function useServerPage<T>(
  fetchPage: (limit: number, offset: number) => Promise<{ items: T[]; total: number }>,
  { key, resetOn = [], onError = String }: { key: string; resetOn?: WatchSource[]; onError?: (e: unknown) => string },
): ServerPage<T> {
  const limit = usePageSize(key)
  const offset = ref(0)
  const rows = shallowRef<T[]>([])
  const total = ref(0)
  const loading = ref(false)
  const error = ref('')
  let seq = 0

  async function reload() {
    const mine = ++seq
    loading.value = true
    try {
      const page = await fetchPage(limit.value, offset.value)
      if (mine !== seq) return // a newer request was made meanwhile
      if (!page.items.length && offset.value > 0) {
        // the last rows of the last page are gone: show the page before
        offset.value = Math.max(0, offset.value - limit.value)
        return
      }
      rows.value = page.items
      total.value = page.total
      error.value = ''
    } catch (e) {
      if (mine === seq) error.value = onError(e)
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  watch([limit, offset], reload)
  let debounce: number | undefined
  if (resetOn.length)
    watch(resetOn, () => {
      window.clearTimeout(debounce)
      debounce = window.setTimeout(() => {
        if (offset.value) offset.value = 0 // the watcher above reloads
        else reload()
      }, 250)
    })

  return {
    get limit() {
      return limit.value
    },
    set limit(v: number) {
      limit.value = v
    },
    get offset() {
      return offset.value
    },
    set offset(v: number) {
      offset.value = v
    },
    get total() {
      return total.value
    },
    get rows() {
      return rows.value
    },
    get loading() {
      return loading.value
    },
    get error() {
      return error.value
    },
    reload,
  }
}
