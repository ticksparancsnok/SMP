// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// The UI's version is [WEBUI] in ../Version.toml, the only place SMP's versions are
// written; it is compiled in as __SMP_WEBUI_VERSION__ (src/env.d.ts). SMP_VERSION_FILE
// overrides where the file is. Line-based like tools/version.sh: keep
// `version = "..."` on one line under the section header.
function smpVersion(section: string): string {
  const file = process.env.SMP_VERSION_FILE ?? fileURLToPath(new URL('../Version.toml', import.meta.url))
  let text: string
  try {
    text = readFileSync(file, 'utf8')
  } catch (e) {
    throw new Error(`can't read ${file} (${e}): the web UI's version comes from Version.toml in the SMP repository root; set SMP_VERSION_FILE to its path`)
  }
  let inSection = false
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (line.startsWith('[')) {
      inSection = line.slice(1).split(']')[0].trim() === section
      continue
    }
    const m = inSection ? /^version\s*=\s*"([^"]*)"/.exec(line) : null
    if (m) return m[1]
  }
  throw new Error(`${file}: no version = "..." under [${section}]`)
}

// In development the Vite server proxies API calls to the Python server, so the
// browser sees a single origin (session cookie + CSRF origin check just work).
const API = process.env.SMP_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  define: {
    __SMP_WEBUI_VERSION__: JSON.stringify(smpVersion('WEBUI')),
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API },
      '^/s/': { target: API }, // short-link redirects
      '/healthz': { target: API },
    },
  },
})
