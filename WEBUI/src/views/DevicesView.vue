<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '../api/client'
import type { AgentDownload, ApproveIn, Device, DeviceStatus, JoinRequest, Page, Room } from '../api/types'
import { copyText } from '../clipboard'
import Modal from '../components/Modal.vue'
import Pager from '../components/Pager.vue'
import RemotePicker from '../components/RemotePicker.vue'
import { DEVICE_STATUSES, deviceStatusClass, deviceStatusLabel, fmtFingerprint, fmtRelative, isOnline } from '../format'
import { t } from '../i18n'
import { usePage, usePageSize } from '../paging'
import { can } from '../session'

const limit = usePageSize('devices')

const route = useRoute()
const router = useRouter()

// shallowRef avoids deep reactivity overhead on large API responses
const devices = shallowRef<Device[]>([])
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
      limit: limit.value,
      offset: offset.value,
    })
    devices.value = page.items
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
watch(q, () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(firstPage, 250)
})
watch(roomFilter, (v) => {
  router.replace({ query: v ? { room_id: v } : {} })
  firstPage()
})
// One watcher, so a page-size change (which also moves the offset) loads once.
watch([limit, offset], load)

onMounted(async () => {
  load()
  loadJoinRequests()
  // the agent version SMP offers: PCs on another one are about to update themselves
  if (canWrite.value) download.value = await api.get<AgentDownload>('/agent/download').catch(() => null)
  rooms.value = await api.get<Room[]>('/rooms').catch(() => [])
  document.addEventListener('visibilitychange', onVisibility)
  refresher = window.setInterval(refresh, REFRESH_MS)
})
onBeforeUnmount(() => {
  window.clearInterval(refresher)
  document.removeEventListener('visibilitychange', onVisibility)
})

// Agents report every ~25 s (IP, room "by network", user, online), and new PCs ask
// to join: keep the page current while it's visible, like the home page does.
const REFRESH_MS = 10_000
let refresher: number | undefined
function refresh() {
  if (document.hidden) return
  load()
  loadJoinRequests()
}
const onVisibility = () => document.hidden || refresh()

// ---------------------------------------------------------------- create / edit
const edit = ref<Device | null>(null)
const open = ref(false)
const form = reactive({
  hostname: '',
  display_name: '',
  mac: '',
  room_id: '',
  room_mode: 'fixed' as 'fixed' | 'network',
  notes: '',
  status: 'pending' as DeviceStatus,
})
const formError = ref('')

function openEdit(d: Device | null) {
  edit.value = d
  Object.assign(form, {
    hostname: d?.hostname ?? '',
    display_name: d?.display_name ?? '',
    mac: d?.mac ?? '',
    room_id: d?.room_id ?? (roomFilter.value !== '__none' ? roomFilter.value : ''),
    room_mode: (d?.room_mode ?? 'fixed') as 'fixed' | 'network',
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
    if (edit.value) await api.patch(`/devices/${edit.value.id}`, { ...common, status: form.status, room_mode: form.room_mode })
    else await api.post('/devices', { ...common, hostname: form.hostname })
    open.value = false
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function resetIdentity(d: Device) {
  if (!confirm(t('devices.confirm_reset_identity', { hostname: d.hostname }))) return
  try {
    await api.post(`/agent/devices/${d.id}/reset-identity`)
    open.value = false
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

// ---------------------------------------------------------------- the agent download (built into SMP)
const download = ref<AgentDownload | null>(null)
const downloadOpen = ref(false)
const copied = ref('')
// what an admin types on the PC: this very server's address (with its port, if any)
const installCommand = computed(() => `smp-agent.exe install --server ${window.location.host}`)
const verifyCommand = 'Get-FileHash .\\smp-agent.exe'

/** The agent version SMP offers, if it differs from what `d` runs (it updates itself). */
function updatingTo(d: Device): string | null {
  const offered = download.value?.available ? download.value.version : null
  return offered && d.agent_version && d.agent_version !== offered ? offered : null
}

async function openDownload() {
  downloadOpen.value = true
  copied.value = ''
  download.value = await api.get<AgentDownload>('/agent/download').catch(() => null)
}

async function copy(what: string, text: string) {
  if (await copyText(text)) copied.value = what
}

const fmtSize = (bytes: number | null | undefined) => (bytes ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : '')

// ---------------------------------------------------------------- join requests (agents asking to be managed)
const joinRequests = shallowRef<JoinRequest[]>([])
const joinPage = usePage(joinRequests, { key: 'devices.join_requests' })
const approving = ref<JoinRequest | null>(null)
const approveForm = reactive({
  as: 'new' as 'new' | 'existing',
  device_id: '',
  room_mode: 'fixed' as 'fixed' | 'network',
  room_id: '',
  replace_online: false,
})
const approveError = ref('')
// The suggested device (same MAC/hostname), preselected in "approve as an existing device".
const suggested = shallowRef<Device | null>(null)
/** Matches for the device picker: searched on the server, never the whole fleet. */
const searchDevices = (q: string, limit: number) => api.get<Page<Device>>('/devices', { q, limit })
const deviceLabel = (d: Device) =>
  `${d.hostname}${d.mac ? ` (${d.mac})` : ''}${d.id === approving.value?.suggested_device_id ? ' ★' : ''}`

// Machines an admin rejected: remembered by key and MAC until "allow again".
const rejected = shallowRef<JoinRequest[]>([])

async function loadJoinRequests() {
  if (!canWrite.value) return
  try {
    ;[joinRequests.value, rejected.value] = await Promise.all([
      api.get<JoinRequest[]>('/agent/join-requests'),
      api.get<JoinRequest[]>('/agent/join-requests/rejected'),
    ])
  } catch (e) {
    error.value = errorText(e)
  }
}

async function allowAgain(r: JoinRequest) {
  try {
    await api.post(`/agent/join-requests/${r.id}/allow`)
    loadJoinRequests()
  } catch (e) {
    error.value = errorText(e)
  }
}

// The device chosen in "approve as an existing device", to tell whether its own agent
// is online right now: then this request is more likely another machine pretending to
// be it (same hostname/MAC) than a reinstall, and replacing it needs a clear yes.
const chosen = shallowRef<Device | null>(null)
watch(
  () => approveForm.device_id,
  async (id) => {
    chosen.value = !id ? null : id === suggested.value?.id ? suggested.value : await api.get<Device>(`/devices/${id}`).catch(() => null)
    approveForm.replace_online = false
  },
)
const chosenOnline = computed(
  () => approveForm.as === 'existing' && !!chosen.value?.agent_id && isOnline(chosen.value.last_seen_at),
)

async function openApprove(r: JoinRequest) {
  approving.value = r
  approveError.value = ''
  suggested.value = null
  if (r.suggested_device_id)
    suggested.value = await api.get<Device>(`/devices/${r.suggested_device_id}`).catch(() => null)
  Object.assign(approveForm, {
    // Not preselected when that PC is online: approving would cut it off (see above).
    as: r.suggested_device_id && !r.suggested_device_online ? 'existing' : 'new',
    device_id: r.suggested_device_id ?? '',
    room_mode: 'fixed',
    room_id: '',
    replace_online: false,
  })
}

async function approve() {
  const r = approving.value!
  const body: ApproveIn = {
    device_id: approveForm.as === 'existing' ? approveForm.device_id || null : null,
    room_mode: approveForm.room_mode,
    room_id: approveForm.room_mode === 'fixed' ? approveForm.room_id || null : null,
    replace_online: chosenOnline.value && approveForm.replace_online,
  }
  try {
    await api.post(`/agent/join-requests/${r.id}/approve`, body)
    approving.value = null
    loadJoinRequests()
    load()
  } catch (e) {
    approveError.value = errorText(e)
  }
}

async function reject(r: JoinRequest) {
  if (!confirm(t('devices.confirm_reject', { hostname: r.hostname }))) return
  try {
    await api.post(`/agent/join-requests/${r.id}/reject`)
    loadJoinRequests()
  } catch (e) {
    error.value = errorText(e)
  }
}

async function remove(d: Device) {
  if (!confirm(t('devices.confirm_delete', { hostname: d.hostname }))) return
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
      <h1>{{ t('nav./devices') }}</h1>
      <p>{{ t('devices.subtitle') }}</p>
    </div>
    <div v-if="canWrite" class="actions">
      <button class="ghost" @click="openDownload">{{ t('devices.download_agent') }}</button>
      <button @click="openEdit(null)">{{ t('devices.new') }}</button>
    </div>
  </div>

  <div class="toolbar">
    <div class="field">
      <label>{{ t('common.room') }}</label>
      <select v-model="roomFilter">
        <option value="">{{ t('common.all_rooms') }}</option>
        <option value="__none">{{ t('devices.no_room') }}</option>
        <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
      </select>
    </div>
    <div class="field">
      <label>{{ t('common.search') }}</label>
      <input v-model="q" :placeholder="t('devices.search_placeholder')" />
    </div>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>

  <!-- machines whose agent asks to be managed: compare the fingerprint with the PC's screen -->
  <div v-if="joinRequests.length" class="card">
    <h2>{{ t('devices.join_title', { count: joinRequests.length }) }}</h2>
    <p class="muted small">{{ t('devices.join_intro') }}</p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{{ t('devices.device') }}</th>
            <th>MAC / IP</th>
            <th>OS</th>
            <th>{{ t('devices.fingerprint') }}</th>
            <th>{{ t('devices.requested') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in joinPage.rows" :key="r.id">
            <td><strong class="mono">{{ r.hostname }}</strong></td>
            <td class="mono">
              {{ r.mac ?? '–' }}<br /><span class="muted">{{ r.ip ?? '' }}</span>
              <template v-if="r.observed_ip && r.observed_ip !== r.ip">
                <br /><span class="muted small" :title="t('devices.via_title')">{{ t('devices.via', { ip: r.observed_ip }) }}</span>
              </template>
            </td>
            <td class="muted small">{{ [r.os_name, r.os_version].filter(Boolean).join(' ') || '–' }}</td>
            <td class="mono">{{ r.fingerprint_display }}</td>
            <td class="small">{{ fmtRelative(r.created_at) }}</td>
            <td>
              <div class="actions">
                <button class="small" @click="openApprove(r)">{{ t('devices.approve') }}</button>
                <button class="danger small" @click="reject(r)">{{ t('devices.reject') }}</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <Pager v-model:offset="joinPage.offset" v-model:limit="joinPage.limit" :total="joinPage.total" />
    </div>
  </div>

  <!-- rejected machines can't ask again (not after an IP change or a reinstall either) -->
  <details v-if="rejected.length" class="card rejected">
    <summary>
      <strong>{{ t('devices.rejected_title', { count: rejected.length }) }}</strong>
      <span class="muted small"> · {{ t('devices.rejected_intro') }}</span>
    </summary>
    <div class="table-wrap">
      <table>
        <tbody>
          <tr v-for="r in rejected" :key="r.id">
            <td><strong class="mono">{{ r.hostname }}</strong></td>
            <td class="mono">{{ r.mac ?? '–' }}<br /><span class="muted">{{ r.ip ?? '' }}</span></td>
            <td class="mono">{{ r.fingerprint_display }}</td>
            <td class="small">{{ t('devices.rejected_when', { when: fmtRelative(r.rejected_at) }) }}</td>
            <td>
              <div class="actions">
                <button class="ghost small" @click="allowAgain(r)">{{ t('devices.allow_again') }}</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('devices.device') }}</th>
          <th>{{ t('common.room') }}</th>
          <th>MAC / IP</th>
          <th>{{ t('common.status') }}</th>
          <th>{{ t('devices.last_seen') }}</th>
          <th>OS</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.id">
          <td>
            <strong class="mono">{{ d.hostname }}</strong>
            <br v-if="d.display_name" /><span class="muted small">{{ d.display_name }}</span>
            <template v-if="d.logged_in_user && isOnline(d.last_seen_at)">
              <br /><span class="muted small">👤 {{ d.logged_in_user }}</span>
            </template>
          </td>
          <td>
            {{ roomName(d.room_id) }}
            <span v-if="d.room_mode === 'network'" class="badge info" :title="t('devices.room_network_title')">
              {{ t('devices.room_network_badge') }}
            </span>
          </td>
          <td class="mono">
            {{ d.mac ?? '–' }}<br /><span class="muted">{{ d.last_ip ?? '' }}</span>
            <template v-if="d.observed_ip && d.observed_ip !== d.last_ip">
              <br /><span class="muted small" :title="t('devices.via_title')">{{ t('devices.via', { ip: d.observed_ip }) }}</span>
            </template>
          </td>
          <td>
            <span class="badge" :class="deviceStatusClass(d.status)">{{ deviceStatusLabel(d.status) }}</span>
          </td>
          <td><span class="dot" :class="{ on: isOnline(d.last_seen_at) }"></span>{{ fmtRelative(d.last_seen_at) }}</td>
          <td class="muted small">
            {{ [d.os_name, d.os_version].filter(Boolean).join(' ') || '–' }}
            <template v-if="d.agent_version">
              <br />{{ t('devices.agent_version', { version: d.agent_version }) }}
              <span v-if="updatingTo(d)" class="badge info" :title="t('devices.agent_updating_title', { version: updatingTo(d) ?? '' })">
                {{ t('devices.agent_updating', { version: updatingTo(d) ?? '' }) }}
              </span>
            </template>
          </td>
          <td>
            <div class="actions">
              <button class="ghost small" @click="showPolicies(d)">{{ t('devices.policies') }}</button>
              <template v-if="canWrite">
                <button class="ghost small" @click="openEdit(d)">{{ t('common.edit') }}</button>
                <button class="danger small" @click="remove(d)">{{ t('common.delete') }}</button>
              </template>
            </div>
          </td>
        </tr>
        <tr v-if="!devices.length">
          <td colspan="7" class="empty">{{ t('devices.empty') }}</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="offset" v-model:limit="limit" :total="total" />
  </div>

  <Modal v-if="open" :title="edit ? t('devices.edit_title', { hostname: edit.hostname }) : t('devices.new_title')" @close="open = false">
    <form class="stack" @submit.prevent="save">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('devices.hostname') }}</label>
          <input v-model="form.hostname" :disabled="!!edit" required class="mono" placeholder="LAB1-PC01" />
        </div>
        <div class="field">
          <label>{{ t('common.display_name') }}</label>
          <input v-model="form.display_name" :placeholder="t('devices.display_name_placeholder')" />
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>{{ t('devices.mac') }}</label>
          <input v-model="form.mac" class="mono" placeholder="aa:bb:cc:dd:ee:ff" />
        </div>
        <div class="field">
          <label>{{ t('common.room') }}</label>
          <select v-model="form.room_id" :disabled="form.room_mode === 'network'">
            <option value="">{{ t('devices.none') }}</option>
            <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
          </select>
        </div>
      </div>
      <div v-if="edit?.agent_id" class="field">
        <label>{{ t('devices.room_mode') }}</label>
        <select v-model="form.room_mode">
          <option value="fixed">{{ t('devices.room_fixed') }}</option>
          <option value="network">{{ t('devices.room_network') }}</option>
        </select>
      </div>
      <div v-if="edit?.agent_id" class="field">
        <label>{{ t('devices.agent') }}</label>
        <span class="small">
          <span class="mono">{{ fmtFingerprint(edit.agent_id) }}</span> · v{{ edit.agent_version ?? '?' }}
          <span v-if="updatingTo(edit)" class="badge info" :title="t('devices.agent_updating_title', { version: updatingTo(edit) ?? '' })">
            {{ t('devices.agent_updating', { version: updatingTo(edit) ?? '' }) }}
          </span>
          <template v-if="edit.logged_in_user && isOnline(edit.last_seen_at)"> · 👤 {{ edit.logged_in_user }}</template>
        </span>
        <div><button type="button" class="ghost small" @click="resetIdentity(edit)">{{ t('devices.reset_identity') }}</button></div>
      </div>
      <div v-if="edit" class="field">
        <label>{{ t('common.status') }}</label>
        <select v-model="form.status">
          <option v-for="s in DEVICE_STATUSES" :key="s" :value="s">{{ deviceStatusLabel(s) }}</option>
        </select>
      </div>
      <div class="field">
        <label>{{ t('devices.notes') }}</label>
        <textarea v-model="form.notes" style="min-height: 60px"></textarea>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="open = false">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="policiesOf" :title="t('devices.effective_title', { hostname: policiesOf.hostname })" wide @close="policiesOf = null">
    <p class="muted small">{{ t('devices.effective_intro') }}</p>
    <p v-if="effective && !Object.keys(effective).length" class="empty">{{ t('devices.no_effective') }}</p>
    <pre v-else class="json">{{ effective ? JSON.stringify(effective, null, 2) : t('common.loading') }}</pre>
  </Modal>

  <Modal v-if="approving" :title="t('devices.approve_title', { hostname: approving.hostname })" @close="approving = null">
    <form class="stack" @submit.prevent="approve">
      <div v-if="approveError" class="alert err">{{ approveError }}</div>
      <div class="field">
        <label>{{ t('devices.fingerprint') }}</label>
        <span class="mono" style="font-size: 20px; letter-spacing: 0.05em">{{ approving.fingerprint_display }}</span>
        <span class="muted small">{{ t('devices.compare_fingerprint') }}</span>
      </div>
      <div v-if="approving.suggested_device_online" class="alert err">
        {{ t('devices.online_lookalike', { hostname: suggested?.hostname ?? approving.hostname }) }}
      </div>
      <div class="field">
        <label>{{ t('devices.approve_as') }}</label>
        <label class="check"><input v-model="approveForm.as" type="radio" value="new" /> {{ t('devices.approve_new') }}</label>
        <label class="check"><input v-model="approveForm.as" type="radio" value="existing" /> {{ t('devices.approve_existing') }}</label>
        <RemotePicker
          v-if="approveForm.as === 'existing'"
          v-model="approveForm.device_id"
          :search="searchDevices"
          :label="deviceLabel"
          :initial="suggested"
          required
        />
        <span v-if="approveForm.as === 'existing'" class="muted small">{{ t('devices.approve_existing_hint') }}</span>
        <label v-if="chosenOnline" class="check replace-online">
          <input v-model="approveForm.replace_online" type="checkbox" required />
          {{ t('devices.replace_online', { hostname: chosen?.hostname ?? '' }) }}
        </label>
      </div>
      <div class="field">
        <label>{{ t('devices.room_mode') }}</label>
        <label class="check"><input v-model="approveForm.room_mode" type="radio" value="fixed" /> {{ t('devices.room_fixed') }}</label>
        <select v-if="approveForm.room_mode === 'fixed'" v-model="approveForm.room_id">
          <option value="">{{ approveForm.as === 'existing' ? t('devices.room_keep') : t('devices.none') }}</option>
          <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
        </select>
        <label class="check"><input v-model="approveForm.room_mode" type="radio" value="network" /> {{ t('devices.room_network') }}</label>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="approving = null">{{ t('common.cancel') }}</button>
        <button>{{ t('devices.approve') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="downloadOpen" :title="t('devices.download_title')" wide @close="downloadOpen = false">
    <p v-if="!download" class="muted">{{ t('common.loading') }}</p>
    <div v-else-if="!download.available" class="alert err">{{ t('devices.download_missing') }}</div>
    <div v-else class="stack">
      <div class="spread">
        <a class="button" :href="download.url" download>⬇ smp-agent.exe</a>
        <span class="muted small">
          {{ t('devices.download_version', { version: download.version ?? '?' }) }} · {{ fmtSize(download.size) }}
        </span>
      </div>
      <ol class="steps">
        <li>{{ t('devices.download_step_copy') }}</li>
        <li>
          {{ t('devices.download_step_install') }}
          <div class="cmd">
            <code class="mono">{{ installCommand }}</code>
            <button class="ghost small" @click="copy('install', installCommand)">
              {{ copied === 'install' ? t('common.copied') : t('common.copy') }}
            </button>
          </div>
        </li>
        <li>{{ t('devices.download_step_approve') }}</li>
      </ol>
      <div class="field">
        <label>SHA-256</label>
        <code class="mono small sha">{{ download.sha256 }}</code>
        <span class="muted small">
          {{ t('devices.download_verify') }}
          <code class="mono">{{ verifyCommand }}</code>
        </span>
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.steps {
  margin: 0;
  padding-left: 20px;
  display: grid;
  gap: 10px;
}
.cmd {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.cmd code {
  flex: 1;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow-x: auto;
  white-space: nowrap;
}
.sha {
  word-break: break-all;
}
.replace-online {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid var(--danger, #e5484d);
  border-radius: 8px;
}
details.rejected summary {
  cursor: pointer;
}
a.button {
  /* a link that looks like the primary button (main-style.css, button) */
  display: inline-block;
  background: var(--accent);
  color: #fff;
  border-radius: 8px;
  padding: 9px 14px;
  text-decoration: none;
}
a.button:hover {
  filter: brightness(1.1);
}
</style>