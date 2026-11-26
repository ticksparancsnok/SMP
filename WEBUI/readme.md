# SMP web UI

Vue 3 + TypeScript + Vite. Uses the original SMP layout and stylesheet (`src/assets/main-style.css`); additions are in `src/assets/app.css`.

## Development

Run the API server first (see `../server/README.md`, port 8000), then:

```sh
npm install
npm run dev          # http://localhost:5173, proxies /api and /s/ to :8000
```

Set `SMP_API=http://host:port` to proxy to another server. Because of the proxy the browser sees one origin, so the session cookie and the server's CSRF origin check work without CORS.

## After backend API changes

```sh
(cd ../server && smp openapi -o ../WEBUI/openapi.json)
npm run gen:api      # regenerates src/api/schema.d.ts
npm run typecheck    # shows every place that no longer matches the API
```

## Layout

```
src/
  api/client.ts      fetch wrapper (cookie auth, error messages)
  api/types.ts       friendly aliases for the generated OpenAPI types
  session.ts         current user, can()/canInBuilding()/canAnywhere(), menu built from module manifests
  router.ts          routes + permission guard
  App.vue            shell: sidebar, mobile top bar
  views/             one file per page
  components/        Modal, Pager, UserDialog, SchemaForm (settings forms from JSON schema)
```

`npm run build` typechecks and writes `dist/`. The server Docker image does this automatically and serves it.

The UI has no version in `package.json`: `vite.config.ts` compiles in `[WEBUI]` of
`../Version.toml` (the one file holding all SMP versions) as `__SMP_WEBUI_VERSION__`
(declared in `src/env.d.ts`). The sidebar shows the server's version, with both in its
tooltip.

## Languages (HU / EN)

The UI is Hungarian or English, switched with the HU | EN toggle on the login page,
the password-change page and in the sidebar. The choice is remembered per browser
(`localStorage`), and Hungarian is the default. The API itself is English-only.

- `src/i18n/index.ts`: `t(key, params)`, `tOr(key, fallback, params)`, the reactive `locale`
- `src/i18n/en.ts`: the reference dictionary; `hu.ts` must have exactly the same keys
  (`npm run typecheck` fails otherwise)
- A message is a string with `{param}` placeholders, or a function when the wording
  depends on the values (plurals, optional parts)
- Text from the server is translated by stable keys (menu titles by route path, dashboard
  cards by widget id, errors by `code`, ...) and falls back to the server's English.
  See "Languages" in `../docs/writing-a-module.md` for the key list.
- User .xlsx downloads (export, login slips, import sample) send `lang` with the current
  language, so the column headers match the UI (Name | Username | Class or
  Név | Felhasználónév | Osztály). Import accepts either.

## DNS blocklist page

The web UI includes `/dns` for users with `dns.blocklist.read`, with a Blocklist and
an Allowlist tab. The allowlist is checked first, then the blocklist; everything else
is allowed. Public suffixes (com, hu, co.uk, …) are built in and can't go on either
list. Users with `dns.blocklist.write` can:

- add or delete blocklist and allowlist entries,
- import a hosts file: it is uploaded once with a progress bar, checked on the server
  (progress again), previewed as new / already listed / skipped (allowlisted domains and
  public suffixes), paged from the server, then confirmed; saving and sending the list to
  the sinkhole show their progress too, and finish on the server if the dialog is closed.

Blocklist entries the allowlist overrides are shown as "not applied".

The frontend never sends `addedBy`. The control-plane API derives it from the
authenticated principal:

- manual entry: `ctx.principal.name`,
- API-token or service call: `token:<token name>` if no user principal exists,
- hosts import: `imported by: <username>`.

The upload goes through `api.uploadWithProgress()` (XMLHttpRequest: fetch can't report
upload progress) as the raw file; the server does the normalization, loopback/system
filtering and deduplication.
