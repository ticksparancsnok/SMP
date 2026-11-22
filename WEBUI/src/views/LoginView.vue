<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '../api/client'
import { loadSession } from '../session'

const route = useRoute()
const router = useRouter()
const form = reactive({ username: '', password: '' })
const error = ref('')
const busy = ref(false)

async function submit() {
  busy.value = true
  error.value = ''
  try {
    await api.post('/auth/login', form)
    await loadSession()
    const next = typeof route.query.next === 'string' && route.query.next.startsWith('/') ? route.query.next : '/'
    router.replace(next)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth">
    <form class="card stack" @submit.prevent="submit">
      <div>
        <h1>SMP</h1>
        <p class="muted" style="margin: 0">School Management Portal – bejelentkezés</p>
      </div>
      <div v-if="error" class="alert err">{{ error }}</div>
      <div class="field">
        <label for="u">Felhasználónév</label>
        <input id="u" v-model="form.username" autocomplete="username" required autofocus />
      </div>
      <div class="field">
        <label for="p">Jelszó</label>
        <input id="p" v-model="form.password" type="password" autocomplete="current-password" required />
      </div>
      <button :disabled="busy">{{ busy ? 'Belépés…' : 'Belépés' }}</button>
    </form>
  </div>
</template>
