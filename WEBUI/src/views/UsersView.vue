<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api, errorText } from '../api/client'
import type { Group, Page, Room, User } from '../api/types'
import ImportDialog from '../components/ImportDialog.vue'
import Modal from '../components/Modal.vue'
import OtpDisplay from '../components/OtpDisplay.vue'
import Pager from '../components/Pager.vue'
import UserDialog from '../components/UserDialog.vue'
import { fmtDate, fmtRelative, ROLE_LABELS } from '../format'
import { can, canAnywhere, session } from '../session'

const LIMIT = 50
const users = ref<User[]>([])
const total = ref(0)
const offset = ref(0)
const q = ref('')
const role = ref('')
const groupFilter = ref('')
const rooms = ref<Room[]>([])
const groups = ref<Group[]>([])
const error = ref('')
const dialog = ref<{ user: User | null } | null>(null)
const canWrite = computed(() => can('core.users.write'))
const canImport = computed(() => can('core.users.write') && can('core.groups.write'))
// Login slips: admins for everyone, class teachers for their own students (server filters).
const canSlips = computed(() => canWrite.value || can('core.users.reset_class_password'))
const importOpen = ref(false)

// Class teachers (not admins) can issue a new one-time password to their students;
// the server marks those rows with can_reset_password.
const issued = ref<{ username: string; otp: string; until: string | null } | null>(null)

async function teacherReset(u: User) {
  if (!confirm(`Új egyszeri jelszót generálsz ${u.display_name || u.username} számára? A jelenlegi jelszava megszűnik.`)) return
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
    await api.download(path, { q: q.value, role: role.value, group_id: groupFilter.value })
  } catch (e) {
    error.value = errorText(e)
  } finally {
    exporting.value = false
  }
}

function downloadSample() {
  api.download('/users/import-sample.xlsx').catch((e) => (error.value = errorText(e)))
}

function imported() {
  offset.value = 0
  load()
  if (can('core.groups.read')) api.get<Group[]>('/groups').then((g) => (groups.value = g)).catch(() => {})
}

const roomName = (id: string | null | undefined) => rooms.value.find((r) => r.id === id)?.name ?? '?'

async function load() {
  try {
    const page = await api.get<Page<User>>('/users', {
      q: q.value,
      role: role.value,
      group_id: groupFilter.value,
      limit: LIMIT,
      offset: offset.value,
    })
    users.value = page.items
    total.value = page.total
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}

let debounce: number | undefined
watch([q, role, groupFilter], () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(() => {
    offset.value = 0
    load()
  }, 250)
})
watch(offset, load)

onMounted(async () => {
  load()
  if (canAnywhere('core.rooms.read')) rooms.value = await api.get<Room[]>('/rooms').catch(() => [])
  if (can('core.groups.read')) groups.value = await api.get<Group[]>('/groups').catch(() => [])
})

async function remove(u: User) {
  if (!confirm(`Biztosan törlöd ezt a felhasználót: ${u.username}?`)) return
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
      <h1>Felhasználók</h1>
      <p>Tanulók, tanárok és adminisztrátorok</p>
    </div>
    <div v-if="canWrite" class="row">
      <button @click="dialog = { user: null }">+ Új felhasználó</button>
    </div>
  </div>

  <div class="toolbar">
    <div class="field">
      <label>Keresés</label>
      <input v-model="q" placeholder="név, felhasználónév, e-mail" />
    </div>
    <div class="field">
      <label>Szerepkör</label>
      <select v-model="role">
        <option value="">Mind</option>
        <option v-for="(label, key) in ROLE_LABELS" :key="key" :value="key">{{ label }}</option>
      </select>
    </div>
    <div v-if="groups.length" class="field">
      <label>Osztály / csoport</label>
      <select v-model="groupFilter">
        <option value="">Mind</option>
        <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
      </select>
    </div>
    <span class="grow"></span>
    <details ref="exportMenu" class="menu">
      <summary class="button ghost" :aria-disabled="exporting">⬇ Exportálás (.xlsx)</summary>
      <div class="menu-items card">
        <button type="button" class="ghost" @click="exportXlsx('list')">
          Felhasználólista
          <span class="muted small">Név, felhasználónév, osztály – a jelenlegi szűréssel</span>
        </button>
        <button v-if="canSlips" type="button" class="ghost" @click="exportXlsx('credentials')">
          Belépési adatok (egyszeri jelszavak)
          <span class="muted small">
            Belépőlapok nyomtatásához – csak a még nem aktivált fiókok{{ canWrite ? '' : ' (saját osztályaid diákjai)' }}
          </span>
        </button>
      </div>
    </details>
    <button v-if="canImport" class="ghost" title="Diákok importálása .xlsx fájlból" @click="importOpen = true">
      ⬆ Importálás
    </button>
    <button class="ghost" title="Minta fájl az importáláshoz" @click="downloadSample">Minta .xlsx</button>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>Felhasználónév</th>
          <th>Név</th>
          <th>Szerepkörök</th>
          <th>Osztály / csoport</th>
          <th>Állapot</th>
          <th>Utolsó belépés</th>
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
              {{ ROLE_LABELS[r.role] ?? r.role }}{{ r.room_id ? ` – ${roomName(r.room_id)}` : '' }}
            </span>
          </div></td>
          <td><div class="chips">
            <span v-for="g in u.groups" :key="g.id" class="badge">{{ g.name }}</span>
          </div></td>
          <td>
            <div class="chips">
              <span v-if="u.disabled" class="badge danger">Letiltva</span>
              <span v-else class="badge ok">Aktív</span>
              <span
                v-if="u.otp_status === 'active'"
                class="badge info"
                :title="`Egyszeri jelszóval még nem lépett be. Érvényes: ${fmtDate(u.otp_expires_at)}`"
              >
                Aktiválásra vár
              </span>
              <span
                v-else-if="u.otp_status === 'expired'"
                class="badge danger"
                title="Az egyszeri jelszó lejárt – a Szerkesztésben generálj újat"
              >
                Egyszeri jelszó lejárt
              </span>
              <span
                v-else-if="u.must_change_password"
                class="badge warning"
                title="Admin által beállított jelszó – következő belépéskor cserélni kell"
              >
                Jelszócsere
              </span>
            </div>
          </td>
          <td class="muted">{{ fmtRelative(u.last_login_at) }}</td>
          <td>
            <div v-if="!canWrite && u.can_reset_password" class="actions">
              <button class="ghost small" @click="teacherReset(u)">Új egyszeri jelszó</button>
            </div>
            <div v-if="canWrite" class="actions">
              <button class="ghost small" @click="dialog = { user: u }">Szerkesztés</button>
              <button v-if="u.id !== session.me?.id" class="danger small" @click="remove(u)">Törlés</button>
            </div>
          </td>
        </tr>
        <tr v-if="!users.length">
          <td colspan="7" class="empty">Nincs találat.</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="offset" :total="total" :limit="LIMIT" />
  </div>

  <Modal v-if="issued" title="Új egyszeri jelszó" @close="issued = null">
    <OtpDisplay :username="issued.username" :otp="issued.otp" :until="issued.until" />
    <div class="modal-foot">
      <button @click="issued = null">Kész</button>
    </div>
  </Modal>
  <ImportDialog v-if="importOpen" @close="importOpen = false" @imported="imported" />
  <UserDialog v-if="dialog" :user="dialog.user" :rooms="rooms" :groups="groups"
    @changed="load"
    @close="dialog = null" @saved="saved" />
</template>
