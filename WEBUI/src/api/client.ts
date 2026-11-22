// Thin fetch wrapper for /api/v1. Auth is the HttpOnly session cookie set by
// /auth/login, so there is no token handling here.

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(describe(status, detail))
  }
}

type Query = Record<string, string | number | boolean | null | undefined | string[]>

let onUnauthorized: () => void = () => {}
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}
let onPasswordChangeRequired: () => void = () => {}
export function setPasswordChangeHandler(fn: () => void) {
  onPasswordChangeRequired = fn
}

interface Options {
  query?: Query
  body?: unknown // JSON-encoded
  file?: Blob // sent as-is (uploads)
  fileType?: string
}

async function send(method: string, path: string, opts: Options = {}): Promise<Response> {
  const url = new URL('/api/v1' + path, window.location.origin)
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v === undefined || v === null || v === '') continue
    for (const item of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(item))
  }
  const headers: Record<string, string> = {}
  let body: BodyInit | undefined
  if (opts.file) {
    headers['content-type'] = opts.fileType || opts.file.type || 'application/octet-stream'
    body = opts.file
  } else if (opts.body !== undefined) {
    headers['content-type'] = 'application/json'
    body = JSON.stringify(opts.body)
  }
  const res = await fetch(url, {
    method,
    credentials: 'same-origin',
    cache: 'no-store', // always live data (dashboard polling, lists after edits)
    headers,
    body,
  })
  if (res.status === 401 && !path.startsWith('/auth/')) onUnauthorized()
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      detail = (await res.json()).detail
    } catch {
      /* not JSON */
    }
    // Safety net: an admin reset this user's password while they were using the app.
    if (res.status === 403 && detail === 'password change required') onPasswordChangeRequired()
    throw new ApiError(res.status, detail)
  }
  return res
}

async function request<T>(method: string, path: string, opts: Options = {}): Promise<T> {
  const res = await send(method, path, opts)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** GET a file and hand it to the browser as a download (keeps cookie auth and error handling). */
async function download(path: string, query?: Query, fallbackName = 'letoltes'): Promise<void> {
  const res = await send('GET', path, { query })
  const disposition = res.headers.get('content-disposition') ?? ''
  const name = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? fallbackName
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>('GET', path, { query }),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, { body: body ?? {} }),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, { body }),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, { body }),
  del: (path: string) => request<void>('DELETE', path),
  upload: <T>(path: string, file: Blob, fileType: string, query?: Query) =>
    request<T>('POST', path, { file, fileType, query }),
  download,
}

// ---------------------------------------------------------------- error text

interface FieldError {
  loc?: (string | number)[]
  msg?: string
}

function describe(status: number, detail: unknown): string {
  if (Array.isArray(detail)) {
    // FastAPI / pydantic validation errors
    return (detail as FieldError[])
      .map((e) => {
        const field = (e.loc ?? []).filter((p) => p !== 'body').join('.')
        return field ? `${field}: ${e.msg}` : (e.msg ?? '')
      })
      .join('; ')
  }
  const text = typeof detail === 'string' ? detail : ''
  switch (status) {
    case 401:
      return text || 'Nincs bejelentkezve.'
    case 403:
      return `Nincs jogosultságod ehhez a művelethez. (${text})`
    case 404:
      return text || 'Nem található.'
    case 409:
      return 'Ütközés: már létezik ilyen elem, vagy elérted a korlátot.'
    default:
      return text || `Hiba (${status})`
  }
}

export function errorText(e: unknown): string {
  if (e instanceof Error) return e.message
  return String(e)
}
