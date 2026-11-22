<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Assignment, Device, Group, Page, Policy, Room, Schedule, TargetType, User } from '../api/types'
import Modal from '../components/Modal.vue'
import { DAY_INDEXES, dayLabel, describeWindows, fmtDate, fromLocalInput, moduleTitle, TARGET_TYPES, targetTypeLabel } from '../format'
import { t } from '../i18n'
import { can, canAnywhere, session } from '../session'

const route = useRoute()
const tab = computed(() => (route.query.tab as string) || 'assignments')
const TABS = ['assignments', 'policies', 'schedules'] as const

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
      return targetTypeLabel('org')
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
  if (!confirm(t('common.confirm_delete', { what }))) return
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
    pError.value = t('policies.invalid_json')
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
  aForm.label = t('policies.exam_label')
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
    <h1>{{ t('nav./policies') }}</h1>
    <p>{{ t('policies.intro') }}</p>
  </div>

  <nav class="tabs">
    <RouterLink v-for="key in TABS" :key="key" :to="{ query: { tab: key } }" :class="{ active: tab === key }">
      {{ t(`policies.tab.${key}`) }}
    </RouterLink>
  </nav>

  <div v-if="error" class="alert err">{{ error }}</div>

  <!-- ------------------------------------------------------------ assignments -->
  <template v-if="tab === 'assignments'">
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canAssign" :disabled="!policies.length" @click="openAssign">{{ t('policies.new_assignment') }}</button>
    </div>
    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>{{ t('policies.policy') }}</th>
            <th>{{ t('policies.target') }}</th>
            <th>{{ t('policies.schedule') }}</th>
            <th>{{ t('policies.priority') }}</th>
            <th>{{ t('policies.period') }}</th>
            <th>{{ t('common.status') }}</th>
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
            <td>{{ targetTypeLabel(a.target_type) }}: {{ targetName(a) }}</td>
            <td>{{ scheduleName(a.schedule_id) ?? '–' }}</td>
            <td>{{ a.priority }}</td>
            <td class="small">{{ fmtDate(a.starts_at) }} → {{ fmtDate(a.ends_at) }}</td>
            <td>
              <span v-if="isActiveNow(a)" class="badge ok">{{ t('policies.in_effect') }}</span>
              <span v-else class="badge closed">{{ t('policies.inactive') }}</span>
            </td>
            <td>
              <div v-if="canAssign" class="actions">
                <button class="danger small" @click="del(`/policy-assignments/${a.id}`, t('policies.the_assignment'))">{{ t('common.delete') }}</button>
              </div>
            </td>
          </tr>
          <tr v-if="!assignments.length">
            <td colspan="7" class="empty">{{ t('policies.no_assignments') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ------------------------------------------------------------ policies -->
  <template v-if="tab === 'policies'">
    <div v-if="!kinds.length" class="alert ok">{{ t('policies.no_kinds') }}</div>
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canWrite" :disabled="!kinds.length" @click="openPolicy(null)">{{ t('policies.new_policy') }}</button>
    </div>
    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>{{ t('common.name') }}</th>
            <th>{{ t('common.type') }}</th>
            <th>{{ t('common.description') }}</th>
            <th>{{ t('policies.modified') }}</th>
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
                <button class="ghost small" @click="openPolicy(p)">{{ t('common.edit') }}</button>
                <button class="danger small" @click="del(`/policies/${p.id}`, p.name)">{{ t('common.delete') }}</button>
              </div>
            </td>
          </tr>
          <tr v-if="!policies.length">
            <td colspan="5" class="empty">{{ t('policies.no_policies') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ------------------------------------------------------------ schedules -->
  <template v-if="tab === 'schedules'">
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canWrite" @click="openSchedule(null)">{{ t('policies.new_schedule') }}</button>
    </div>
    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>{{ t('common.name') }}</th>
            <th>{{ t('policies.windows') }}</th>
            <th>{{ t('policies.timezone') }}</th>
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
                <button class="ghost small" @click="openSchedule(s)">{{ t('common.edit') }}</button>
                <button class="danger small" @click="del(`/schedules/${s.id}`, s.name)">{{ t('common.delete') }}</button>
              </div>
            </td>
          </tr>
          <tr v-if="!schedules.length">
            <td colspan="4" class="empty">{{ t('policies.no_schedules') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ------------------------------------------------------------ dialogs -->
  <Modal v-if="policyDlg" :title="policyDlg.policy ? t('policies.edit_policy') : t('policies.new_policy_title')" wide @close="policyDlg = null">
    <form class="stack" @submit.prevent="savePolicy">
      <div v-if="pError" class="alert err">{{ pError }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('policies.kind') }}</label>
          <select v-model="pForm.kind" :disabled="!!policyDlg.policy">
            <option v-for="k in kinds" :key="k.name" :value="k.name">{{ moduleTitle(k) }} ({{ k.name }})</option>
          </select>
        </div>
        <div class="field">
          <label>{{ t('common.name') }}</label>
          <input v-model="pForm.name" required />
        </div>
      </div>
      <div class="field">
        <label>{{ t('common.description') }}</label>
        <input v-model="pForm.description" />
      </div>
      <div class="field">
        <label>{{ t('policies.data') }}</label>
        <textarea v-model="pForm.data" class="mono" style="min-height: 180px"></textarea>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="policyDlg = null">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="scheduleDlg" :title="scheduleDlg.schedule ? t('policies.edit_schedule') : t('policies.new_schedule_title')" wide @close="scheduleDlg = null">
    <form class="stack" @submit.prevent="saveSchedule">
      <div v-if="sError" class="alert err">{{ sError }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('common.name') }}</label>
          <input v-model="sForm.name" required :placeholder="t('policies.schedule_name_placeholder')" />
        </div>
        <div class="field">
          <label>{{ t('policies.timezone') }}</label>
          <input v-model="sForm.timezone" required />
        </div>
      </div>
      <div class="field">
        <label>{{ t('policies.windows_label') }}</label>
        <div class="stack" style="gap: 8px">
          <div v-for="(w, i) in sForm.windows" :key="i" class="row" style="align-items: center">
            <div class="chips">
              <label v-for="di in DAY_INDEXES" :key="di" class="check badge" :class="{ info: w.days.includes(di) }">
                <input type="checkbox" :checked="w.days.includes(di)" style="display: none" @change="toggleDay(w, di)" />{{ dayLabel(di) }}
              </label>
            </div>
            <input v-model="w.start" type="time" required style="width: 110px" />
            <span>–</span>
            <input v-model="w.end" type="time" required style="width: 110px" />
            <button type="button" class="ghost small" @click="sForm.windows.splice(i, 1)">✕</button>
          </div>
          <div>
            <button type="button" class="ghost small" @click="sForm.windows.push({ days: [0, 1, 2, 3, 4], start: '08:00', end: '08:45' })">
              {{ t('policies.add_window') }}
            </button>
          </div>
        </div>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="scheduleDlg = null">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="assignOpen" :title="t('policies.new_assignment_title')" wide @close="assignOpen = false">
    <form class="stack" @submit.prevent="saveAssign">
      <div v-if="aError" class="alert err">{{ aError }}</div>
      <div class="row">
        <span class="muted small">{{ t('policies.quick') }}</span>
        <button type="button" class="ghost small" @click="examPreset(45)">{{ t('policies.exam', { minutes: 45 }) }}</button>
        <button type="button" class="ghost small" @click="examPreset(90)">{{ t('policies.exam', { minutes: 90 }) }}</button>
      </div>
      <div class="field">
        <label>{{ t('policies.policy') }}</label>
        <select v-model="aForm.policy_id" required>
          <option v-for="p in policies" :key="p.id" :value="p.id">{{ p.name }} ({{ p.kind }})</option>
        </select>
      </div>
      <div class="row">
        <div class="field">
          <label>{{ t('policies.target_type') }}</label>
          <select v-model="aForm.target_type">
            <option v-for="tt in TARGET_TYPES" :key="tt" :value="tt">{{ targetTypeLabel(tt) }}</option>
          </select>
        </div>
        <div v-if="aForm.target_type !== 'org'" class="field">
          <label>{{ t('policies.target') }}</label>
          <select v-model="aForm.target_id" required>
            <option value="" disabled>{{ t('policies.choose') }}</option>
            <option v-for="o in targetOptions" :key="o.id" :value="o.id">{{ o.name }}</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>{{ t('policies.schedule_optional') }}</label>
          <select v-model="aForm.schedule_id">
            <option value="">{{ t('policies.always') }}</option>
            <option v-for="s in schedules" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </div>
        <div class="field">
          <label>{{ t('policies.priority') }}</label>
          <input v-model.number="aForm.priority" type="number" min="-1000" max="1000" />
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>{{ t('policies.starts') }}</label>
          <input v-model="aForm.starts_at" type="datetime-local" />
        </div>
        <div class="field">
          <label>{{ t('policies.ends') }}</label>
          <input v-model="aForm.ends_at" type="datetime-local" />
        </div>
      </div>
      <div class="field">
        <label>{{ t('policies.label') }}</label>
        <input v-model="aForm.label" :placeholder="t('policies.label_placeholder')" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="assignOpen = false">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>
</template>
