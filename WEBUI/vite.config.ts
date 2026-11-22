// SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
// SPDX-License-Identifier: AGPL-3.0-or-later
// Attribution required in modified versions: see NOTICE

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// In development the Vite server proxies API calls to the Python server, so the
// browser sees a single origin (session cookie + CSRF origin check just work).
const API = process.env.SMP_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API },
      '^/s/': { target: API }, // short-link redirects
      '/healthz': { target: API },
    },
  },
})
