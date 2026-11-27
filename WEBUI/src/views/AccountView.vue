<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Building, Room } from '../api/types'
import { passwordRules, roleLabel, roleScopeName } from '../format'
import { t } from '../i18n'
import { canAnywhere, session } from '../session'

const rooms = ref<Room[]>([])
const buildings = ref<Building[]>([])
const pw = reactive({ current: '', next: '', again: '' })
const msg = ref<{ ok: boolean; text: string } | null>(null)
const busy = ref(false)

onMounted(async () => {
  if (canAnywhere('core.rooms.read')) rooms.value = await api.get<Room[]>('/rooms').catch(() => [])
  if (canAnywhere('core.buildings.read')) buildings.value = await api.get<Building[]>('/buildings').catch(() => [])
})

// The building or room a role applies to ('' for the whole school or when it can't be looked up)
const scopeOf = (r: { room_id?: string | null; building_id?: string | null }) => roleScopeName(r, buildings.value, rooms.value)

async function changePassword() {
  msg.value = null
  if (pw.next !== pw.again) {
    msg.value = { ok: false, text: t('password.mismatch') }
    return
  }
  busy.value = true
  try {
    await api.post('/auth/password', { current_password: pw.current, new_password: pw.next })
    msg.value = { ok: true, text: t('account.changed') }
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
    <h1>{{ t('nav./account') }}</h1>
  </div>
  <div v-if="session.me" class="grid">
    <div class="card">
      <h2>{{ t('account.details') }}</h2>
      <table>
        <tbody>
          <tr><th>{{ t('common.username') }}</th><td>{{ session.me.username }}</td></tr>
          <tr><th>{{ t('common.name') }}</th><td>{{ session.me.display_name }}</td></tr>
          <tr><th>{{ t('account.school') }}</th><td>{{ session.me.org_name }}</td></tr>
          <tr>
            <th>{{ t('common.roles') }}</th>
            <td><div class="chips">
              <span v-for="(r, i) in session.me.roles" :key="i" class="badge info">
                {{ roleLabel(r.role) }}{{ scopeOf(r) ? ` – ${scopeOf(r)}` : '' }}
              </span>
            </div></td>
          </tr>
        </tbody>
      </table>
      <details style="margin-top: 12px">
        <summary>{{ t('common.permissions') }} ({{ session.me.permissions.length }})</summary>
        <div class="chips" style="margin-top: 8px">
          <span v-for="p in session.me.permissions" :key="p" class="badge mono">{{ p }}</span>
        </div>
      </details>
    </div>

    <form class="card stack" @submit.prevent="changePassword">
      <h2>{{ t('account.change_password') }}</h2>
      <div v-if="msg" class="alert" :class="msg.ok ? 'ok' : 'err'">{{ msg.text }}</div>
      <div class="field">
        <label>{{ t('account.current_password') }}</label>
        <input v-model="pw.current" type="password" autocomplete="current-password" required />
      </div>
      <div class="field">
        <label>{{ t('pwchange.new') }}</label>
        <input v-model="pw.next" type="password" autocomplete="new-password" minlength="8" required />
        <span class="muted small">{{ passwordRules() }}</span>
      </div>
      <div class="field">
        <label>{{ t('pwchange.again') }}</label>
        <input v-model="pw.again" type="password" autocomplete="new-password" minlength="8" required />
      </div>
      <button :disabled="busy">{{ t('common.save') }}</button>
    </form>
  </div>
</template>
