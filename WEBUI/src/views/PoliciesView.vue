<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Assignment, Device, Group, Page, Policy, Room, Schedule, TargetType, User } from '../api/types'
import Modal from '../components/Modal.vue'
import { DAYS, describeWindows, fmtDate, fromLocalInput, TARGET_TYPES } from '../format'
import { can, canAnywhere, session } from '../session'

const route = useRoute()
const tab = computed(() => (route.query.tab as string) || 'assignments')
const TABS = { assignments: 'Hozzárendelések', policies: 'Szabályok', schedules: 'Időbeosztások' }

const policies = ref<Policy[]>([])
const schedules = ref<Schedule[]>([])
const assignments = ref<Assignment[]>([])
const rooms = ref<Room[]>([])
const groups = ref<Group[]>([])
const devices = ref<Device[]>([])
const users = ref<User[]>([])
const error = ref('')

const canWrite = computed(() => can('core.policies.write'))
const canAssign = computed(() => canAnywhere('core.policies.assign'))
// Policy kinds = modules that declare a policy schema (dns, software, classroom, ...).
const kinds = computed(() => session.modules.filter((m) => m.policy_schema))

async function load() {
  try {
    ;[policies.value, schedules.value, assignments.value] = await Promise.all([
      api.get<Policy[]>('/policies'),
      api.get<Schedule[]>('/schedules'),
      api.get<Assignment[]>('/policy-assignments'),
    ])
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}

onMounted(async () => {
  await load()
  // Lookup lists for target names; each is optional depending on permissions.
  const opt = <T,>(p: Promise<T>, fallback: T) => p.catch(() => fallback)
  ;[rooms.value, groups.value] = await Promise.all([
    opt(api.get<Room[]>('/rooms'), []),
    can('core.groups.read') ? opt(api.get<Group[]>('/groups'), []) : [],
  ])
  devices.value = (await opt(api.get<Page<Device>>('/devices', { limit: 2000 }), { items: [], total: 0 })).items
  if (can('core.users.read')) users.value = (await opt(api.get<Page<User>>('/users', { limit: 1000 }), { items: [], total: 0 })).items
})

function targetName(a: Assignment): string {
  const id = a.target_id
  switch (a.target_type) {
    case 'org':
      return 'Egész iskola'
    case 'room':
      return rooms.value.find((x) => x.id === id)?.name ?? id ?? ''
    case 'group':
      return groups.value.find((x) => x.id === id)?.name ?? id ?? ''
    case 'device':
      return devices.value.find((x) => x.id === id)?.hostname ?? id ?? ''
    case 'user':
      return users.value.find((x) => x.id === id)?.username ?? id ?? ''
  }
  return id ?? ''
}
const policyOf = (id: string) => policies.value.find((p) => p.id === id)
const scheduleName = (id: string | null | undefined) => schedules.value.find((s) => s.id === id)?.name

function isActiveNow(a: Assignment): boolean {
  const now = Date.now()
  return a.enabled && (!a.starts_at || Date.parse(a.starts_at) <= now) && (!a.ends_at || Date.parse(a.ends_at) > now)
}

async function del(path: string, what: string) {
  if (!confirm(`Biztosan törlöd: ${what}?`)) return
  try {
    await api.del(path)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}

// ---------------------------------------------------------------- policy dialog
const policyDlg = ref<{ policy: Policy | null } | null>(null)
const pForm = reactive({ kind: '', name: '', description: '', data: '{}' })
const pError = ref('')

function openPolicy(p: Policy | null) {
  Object.assign(pForm, {
    kind: p?.kind ?? kinds.value[0]?.name ?? '',
    name: p?.name ?? '',
    description: p?.description ?? '',
    data: JSON.stringify(p?.data ?? {}, null, 2),
  })
  pError.value = ''
  policyDlg.value = { policy: p }
}

async function savePolicy() {
  let data: unknown
  try {
    data = JSON.parse(pForm.data)
  } catch {
    pError.value = 'A beállítások mező nem érvényes JSON.'
    return
  }
  try {
    const p = policyDlg.value!.policy
    if (p) await api.patch(`/policies/${p.id}`, { name: pForm.name, description: pForm.description, data })
    else await api.post('/policies', { kind: pForm.kind, name: pForm.name, description: pForm.description, data })
    policyDlg.value = null
    load()
  } catch (e) {
    pError.value = errorText(e)
  }
}

// ---------------------------------------------------------------- schedule dialog
interface WindowForm {
  days: number[]
  start: string
  end: string
}
const scheduleDlg = ref<{ schedule: Schedule | null } | null>(null)
const sForm = reactive({ name: '', timezone: 'Europe/Budapest', windows: [] as WindowForm[] })
const sError = ref('')

function openSchedule(s: Schedule | null) {
  Object.assign(sForm, {
    name: s?.name ?? '',
    timezone: s?.timezone ?? 'Europe/Budapest',
    windows: (s?.windows as WindowForm[] | undefined)?.map((w) => ({ ...w, days: [...w.days] })) ?? [
      { days: [0, 1, 2, 3, 4], start: '08:00', end: '08:45' },
    ],
  })
  sError.value = ''
  scheduleDlg.value = { schedule: s }
}

function toggleDay(w: WindowForm, d: number) {
  w.days = w.days.includes(d) ? w.days.filter((x) => x !== d) : [...w.days, d].sort()
}

async function saveSchedule() {
  try {
    const s = scheduleDlg.value!.schedule
    if (s) await api.put(`/schedules/${s.id}`, sForm)
    else await api.post('/schedules', sForm)
    scheduleDlg.value = null
    load()
  } catch (e) {
    sError.value = errorText(e)
  }
}

// ---------------------------------------------------------------- assignment dialog
const assignOpen = ref(false)
const aForm = reactive({
  policy_id: '',
  target_type: 'room' as TargetType,
  target_id: '',
  schedule_id: '',
  priority: 0,
  starts_at: '',
  ends_at: '',
  label: '',
})
const aError = ref('')

const targetOptions = computed<{ id: string; name: string }[]>(() => {
  switch (aForm.target_type) {
    case 'room':
      return rooms.value.map((r) => ({ id: r.id, name: r.name }))
    case 'group':
      return groups.value.map((g) => ({ id: g.id, name: g.name }))
    case 'device':
      return devices.value.map((d) => ({ id: d.id, name: d.hostname }))
    case 'user':
      return users.value.map((u) => ({ id: u.id, name: `${u.display_name} (${u.username})` }))
  }
  return []
})

/** Clear the target, or pick it when there is only one choice (e.g. a teacher with one room). */
function resetTarget() {
  const opts = targetOptions.value
  aForm.target_id = opts.length === 1 ? opts[0]!.id : ''
}
// Covers manual changes and the exam preset switching e.g. device -> room, which
// would otherwise keep a device id under target_type "room".
watch(() => aForm.target_type, resetTarget)

function openAssign() {
  Object.assign(aForm, {
    policy_id: policies.value[0]?.id ?? '',
    target_type: can('core.policies.assign') ? 'org' : 'room',
    target_id: '',
    schedule_id: '',
    priority: 0,
    starts_at: '',
    ends_at: '',
    label: '',
  })
  resetTarget()
  aError.value = ''
  assignOpen.value = true
}

/** Preset: high-priority override for a room that ends after `minutes`. */
function examPreset(minutes: number) {
  aForm.target_type = 'room'
  aForm.priority = 100
  aForm.label = 'vizsgamód'
  aForm.starts_at = ''
  const end = new Date(Date.now() + minutes * 60_000)
  const pad = (n: number) => String(n).padStart(2, '0')
  aForm.ends_at = `${end.getFullYear()}-${pad(end.getMonth() + 1)}-${pad(end.getDate())}T${pad(end.getHours())}:${pad(end.getMinutes())}`
}

async function saveAssign() {
  try {
    await api.post('/policy-assignments', {
      policy_id: aForm.policy_id,
      target_type: aForm.target_type,
      target_id: aForm.target_type === 'org' ? null : aForm.target_id || null,
      schedule_id: aForm.schedule_id || null,
      priority: aForm.priority,
      starts_at: fromLocalInput(aForm.starts_at),
      ends_at: fromLocalInput(aForm.ends_at),
      label: aForm.label,
    })
    assignOpen.value = false
    load()
  } catch (e) {
    aError.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head">
    <h1>Szabályok</h1>
    <p>
      Szabály = egy modul beállításcsomagja (pl. DNS szűrés). Hozzárendeléssel érvényes az iskolára, teremre, csoportra
      vagy gépre – időbeosztással vagy határidővel is. Ütközésnél a nagyobb prioritás, majd a konkrétabb cél nyer.
    </p>
  </div>

  <nav class="tabs">
    <RouterLink v-for="(label, key) in TABS" :key="key" :to="{ query: { tab: key } }" :class="{ active: tab === key }">
      {{ label }}
    </RouterLink>
  </nav>

  <div v-if="error" class="alert err">{{ error }}</div>

  <!-- ------------------------------------------------------------ assignments -->
  <template v-if="tab === 'assignments'">
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canAssign" :disabled="!policies.length" @click="openAssign">+ Hozzárendelés</button>
    </div>
    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Szabály</th>
            <th>Cél</th>
            <th>Időbeosztás</th>
            <th>Prioritás</th>
            <th>Időszak</th>
            <th>Állapot</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in assignments" :key="a.id">
            <td>
              <strong>{{ policyOf(a.policy_id)?.name ?? '?' }}</strong>
              <span class="badge" style="margin-left: 6px">{{ policyOf(a.policy_id)?.kind }}</span>
              <br v-if="a.label" /><span class="muted small">{{ a.label }}</span>
            </td>
            <td>{{ TARGET_TYPES[a.target_type] }}: {{ targetName(a) }}</td>
            <td>{{ scheduleName(a.schedule_id) ?? '–' }}</td>
            <td>{{ a.priority }}</td>
            <td class="small">{{ fmtDate(a.starts_at) }} → {{ fmtDate(a.ends_at) }}</td>
            <td>
              <span v-if="isActiveNow(a)" class="badge ok">Érvényes</span>
              <span v-else class="badge closed">Inaktív</span>
            </td>
            <td>
              <div v-if="canAssign" class="actions">
                <button class="danger small" @click="del(`/policy-assignments/${a.id}`, 'a hozzárendelést')">Törlés</button>
              </div>
            </td>
          </tr>
          <tr v-if="!assignments.length">
            <td colspan="7" class="empty">Nincs hozzárendelés.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ------------------------------------------------------------ policies -->
  <template v-if="tab === 'policies'">
    <div v-if="!kinds.length" class="alert ok">
      Még egyik modul sem definiál szabálytípust. A DNS, monitorozás és programtelepítés modulok fognak – addig itt nem
      hozható létre szabály.
    </div>
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canWrite" :disabled="!kinds.length" @click="openPolicy(null)">+ Új szabály</button>
    </div>
    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Név</th>
            <th>Típus</th>
            <th>Leírás</th>
            <th>Módosítva</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in policies" :key="p.id">
            <td><strong>{{ p.name }}</strong></td>
            <td><span class="badge">{{ p.kind }}</span></td>
            <td class="muted">{{ p.description }}</td>
            <td class="muted">{{ fmtDate(p.updated_at) }}</td>
            <td>
              <div v-if="canWrite" class="actions">
                <button class="ghost small" @click="openPolicy(p)">Szerkesztés</button>
                <button class="danger small" @click="del(`/policies/${p.id}`, p.name)">Törlés</button>
              </div>
            </td>
          </tr>
          <tr v-if="!policies.length">
            <td colspan="5" class="empty">Nincs szabály.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ------------------------------------------------------------ schedules -->
  <template v-if="tab === 'schedules'">
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canWrite" @click="openSchedule(null)">+ Új időbeosztás</button>
    </div>
    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Név</th>
            <th>Idősávok</th>
            <th>Időzóna</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in schedules" :key="s.id">
            <td><strong>{{ s.name }}</strong></td>
            <td class="small">{{ describeWindows(s.windows) }}</td>
            <td class="muted">{{ s.timezone }}</td>
            <td>
              <div v-if="canWrite" class="actions">
                <button class="ghost small" @click="openSchedule(s)">Szerkesztés</button>
                <button class="danger small" @click="del(`/schedules/${s.id}`, s.name)">Törlés</button>
              </div>
            </td>
          </tr>
          <tr v-if="!schedules.length">
            <td colspan="4" class="empty">Nincs időbeosztás.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ------------------------------------------------------------ dialogs -->
  <Modal v-if="policyDlg" :title="policyDlg.policy ? 'Szabály szerkesztése' : 'Új szabály'" wide @close="policyDlg = null">
    <form class="stack" @submit.prevent="savePolicy">
      <div v-if="pError" class="alert err">{{ pError }}</div>
      <div class="row">
        <div class="field">
          <label>Típus (modul)</label>
          <select v-model="pForm.kind" :disabled="!!policyDlg.policy">
            <option v-for="k in kinds" :key="k.name" :value="k.name">{{ k.title }} ({{ k.name }})</option>
          </select>
        </div>
        <div class="field">
          <label>Név</label>
          <input v-model="pForm.name" required />
        </div>
      </div>
      <div class="field">
        <label>Leírás</label>
        <input v-model="pForm.description" />
      </div>
      <div class="field">
        <label>Beállítások (JSON – a modul ellenőrzi)</label>
        <textarea v-model="pForm.data" class="mono" style="min-height: 180px"></textarea>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="policyDlg = null">Mégse</button>
        <button>Mentés</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="scheduleDlg" :title="scheduleDlg.schedule ? 'Időbeosztás szerkesztése' : 'Új időbeosztás'" wide @close="scheduleDlg = null">
    <form class="stack" @submit.prevent="saveSchedule">
      <div v-if="sError" class="alert err">{{ sError }}</div>
      <div class="row">
        <div class="field">
          <label>Név</label>
          <input v-model="sForm.name" required placeholder="pl. Tanórák" />
        </div>
        <div class="field">
          <label>Időzóna</label>
          <input v-model="sForm.timezone" required />
        </div>
      </div>
      <div class="field">
        <label>Idősávok (éjfélen átnyúló sáv is megadható, pl. 22:00–06:00)</label>
        <div class="stack" style="gap: 8px">
          <div v-for="(w, i) in sForm.windows" :key="i" class="row" style="align-items: center">
            <div class="chips">
              <label v-for="(d, di) in DAYS" :key="di" class="check badge" :class="{ info: w.days.includes(di) }">
                <input type="checkbox" :checked="w.days.includes(di)" style="display: none" @change="toggleDay(w, di)" />{{ d }}
              </label>
            </div>
            <input v-model="w.start" type="time" required style="width: 110px" />
            <span>–</span>
            <input v-model="w.end" type="time" required style="width: 110px" />
            <button type="button" class="ghost small" @click="sForm.windows.splice(i, 1)">✕</button>
          </div>
          <div>
            <button type="button" class="ghost small" @click="sForm.windows.push({ days: [0, 1, 2, 3, 4], start: '08:00', end: '08:45' })">
              + Idősáv
            </button>
          </div>
        </div>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="scheduleDlg = null">Mégse</button>
        <button>Mentés</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="assignOpen" title="Új hozzárendelés" wide @close="assignOpen = false">
    <form class="stack" @submit.prevent="saveAssign">
      <div v-if="aError" class="alert err">{{ aError }}</div>
      <div class="row">
        <span class="muted small">Gyors beállítás:</span>
        <button type="button" class="ghost small" @click="examPreset(45)">Vizsgamód 45 percre</button>
        <button type="button" class="ghost small" @click="examPreset(90)">Vizsgamód 90 percre</button>
      </div>
      <div class="field">
        <label>Szabály</label>
        <select v-model="aForm.policy_id" required>
          <option v-for="p in policies" :key="p.id" :value="p.id">{{ p.name }} ({{ p.kind }})</option>
        </select>
      </div>
      <div class="row">
        <div class="field">
          <label>Cél típusa</label>
          <select v-model="aForm.target_type">
            <option v-for="(label, key) in TARGET_TYPES" :key="key" :value="key">{{ label }}</option>
          </select>
        </div>
        <div v-if="aForm.target_type !== 'org'" class="field">
          <label>Cél</label>
          <select v-model="aForm.target_id" required>
            <option value="" disabled>– válassz –</option>
            <option v-for="o in targetOptions" :key="o.id" :value="o.id">{{ o.name }}</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>Időbeosztás (opcionális)</label>
          <select v-model="aForm.schedule_id">
            <option value="">– mindig –</option>
            <option v-for="s in schedules" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </div>
        <div class="field">
          <label>Prioritás</label>
          <input v-model.number="aForm.priority" type="number" min="-1000" max="1000" />
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>Kezdete (opcionális)</label>
          <input v-model="aForm.starts_at" type="datetime-local" />
        </div>
        <div class="field">
          <label>Vége (opcionális)</label>
          <input v-model="aForm.ends_at" type="datetime-local" />
        </div>
      </div>
      <div class="field">
        <label>Címke</label>
        <input v-model="aForm.label" placeholder="pl. vizsgamód" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="assignOpen = false">Mégse</button>
        <button>Mentés</button>
      </div>
    </form>
  </Modal>
</template>
