// UI language (Hungarian / English). The API speaks English; everything the user
// reads is translated here:
//   - text written in the UI:            t('users.title')
//   - text the server sends, by a stable key, falling back to the server's English:
//       menu titles by route path        tOr(`nav.${path}`, route.title)
//       dashboard cards by widget id     tOr(`widget.${id}`, widget.title)
//       errors by code (see api/client)  tOr(`code.${code}`, detail, params)
//
// en.ts is the reference dictionary; hu.ts must have exactly the same keys
// (vue-tsc fails otherwise). A message is a string with {param} placeholders, or a
// function when the wording depends on the values (plurals, optional parts).
import { ref, watch } from 'vue'
import en from './en'
import hu from './hu'

export type Locale = 'hu' | 'en'
export type Params = Record<string, string | number | undefined>
export type Message = string | ((p: Params) => string)
export type MessageKey = keyof typeof en
export type Dictionary = Record<MessageKey, Message>

export const LOCALES: { code: Locale; label: string; name: string }[] = [
  { code: 'hu', label: 'HU', name: 'Magyar' },
  { code: 'en', label: 'EN', name: 'English' },
]

const DICTIONARIES: Record<Locale, Dictionary> = { hu, en }
const STORAGE_KEY = 'smp.locale'
const DEFAULT: Locale = 'hu'

function saved(): Locale {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'hu' || v === 'en') return v
  } catch {
    // storage blocked (private mode, policy): just use the default
  }
  return DEFAULT
}

/** The current UI language. Reactive: changing it re-renders every t() call. */
export const locale = ref<Locale>(saved())

watch(
  locale,
  (l) => {
    try {
      localStorage.setItem(STORAGE_KEY, l)
    } catch {
      /* not remembered, still switched */
    }
    document.documentElement.lang = l
  },
  { immediate: true },
)

function render(m: Message, params: Params): string {
  if (typeof m === 'function') return m(params)
  return m.replace(/\{(\w+)\}/g, (all, name: string) => (params[name] === undefined ? all : String(params[name])))
}

/** Translate a UI string. */
export function t(key: MessageKey, params: Params = {}): string {
  return render(DICTIONARIES[locale.value][key] ?? en[key], params)
}

/** Translate a key built at runtime (from server data); `fallback` when there is no translation. */
export function tOr(key: string, fallback: string, params: Params = {}): string {
  const dict = DICTIONARIES[locale.value] as Record<string, Message>
  return key in dict ? render(dict[key]!, params) : fallback
}

/** BCP 47 tag for Intl date/number formatting in the current language. */
export function intlLocale(): string {
  return locale.value === 'hu' ? 'hu-HU' : 'en-GB'
}
