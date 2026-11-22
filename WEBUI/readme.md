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
  session.ts         current user, can()/canAnywhere(), menu built from module manifests
  router.ts          routes + permission guard
  App.vue            shell: sidebar, mobile top bar
  views/             one file per page
  components/        Modal, Pager, UserDialog, SchemaForm (settings forms from JSON schema)
```

`npm run build` typechecks and writes `dist/`. The server Docker image does this automatically and serves it.

## DNS blocklist page

The web UI includes `/dns` for users with `dns.blocklist.read`. Users with
`dns.blocklist.write` can:

- add an individual domain,
- delete an entry,
- preview a hosts-file import,
- confirm the import.

The frontend never sends `addedBy`. The control-plane API derives it from the
authenticated principal:

- manual entry: `ctx.principal.name`,
- API-token or service call: `token:<token name>` if no user principal exists,
- hosts import: `imported by: <username>`.

The API client sends raw file contents to the DNS preview/import endpoints;
the FastAPI handler performs normalization, loopback/system filtering and
deduplication.
