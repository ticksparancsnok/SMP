<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '../api/client'
import LangSwitch from '../components/LangSwitch.vue'
import LegalNotice from '../components/LegalNotice.vue'
import { t } from '../i18n'
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
      <div class="spread" style="align-items: flex-start">
        <div>
          <h1>SMP</h1>
          <p class="muted" style="margin: 0">{{ t('login.subtitle') }}</p>
        </div>
        <LangSwitch />
      </div>
      <div v-if="error" class="alert err">{{ error }}</div>
      <div class="field">
        <label for="u">{{ t('common.username') }}</label>
        <input id="u" v-model="form.username" autocomplete="username" required autofocus />
      </div>
      <div class="field">
        <label for="p">{{ t('common.password') }}</label>
        <input id="p" v-model="form.password" type="password" autocomplete="current-password" required />
      </div>
      <button :disabled="busy">{{ busy ? t('login.submitting') : t('login.submit') }}</button>
      <LegalNotice />
    </form>
  </div>
</template>
