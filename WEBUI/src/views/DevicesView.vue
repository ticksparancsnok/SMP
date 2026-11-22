<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Device, DeviceStatus, Page, Room } from '../api/types'
import Modal from '../components/Modal.vue'
import Pager from '../components/Pager.vue'
import { DEVICE_STATUS, fmtRelative, isOnline } from '../format'
import { can } from '../session'

const LIMIT = 100
const route = useRoute()
const router = useRouter()

const devices = ref<Device[]>([])
const total = ref(0)
const offset = ref(0)
const rooms = ref<Room[]>([])
const error = ref('')
// '' = all, '__none' = no room, otherwise a room id. Kept in the URL so dashboard/room links work.
const roomFilter = ref(typeof route.query.room_id === 'string' ? route.query.room_id : '')
const q = ref('')
const canWrite = computed(() => can('core.devices.write'))

const roomName = (id: string | null | undefined) => (id ? (rooms.value.find((r) => r.id === id)?.name ?? '?') : '–')

async function load() {
  try {
    const page = await api.get<Page<Device>>('/devices', {
      room_id: roomFilter.value && roomFilter.value !== '__none' ? roomFilter.value : undefined,
      unassigned: roomFilter.value === '__none' ? true : undefined,
      q: q.value,
      limit: LIMIT,
      offset: offset.value,
    })
    devices.value = page.items
    total.value = page.total
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}

let debounce: number | undefined
watch(q, () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(() => {
    offset.value = 0
    load()
  }, 250)
})
watch(roomFilter, (v) => {
  router.replace({ query: v ? { room_id: v } : {} })
  offset.value = 0
  load()
})
watch(offset, load)

onMounted(async () => {
  load()
  rooms.value = await api.get<Room[]>('/rooms').catch(() => [])
})

// ---------------------------------------------------------------- create / edit
const edit = ref<Device | null>(null)
const open = ref(false)
const form = reactive({ hostname: '', display_name: '', mac: '', room_id: '', notes: '', status: 'pending' as DeviceStatus })
const formError = ref('')

function openEdit(d: Device | null) {
  edit.value = d
  Object.assign(form, {
    hostname: d?.hostname ?? '',
    display_name: d?.display_name ?? '',
    mac: d?.mac ?? '',
    room_id: d?.room_id ?? (roomFilter.value !== '__none' ? roomFilter.value : ''),
    notes: d?.notes ?? '',
    status: (d?.status ?? 'pending') as DeviceStatus,
  })
  formError.value = ''
  open.value = true
}

async function save() {
  const common = {
    display_name: form.display_name,
    mac: form.mac || null,
    room_id: form.room_id || null,
    notes: form.notes,
  }
  try {
    if (edit.value) await api.patch(`/devices/${edit.value.id}`, { ...common, status: form.status })
    else await api.post('/devices', { ...common, hostname: form.hostname })
    open.value = false
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function remove(d: Device) {
  if (!confirm(`Biztosan törlöd a gépet: ${d.hostname}?`)) return
  try {
    await api.del(`/devices/${d.id}`)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}

// ---------------------------------------------------------------- effective policies
const policiesOf = ref<Device | null>(null)
const effective = ref<Record<string, unknown> | null>(null)

async function showPolicies(d: Device) {
  policiesOf.value = d
  effective.value = null
  try {
    effective.value = await api.get<Record<string, unknown>>(`/devices/${d.id}/effective-policies`)
  } catch (e) {
    effective.value = { error: errorText(e) }
  }
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>Gépek</h1>
      <p>A gépek az ügynök telepítésekor automatikusan megjelennek; itt előre is felvehetők.</p>
    </div>
    <button v-if="canWrite" @click="openEdit(null)">+ Gép felvétele</button>
  </div>

  <div class="toolbar">
    <div class="field">
      <label>Terem</label>
      <select v-model="roomFilter">
        <option value="">Minden terem</option>
        <option value="__none">Terem nélkül</option>
        <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
      </select>
    </div>
    <div class="field">
      <label>Keresés</label>
      <input v-model="q" placeholder="gépnév vagy MAC" />
    </div>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>Gép</th>
          <th>Terem</th>
          <th>MAC / IP</th>
          <th>Állapot</th>
          <th>Utoljára látva</th>
          <th>OS</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.id">
          <td>
            <strong class="mono">{{ d.hostname }}</strong>
            <br v-if="d.display_name" /><span class="muted small">{{ d.display_name }}</span>
          </td>
          <td>{{ roomName(d.room_id) }}</td>
          <td class="mono">
            {{ d.mac ?? '–' }}<br /><span class="muted">{{ d.last_ip ?? '' }}</span>
          </td>
          <td>
            <span class="badge" :class="DEVICE_STATUS[d.status]?.cls">{{ DEVICE_STATUS[d.status]?.label ?? d.status }}</span>
          </td>
          <td><span class="dot" :class="{ on: isOnline(d.last_seen_at) }"></span>{{ fmtRelative(d.last_seen_at) }}</td>
          <td class="muted small">{{ [d.os_name, d.os_version].filter(Boolean).join(' ') || '–' }}</td>
          <td>
            <div class="actions">
              <button class="ghost small" @click="showPolicies(d)">Szabályok</button>
              <template v-if="canWrite">
                <button class="ghost small" @click="openEdit(d)">Szerkesztés</button>
                <button class="danger small" @click="remove(d)">Törlés</button>
              </template>
            </div>
          </td>
        </tr>
        <tr v-if="!devices.length">
          <td colspan="7" class="empty">Nincs gép.</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="offset" :total="total" :limit="LIMIT" />
  </div>

  <Modal v-if="open" :title="edit ? `Gép: ${edit.hostname}` : 'Gép felvétele'" @close="open = false">
    <form class="stack" @submit.prevent="save">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="row">
        <div class="field">
          <label>Gépnév (hostname)</label>
          <input v-model="form.hostname" :disabled="!!edit" required class="mono" placeholder="LAB1-PC01" />
        </div>
        <div class="field">
          <label>Megjelenített név</label>
          <input v-model="form.display_name" placeholder="pl. Tanári gép" />
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>MAC cím</label>
          <input v-model="form.mac" class="mono" placeholder="aa:bb:cc:dd:ee:ff" />
        </div>
        <div class="field">
          <label>Terem</label>
          <select v-model="form.room_id">
            <option value="">– nincs –</option>
            <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
          </select>
        </div>
      </div>
      <div v-if="edit" class="field">
        <label>Állapot</label>
        <select v-model="form.status">
          <option v-for="(s, key) in DEVICE_STATUS" :key="key" :value="key">{{ s.label }}</option>
        </select>
      </div>
      <div class="field">
        <label>Megjegyzés</label>
        <textarea v-model="form.notes" style="min-height: 60px"></textarea>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="open = false">Mégse</button>
        <button>Mentés</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="policiesOf" :title="`Érvényes szabályok: ${policiesOf.hostname}`" wide @close="policiesOf = null">
    <p class="muted small">Ami most érvényes erre a gépre, modulonként, és hogy melyik hozzárendelésből jön.</p>
    <p v-if="effective && !Object.keys(effective).length" class="empty">Nincs érvényes szabály.</p>
    <pre v-else class="json">{{ effective ? JSON.stringify(effective, null, 2) : 'Betöltés…' }}</pre>
  </Modal>
</template>
