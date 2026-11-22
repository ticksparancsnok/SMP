<script setup lang="ts">
// Shown instead of the app while me.must_change_password is set (first login with a
// one-time or admin-set password, or after an admin reset). The server blocks every
// other API call until this succeeds.
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '../api/client'
import { PASSWORD_RULES } from '../format'
import { loadSession, logout, session } from '../session'

const route = useRoute()
const router = useRouter()
const pw = reactive({ current: '', next: '', again: '' })
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  if (pw.next !== pw.again) {
    error.value = 'A két új jelszó nem egyezik.'
    return
  }
  if (pw.next === pw.current) {
    error.value = 'Az új jelszó nem lehet ugyanaz, mint a jelenlegi.'
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
      <div>
        <h1>Új jelszó szükséges</h1>
        <p class="muted" style="margin: 0">
          Szia {{ session.me?.display_name || session.me?.username }}! A jelszavadat egy adminisztrátor állította be,
          ezért a folytatás előtt meg kell változtatnod.
        </p>
      </div>
      <div v-if="error" class="alert err">{{ error }}</div>
      <div class="field">
        <label for="cur">Kapott (egyszeri) jelszó</label>
        <input id="cur" v-model="pw.current" type="password" autocomplete="current-password" required autofocus />
      </div>
      <div class="field">
        <label for="new">Új jelszó</label>
        <input id="new" v-model="pw.next" type="password" autocomplete="new-password" minlength="8" required />
        <span class="muted small">{{ PASSWORD_RULES }}</span>
      </div>
      <div class="field">
        <label for="again">Új jelszó még egyszer</label>
        <input id="again" v-model="pw.again" type="password" autocomplete="new-password" minlength="8" required />
      </div>
      <button :disabled="busy">Jelszó mentése</button>
      <button type="button" class="ghost" @click="signOut">Kijelentkezés</button>
    </form>
  </div>
</template>
