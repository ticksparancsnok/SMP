<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api, errorText } from '../api/client'
import type { Building, Group, Page, Room, User } from '../api/types'
import ImportDialog from '../components/ImportDialog.vue'
import Modal from '../components/Modal.vue'
import OtpDisplay from '../components/OtpDisplay.vue'
import Pager from '../components/Pager.vue'
import UserDialog from '../components/UserDialog.vue'
import { fmtDate, fmtRelative, roleLabel, roleScopeName, ROLES } from '../format'
import { locale, t } from '../i18n'
import { usePageSize } from '../paging'
import { can, canAnywhere, session } from '../session'

const limit = usePageSize('users')
const users = ref<User[]>([])
const total = ref(0)
const offset = ref(0)
const q = ref('')
const role = ref('')
const groupFilter = ref('')
const rooms = ref<Room[]>([])
const buildings = ref<Building[]>([])
const groups = ref<Group[]>([])
const error = ref('')
const dialog = ref<{ user: User | null } | null>(null)
// A manager writes users of their buildings only (permissions exist per building); the
// server says per user whether this one may be changed (can_edit).
const canWrite = computed(() => canAnywhere('core.users.write'))
const canImport = computed(() => canAnywhere('core.users.write') && canAnywhere('core.groups.write'))
// Login slips: admins for everyone, class teachers for their own students (server filters).
const canSlips = computed(() => canWrite.value || can('core.users.reset_class_password'))
const importOpen = ref(false)

// Class teachers (not admins) can issue a new one-time password to their students;
// the server marks those rows with can_reset_password.
const issued = ref<{ username: string; otp: string; until: string | null } | null>(null)

async function teacherReset(u: User) {
  if (!confirm(t('users.confirm_reset', { name: u.display_name || u.username }))) return
  try {
    const r = await api.post<{ one_time_password: string | null; otp_expires_at: string | null }>(
      `/users/${u.id}/password`,
      {},
    )
    if (r.one_time_password) issued.value = { username: u.username, otp: r.one_time_password, until: r.otp_expires_at }
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}
const exporting = ref(false)

const exportMenu = ref<HTMLDetailsElement | null>(null)

/** Export exactly what the current filters show (all pages, not just this one).
 *  'list' = Név | Felhasználónév | Osztály; 'credentials' = the same plus each user's
 *  unused one-time password (login slips; admins only, audited). */
async function exportXlsx(kind: 'list' | 'credentials') {
  if (exportMenu.value) exportMenu.value.open = false
  exporting.value = true
  try {
    const path = kind === 'list' ? '/users/export.xlsx' : '/users/credentials.xlsx'
    // Column headers follow the UI language (import accepts either).
    await api.download(path, { q: q.value, role: role.value, group_id: groupFilter.value, lang: locale.value })
  } catch (e) {
    error.value = errorText(e)
  } finally {
    exporting.value = false
  }
}

function downloadSample() {
  api.download('/users/import-sample.xlsx', { lang: locale.value }).catch((e) => (error.value = errorText(e)))
}

function imported() {
  firstPage()
  if (canAnywhere('core.groups.read')) api.get<Group[]>('/groups').then((g) => (groups.value = g)).catch(() => {})
}


async function load() {
  try {
    const page = await api.get<Page<User>>('/users', {
      q: q.value,
      role: role.value,
      group_id: groupFilter.value,
      limit: limit.value,
      offset: offset.value,
    })
    users.value = page.items
    total.value = page.total
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}

/** Back to the first page after a filter change; the [limit, offset] watcher loads it. */
function firstPage() {
  if (offset.value) offset.value = 0
  else load()
}

let debounce: number | undefined
watch([q, role, groupFilter], () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(firstPage, 250)
})
// One watcher, so a page-size change (which also moves the offset) loads once.
watch([limit, offset], load)

onMounted(async () => {
  load()
  if (canAnywhere('core.rooms.read')) rooms.value = await api.get<Room[]>('/rooms').catch(() => [])
  if (canAnywhere('core.buildings.read')) buildings.value = await api.get<Building[]>('/buildings').catch(() => [])
  if (canAnywhere('core.groups.read')) groups.value = await api.get<Group[]>('/groups').catch(() => [])
})

async function remove(u: User) {
  if (!confirm(t('users.confirm_delete', { username: u.username }))) return
  try {
    await api.del(`/users/${u.id}`)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}

function saved() {
  dialog.value = null
  load()
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>{{ t('nav./users') }}</h1>
      <p>{{ t('users.subtitle') }}</p>
    </div>
    <div v-if="canWrite" class="row">
      <button @click="dialog = { user: null }">{{ t('users.new') }}</button>
    </div>
  </div>

  <div class="toolbar">
    <div class="field">
      <label>{{ t('common.search') }}</label>
      <input v-model="q" :placeholder="t('users.search_placeholder')" />
    </div>
    <div class="field">
      <label>{{ t('users.role') }}</label>
      <select v-model="role">
        <option value="">{{ t('common.all') }}</option>
        <option v-for="r in ROLES" :key="r" :value="r">{{ roleLabel(r) }}</option>
      </select>
    </div>
    <div v-if="groups.length" class="field">
      <label>{{ t('common.class_or_group') }}</label>
      <select v-model="groupFilter">
        <option value="">{{ t('common.all') }}</option>
        <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
      </select>
    </div>
    <span class="grow"></span>
    <details ref="exportMenu" class="menu">
      <summary class="button ghost" :aria-disabled="exporting">{{ t('users.export') }}</summary>
      <div class="menu-items card">
        <button type="button" class="ghost" @click="exportXlsx('list')">
          {{ t('users.export_list') }}
          <span class="muted small">{{ t('users.export_list_hint') }}</span>
        </button>
        <button v-if="canSlips" type="button" class="ghost" @click="exportXlsx('credentials')">
          {{ t('users.export_credentials') }}
          <span class="muted small">
            {{ t('users.export_credentials_hint') }}{{ canWrite ? '' : t('users.export_credentials_hint_own') }}
          </span>
        </button>
      </div>
    </details>
    <button v-if="canImport" class="ghost" :title="t('users.import_title')" @click="importOpen = true">
      {{ t('users.import') }}
    </button>
    <button class="ghost" :title="t('users.sample_title')" @click="downloadSample">{{ t('users.sample') }}</button>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('common.username') }}</th>
          <th>{{ t('common.name') }}</th>
          <th>{{ t('common.roles') }}</th>
          <th>{{ t('common.class_or_group') }}</th>
          <th>{{ t('common.status') }}</th>
          <th>{{ t('users.last_login') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="u in users" :key="u.id">
          <td class="mono">{{ u.username }}</td>
          <td>
            {{ u.display_name }}<br v-if="u.email" /><span class="muted small">{{ u.email }}</span>
          </td>
          <td><div class="chips">
            <span v-for="(r, i) in u.roles" :key="i" class="badge info">
              {{ roleLabel(r.role) }}{{ roleScopeName(r, buildings, rooms) ? ` – ${roleScopeName(r, buildings, rooms)}` : '' }}
            </span>
          </div></td>
          <td><div class="chips">
            <span v-for="g in u.groups" :key="g.id" class="badge">{{ g.name }}</span>
          </div></td>
          <td>
            <div class="chips">
              <span v-if="u.disabled" class="badge danger">{{ t('users.disabled') }}</span>
              <span v-else class="badge ok">{{ t('common.active') }}</span>
              <span
                v-if="u.otp_status === 'active'"
                class="badge info"
                :title="t('users.awaiting_activation_title', { until: fmtDate(u.otp_expires_at) })"
              >
                {{ t('users.awaiting_activation') }}
              </span>
              <span
                v-else-if="u.otp_status === 'expired'"
                class="badge danger"
                :title="t('users.otp_expired_title')"
              >
                {{ t('users.otp_expired') }}
              </span>
              <span
                v-else-if="u.must_change_password"
                class="badge warning"
                :title="t('users.must_change_title')"
              >
                {{ t('users.must_change') }}
              </span>
            </div>
          </td>
          <td class="muted">{{ fmtRelative(u.last_login_at) }}</td>
          <td>
            <div v-if="!canWrite && u.can_reset_password" class="actions">
              <button class="ghost small" @click="teacherReset(u)">{{ t('users.new_otp') }}</button>
            </div>
            <div v-if="canWrite" class="actions">
              <button
                class="ghost small"
                :disabled="!u.can_edit"
                :title="u.can_edit ? '' : t('code.users.not_editable')"
                @click="dialog = { user: u }"
              >{{ t('common.edit') }}</button>
              <button
                v-if="u.id !== session.me?.id"
                class="danger small"
                :disabled="!u.can_edit"
                :title="u.can_edit ? '' : t('code.users.not_editable')"
                @click="remove(u)"
              >{{ t('common.delete') }}</button>
            </div>
          </td>
        </tr>
        <tr v-if="!users.length">
          <td colspan="7" class="empty">{{ t('common.no_matches') }}</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="offset" v-model:limit="limit" :total="total" />
  </div>

  <Modal v-if="issued" :title="t('users.new_otp')" @close="issued = null">
    <OtpDisplay :username="issued.username" :otp="issued.otp" :until="issued.until" />
    <div class="modal-foot">
      <button @click="issued = null">{{ t('common.done') }}</button>
    </div>
  </Modal>
  <ImportDialog v-if="importOpen" :buildings="buildings" @close="importOpen = false" @imported="imported" />
  <UserDialog v-if="dialog" :user="dialog.user" :rooms="rooms" :groups="groups" :buildings="buildings"
    @changed="load"
    @close="dialog = null" @saved="saved" />
</template>
