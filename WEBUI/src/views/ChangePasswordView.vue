<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Shown instead of the app while me.must_change_password is set (first login with a
// one-time or admin-set password, or after an admin reset). The server blocks every
// other API call until this succeeds.
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '../api/client'
import LangSwitch from '../components/LangSwitch.vue'
import { passwordRules } from '../format'
import { t } from '../i18n'
import { loadSession, logout, session } from '../session'

const route = useRoute()
const router = useRouter()
const pw = reactive({ current: '', next: '', again: '' })
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  if (pw.next !== pw.again) {
    error.value = t('password.mismatch')
    return
  }
  if (pw.next === pw.current) {
    error.value = t('code.auth.password_unchanged')
    return
  }
  busy.value = true
  try {
    await api.post('/auth/password', { current_password: pw.current, new_password: pw.next })
    await loadSession()
    const next = typeof route.query.next === 'string' && route.query.next.startsWith('/') ? route.query.next : '/'
    router.replace(next)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function signOut() {
  await logout()
  router.push('/login')
}
</script>

<template>
  <div class="auth">
    <form class="card stack" @submit.prevent="submit">
      <!-- own row: the title's length differs per language and would move it -->
      <div class="auth-lang"><LangSwitch /></div>
      <h1>{{ t('pwchange.title') }}</h1>
      <p class="muted" style="margin: 0">
        {{ t('pwchange.intro', { name: session.me?.display_name || session.me?.username || '' }) }}
      </p>
      <div v-if="error" class="alert err">{{ error }}</div>
      <div class="field">
        <label for="cur">{{ t('pwchange.current') }}</label>
        <input id="cur" v-model="pw.current" type="password" autocomplete="current-password" required autofocus />
      </div>
      <div class="field">
        <label for="new">{{ t('pwchange.new') }}</label>
        <input id="new" v-model="pw.next" type="password" autocomplete="new-password" minlength="8" required />
        <span class="muted small">{{ passwordRules() }}</span>
      </div>
      <div class="field">
        <label for="again">{{ t('pwchange.again') }}</label>
        <input id="again" v-model="pw.again" type="password" autocomplete="new-password" minlength="8" required />
      </div>
      <button :disabled="busy">{{ t('pwchange.submit') }}</button>
      <button type="button" class="ghost" @click="signOut">{{ t('common.sign_out') }}</button>
    </form>
  </div>
</template>
