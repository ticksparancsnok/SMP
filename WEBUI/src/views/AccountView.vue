<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Room } from '../api/types'
import { PASSWORD_RULES, ROLE_LABELS } from '../format'
import { canAnywhere, session } from '../session'

const rooms = ref<Room[]>([])
const pw = reactive({ current: '', next: '', again: '' })
const msg = ref<{ ok: boolean; text: string } | null>(null)
const busy = ref(false)

onMounted(async () => {
  if (canAnywhere('core.rooms.read')) rooms.value = await api.get<Room[]>('/rooms').catch(() => [])
})

const roomName = (id: string) => rooms.value.find((r) => r.id === id)?.name ?? id

async function changePassword() {
  msg.value = null
  if (pw.next !== pw.again) {
    msg.value = { ok: false, text: 'A két új jelszó nem egyezik.' }
    return
  }
  busy.value = true
  try {
    await api.post('/auth/password', { current_password: pw.current, new_password: pw.next })
    msg.value = { ok: true, text: 'Jelszó megváltoztatva. A többi eszközön kijelentkeztettünk.' }
    pw.current = pw.next = pw.again = ''
  } catch (e) {
    msg.value = { ok: false, text: errorText(e) }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page-head">
    <h1>Saját fiók</h1>
  </div>
  <div v-if="session.me" class="grid">
    <div class="card">
      <h2>Adatok</h2>
      <table>
        <tbody>
          <tr><th>Felhasználónév</th><td>{{ session.me.username }}</td></tr>
          <tr><th>Név</th><td>{{ session.me.display_name }}</td></tr>
          <tr><th>Iskola</th><td>{{ session.me.org_name }}</td></tr>
          <tr>
            <th>Szerepkörök</th>
            <td><div class="chips">
              <span v-for="(r, i) in session.me.roles" :key="i" class="badge info">
                {{ ROLE_LABELS[r.role ?? ''] ?? r.role }}{{ r.room_id ? ` – ${roomName(r.room_id)}` : '' }}
              </span>
            </div></td>
          </tr>
        </tbody>
      </table>
      <details style="margin-top: 12px">
        <summary>Jogosultságok ({{ session.me.permissions.length }})</summary>
        <div class="chips" style="margin-top: 8px">
          <span v-for="p in session.me.permissions" :key="p" class="badge mono">{{ p }}</span>
        </div>
      </details>
    </div>

    <form class="card stack" @submit.prevent="changePassword">
      <h2>Jelszó módosítása</h2>
      <div v-if="msg" class="alert" :class="msg.ok ? 'ok' : 'err'">{{ msg.text }}</div>
      <div class="field">
        <label>Jelenlegi jelszó</label>
        <input v-model="pw.current" type="password" autocomplete="current-password" required />
      </div>
      <div class="field">
        <label>Új jelszó</label>
        <input v-model="pw.next" type="password" autocomplete="new-password" minlength="8" required />
        <span class="muted small">{{ PASSWORD_RULES }}</span>
      </div>
      <div class="field">
        <label>Új jelszó még egyszer</label>
        <input v-model="pw.again" type="password" autocomplete="new-password" minlength="8" required />
      </div>
      <button :disabled="busy">Mentés</button>
    </form>
  </div>
</template>
