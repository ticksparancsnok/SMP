// Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
// License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
// Attribution required in modified versions: see NOTICE

import { createApp } from 'vue'
import App from './App.vue'
import { setPasswordChangeHandler, setUnauthorizedHandler } from './api/client'
import './assets/main-style.css'
import './assets/app.css'
import router from './router'
import { loadSession, session } from './session'

// Session expired or revoked while using the app -> back to the login page.
setUnauthorizedHandler(() => {
  session.me = null
  const here = router.currentRoute.value
  if (!here.meta.public) router.push({ path: '/login', query: { next: here.fullPath } })
})

// The server says this user must replace their password first.
setPasswordChangeHandler(async () => {
  await loadSession()
  router.push('/change-password')
})

createApp(App).use(router).mount('#app')
