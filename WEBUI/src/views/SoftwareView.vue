<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Software deployment: packages (an uploaded .msi/.exe + how to install, detect and
// remove it), where they go (school / room / computer, install or remove) and how
// each computer is doing. Admins change things; teachers see their rooms' computers.
// Server side: server/smp/modules/software/; the PCs: agent/.../capabilities/software/.
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import { api, errorText } from '../api/client'
import type {
  Building,
  Device,
  Page,
  Room,
  SoftwareAssignment,
  SoftwareDeviceState,
  SoftwarePackage,
  SoftwarePackageIn,
  SoftwareUpload,
} from '../api/types'
import Modal from '../components/Modal.vue'
import { fmtRelative } from '../format'
import { t, tOr } from '../i18n'
import { can, canAnywhere } from '../session'

const canEdit = computed(() => can('software.packages.write'))
// Assigning is also a manager's job (their buildings: the permission exists per building);
// uploading packages is for the school level only.
const canAssign = computed(() => canAnywhere('software.assignments.write'))
const assignOrgWide = computed(() => can('software.assignments.write'))

const packages = shallowRef<SoftwarePackage[]>([])
const error = ref('')
const selectedId = ref<string | null>(null)
const selected = computed(() => packages.value.find((p) => p.id === selectedId.value) ?? null)

async function load() {
  try {
    packages.value = await api.get<SoftwarePackage[]>('/software/packages')
    error.value = ''
    if (selectedId.value && !selected.value) selectedId.value = null
  } catch (e) {
    error.value = errorText(e)
  }
}

// ---------------------------------------------------------------- formatting

function fmtSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

/** Badge colour per state (see server/smp/modules/software/desired.py). */
const STATE_CLASS: Record<string, string> = {
  installed: 'ok',
  removed: 'closed',
  reboot_pending: 'warning',
  failed: 'danger',
  remove_failed: 'danger',
  pending: 'waiting', // nothing happened yet
  waiting: 'waiting',
  downloading: 'info', // working on it
  installing: 'info',
  removing: 'info',
}
const STATE_ORDER = ['failed', 'remove_failed', 'reboot_pending', 'downloading', 'installing', 'removing', 'waiting', 'pending', 'installed', 'removed']
const stateLabel = (s: string | null | undefined) => (s ? tOr(`software.state.${s}`, s) : '–')
/** The agent's message (it names the exit code); a bare non-zero exit code otherwise. */
const detailText = (d: SoftwareDeviceState) =>
  d.message || (d.exit_code ? t('software.exit_code', { code: d.exit_code }) : '')
const sortedCounts = (counts: Record<string, number>) =>
  Object.entries(counts).sort(([a], [b]) => (STATE_ORDER.indexOf(a) + 1 || 99) - (STATE_ORDER.indexOf(b) + 1 || 99))

// ---------------------------------------------------------------- selected package: assignments + computers

const assignments = shallowRef<SoftwareAssignment[]>([])
const devices = shallowRef<SoftwareDeviceState[]>([])
const detailError = ref('')

async function loadDetail() {
  const id = selectedId.value
  if (!id) return
  try {
    const [a, d] = await Promise.all([
      api.get<SoftwareAssignment[]>(`/software/packages/${id}/assignments`),
      api.get<SoftwareDeviceState[]>(`/software/packages/${id}/devices`),
    ])
    if (selectedId.value !== id) return
    assignments.value = a
    devices.value = d
    detailError.value = ''
  } catch (e) {
    detailError.value = errorText(e)
  }
}
watch(selectedId, () => {
  assignments.value = []
  devices.value = []
  loadDetail()
})

// Installations take minutes: keep the numbers fresh while the page is open and visible.
let timer: number | undefined
function refresh() {
  if (document.visibilityState !== 'visible') return
  load()
  loadDetail()
}
onMounted(() => {
  load()
  timer = window.setInterval(refresh, 10_000)
})
onUnmounted(() => window.clearInterval(timer))

const targetLabel = (a: SoftwareAssignment) =>
  a.target_type === 'org' ? t('software.target_org') : (a.target_name ?? '?')

// New assignment (admins)
const rooms = shallowRef<Room[]>([])
const buildings = shallowRef<Building[]>([])
const allDevices = shallowRef<Device[]>([])
const assignForm = reactive({ target_type: 'org' as 'org' | 'building' | 'room' | 'device', target_id: '', action: 'install' as 'install' | 'remove' })
const assignError = ref('')

async function loadTargets() {
  if (!canAssign.value || rooms.value.length || allDevices.value.length || buildings.value.length) return
  const [r, d, b] = await Promise.all([
    api.get<Room[]>('/rooms').catch(() => []),
    api.get<Page<Device>>('/devices', { limit: 2000 }).catch(() => ({ items: [], total: 0 })),
    api.get<Building[]>('/buildings').catch(() => []),
  ])
  rooms.value = r
  buildings.value = b
  // someone without school-wide rights starts at their building
  if (!assignOrgWide.value) assignForm.target_type = b.length ? 'building' : 'room'
  allDevices.value = d.items.filter((x) => x.agent_id).sort((a, b) => a.hostname.localeCompare(b.hostname))
}
watch(selectedId, loadTargets)

async function addAssignment() {
  if (!selected.value) return
  try {
    await api.post('/software/assignments', {
      package_id: selected.value.id,
      target_type: assignForm.target_type,
      target_id: assignForm.target_type === 'org' ? null : assignForm.target_id,
      action: assignForm.action,
    })
    assignError.value = ''
    assignForm.target_id = ''
    loadDetail()
    load()
  } catch (e) {
    assignError.value = errorText(e)
  }
}

async function setAction(a: SoftwareAssignment, action: 'install' | 'remove') {
  if (action === 'remove' && !confirm(t('software.confirm_remove', { target: targetLabel(a) }))) return
  try {
    await api.patch(`/software/assignments/${a.id}`, { action })
    loadDetail()
    load()
  } catch (e) {
    detailError.value = errorText(e)
  }
}

async function deleteAssignment(a: SoftwareAssignment) {
  if (!confirm(t('software.confirm_unassign', { target: targetLabel(a) }))) return
  try {
    await api.del(`/software/assignments/${a.id}`)
    loadDetail()
    load()
  } catch (e) {
    detailError.value = errorText(e)
  }
}

// ---------------------------------------------------------------- package dialog (admins)

type DetType = 'msi' | 'registry' | 'file'
const dialogOpen = ref(false)
const editing = ref<SoftwarePackage | null>(null)
const upload = ref<SoftwareUpload | null>(null) // a newly uploaded file (new package, or a new version)
const uploading = ref<number | null>(null) // 0..1 while a file goes up
const formError = ref('')
const form = reactive({
  name: '', version: '', publisher: '', notes: '',
  install_args: '', uninstall_command: '',
  detType: 'msi' as DetType, productCode: '', regKey: '', regValue: '', regEquals: '', filePath: '',
  reboot: 'never' as 'never' | 'if_required' | 'always',
  timeoutMin: 60, successCodes: '',
})
/** The kind of the file the package will have: a new upload's, else the current one's. */
const kind = computed(() => upload.value?.kind ?? editing.value?.kind ?? null)

function openDialog(p: SoftwarePackage | null) {
  editing.value = p
  upload.value = null
  formError.value = ''
  const d = (p?.detection ?? {}) as Record<string, string | null | undefined>
  Object.assign(form, {
    name: p?.name ?? '', version: p?.version ?? '', publisher: p?.publisher ?? '', notes: p?.notes ?? '',
    install_args: p?.install_args ?? '', uninstall_command: p?.uninstall_command ?? '',
    detType: (d.type as DetType) ?? 'msi', productCode: d.product_code ?? '',
    regKey: d.key ?? '', regValue: d.value ?? '', regEquals: d.equals ?? '', filePath: d.path ?? '',
    reboot: p?.reboot ?? 'never',
    timeoutMin: Math.round((p?.timeout_s ?? 3600) / 60), successCodes: (p?.success_codes ?? []).join(', '),
  })
  dialogOpen.value = true
}

async function chooseFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  formError.value = ''
  uploading.value = 0
  try {
    upload.value = await api.uploadWithProgress<SoftwareUpload>(
      `/software/uploads?filename=${encodeURIComponent(file.name)}`,
      file,
      'application/octet-stream',
      (sent) => (uploading.value = sent),
    )
    if (!form.name) form.name = file.name.replace(/\.(msi|exe)$/i, '')
    // An .exe can't use the product code rule; an .msi starts with it.
    if (upload.value.kind === 'exe' && form.detType === 'msi') form.detType = 'registry'
    if (upload.value.kind === 'msi' && !editing.value) form.detType = 'msi'
  } catch (err) {
    formError.value = errorText(err)
  } finally {
    uploading.value = null
    input.value = ''
  }
}

function detection(): SoftwarePackageIn['detection'] {
  if (form.detType === 'msi') return { type: 'msi', product_code: form.productCode.trim() || null }
  if (form.detType === 'registry') {
    const value = form.regValue.trim() || null
    return { type: 'registry', key: form.regKey.trim(), value, equals: value ? form.regEquals.trim() || null : null }
  }
  return { type: 'file', path: form.filePath.trim() }
}

async function savePackage() {
  const codes = form.successCodes.split(/[\s,;]+/).filter(Boolean).map(Number)
  if (codes.some((c) => !Number.isInteger(c))) {
    formError.value = t('software.bad_codes')
    return
  }
  const body = {
    name: form.name.trim(), version: form.version.trim(), publisher: form.publisher.trim(), notes: form.notes,
    install_args: form.install_args.trim(),
    uninstall_command: kind.value === 'exe' ? form.uninstall_command.trim() : '',
    detection: detection(), reboot: form.reboot, success_codes: codes,
    timeout_s: Math.round(form.timeoutMin * 60),
  }
  try {
    if (editing.value) {
      await api.patch(`/software/packages/${editing.value.id}`, { ...body, ...(upload.value ? { upload_id: upload.value.id } : {}) })
    } else {
      if (!upload.value) {
        formError.value = t('software.choose_file_first')
        return
      }
      const created = await api.post<SoftwarePackage>('/software/packages', { ...body, upload_id: upload.value.id })
      selectedId.value = created.id
    }
    dialogOpen.value = false
    await load()
    loadDetail()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function deletePackage(p: SoftwarePackage) {
  if (!confirm(t('software.confirm_delete', { name: p.name }))) return
  try {
    await api.del(`/software/packages/${p.id}`)
    if (selectedId.value === p.id) selectedId.value = null
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>{{ t('nav./software') }}</h1>
      <p>{{ t('software.subtitle') }}</p>
    </div>
    <button v-if="canEdit" @click="openDialog(null)">{{ t('software.new') }}</button>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('common.name') }}</th>
          <th>{{ t('software.file') }}</th>
          <th>{{ t('software.restart') }}</th>
          <th>{{ t('software.computers') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="p in packages" :key="p.id" class="clickable" :class="{ selected: p.id === selectedId }"
            @click="selectedId = p.id === selectedId ? null : p.id">
          <td>
            <strong>{{ p.name }}</strong> <span class="muted">{{ p.version }}</span>
            <div v-if="p.publisher" class="muted small-text">{{ p.publisher }}</div>
          </td>
          <td>
            <span class="badge normal mono">.{{ p.kind }}</span>
            <span class="muted size">{{ fmtSize(p.size) }}</span>
            <div class="muted small-text mono" :title="p.sha256">{{ p.file_name }}</div>
          </td>
          <td>{{ tOr(`software.reboot.${p.reboot}`, p.reboot) }}</td>
          <td>
            <div class="chips">
              <span v-for="[state, count] in sortedCounts(p.counts)" :key="state" class="badge" :class="STATE_CLASS[state]">
                {{ stateLabel(state) }}: {{ count }}
              </span>
              <span v-if="!p.assignments" class="muted">{{ t('software.not_assigned') }}</span>
            </div>
          </td>
          <td>
            <div v-if="canEdit" class="actions" @click.stop>
              <button class="ghost small" @click="openDialog(p)">{{ t('common.edit') }}</button>
              <button class="danger small" @click="deletePackage(p)">{{ t('common.delete') }}</button>
            </div>
          </td>
        </tr>
        <tr v-if="!packages.length">
          <td colspan="5" class="empty">{{ canEdit ? t('software.empty_admin') : t('software.empty') }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <template v-if="selected">
    <div v-if="detailError" class="alert err">{{ detailError }}</div>

    <div class="card stack">
      <h2>{{ t('software.where', { name: selected.name }) }}</h2>
      <p class="muted">{{ t('software.where_hint') }}</p>
      <table>
        <tbody>
          <tr v-for="a in assignments" :key="a.id">
            <td>{{ tOr(`software.target.${a.target_type}`, a.target_type) }}</td>
            <td><strong>{{ targetLabel(a) }}</strong></td>
            <td><span class="badge" :class="a.action === 'install' ? 'ok' : 'danger'">{{ tOr(`software.action.${a.action}`, a.action) }}</span></td>
            <td>
              <div v-if="canAssign" class="actions">
                <button v-if="a.action === 'install' && selected.removable" class="ghost small" @click="setAction(a, 'remove')">
                  {{ t('software.switch_remove') }}
                </button>
                <button v-if="a.action === 'remove'" class="ghost small" @click="setAction(a, 'install')">
                  {{ t('software.switch_install') }}
                </button>
                <button class="danger small" @click="deleteAssignment(a)">{{ t('software.unassign') }}</button>
              </div>
            </td>
          </tr>
          <tr v-if="!assignments.length">
            <td colspan="4" class="empty">{{ t('software.no_assignments') }}</td>
          </tr>
        </tbody>
      </table>

      <form v-if="canAssign" class="row" @submit.prevent="addAssignment">
        <div class="field">
          <label>{{ t('software.target_label') }}</label>
          <select v-model="assignForm.target_type" @change="assignForm.target_id = ''">
            <option v-if="assignOrgWide" value="org">{{ t('software.target_org') }}</option>
            <option v-if="buildings.length" value="building">{{ t('software.target.building') }}</option>
            <option value="room">{{ t('software.target.room') }}</option>
            <option value="device">{{ t('software.target.device') }}</option>
          </select>
        </div>
        <div v-if="assignForm.target_type === 'building'" class="field">
          <label>{{ t('software.target.building') }}</label>
          <select v-model="assignForm.target_id" required>
            <option v-for="b in buildings" :key="b.id" :value="b.id">{{ b.name }}</option>
          </select>
        </div>
        <div v-if="assignForm.target_type === 'room'" class="field">
          <label>{{ t('software.target.room') }}</label>
          <select v-model="assignForm.target_id" required>
            <option v-for="r in rooms" :key="r.id" :value="r.id">{{ r.name }}</option>
          </select>
        </div>
        <div v-if="assignForm.target_type === 'device'" class="field">
          <label>{{ t('software.target.device') }}</label>
          <select v-model="assignForm.target_id" required>
            <option v-for="d in allDevices" :key="d.id" :value="d.id">{{ d.hostname }}</option>
          </select>
        </div>
        <div class="field">
          <label>{{ t('software.action_label') }}</label>
          <select v-model="assignForm.action">
            <option value="install">{{ t('software.action.install') }}</option>
            <option value="remove" :disabled="!selected.removable">{{ t('software.action.remove') }}</option>
          </select>
        </div>
        <div class="field" style="align-self: end">
          <button>{{ t('software.assign') }}</button>
        </div>
      </form>
      <div v-if="assignError" class="alert err">{{ assignError }}</div>
    </div>

    <div class="card table-wrap">
      <h2>{{ t('software.status_title') }}</h2>
      <table>
        <thead>
          <tr>
            <th>{{ t('software.computer') }}</th>
            <th>{{ t('common.room') }}</th>
            <th>{{ t('software.action_label') }}</th>
            <th>{{ t('common.status') }}</th>
            <th>{{ t('software.details') }}</th>
            <th>{{ t('software.reported') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in devices" :key="d.device_id">
            <td><span class="dot" :class="{ on: d.online }"></span> <strong>{{ d.hostname }}</strong></td>
            <td>{{ d.room_name ?? '–' }}</td>
            <td>{{ d.action ? tOr(`software.action.${d.action}`, d.action) : t('software.no_longer_assigned') }}</td>
            <td><span class="badge" :class="STATE_CLASS[d.state ?? '']">{{ stateLabel(d.state) }}</span></td>
            <td class="muted small-text">{{ detailText(d) }}</td>
            <td class="muted">{{ fmtRelative(d.updated_at) }}</td>
          </tr>
          <tr v-if="!devices.length">
            <td colspan="6" class="empty">{{ t('software.no_devices') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <Modal v-if="dialogOpen" :title="editing ? t('software.edit_title', { name: editing.name }) : t('software.new_title')"
         @close="dialogOpen = false">
    <form class="stack" @submit.prevent="savePackage">
      <div v-if="formError" class="alert err">{{ formError }}</div>

      <div class="field">
        <label>{{ editing ? t('software.new_version') : t('software.file_label') }}</label>
        <input type="file" accept=".msi,.exe" :disabled="uploading !== null" @change="chooseFile" />
        <progress v-if="uploading !== null" :value="uploading" max="1" style="width: 100%"></progress>
        <span v-if="upload" class="muted">✓ {{ upload.file_name }} · .{{ upload.kind }} · {{ fmtSize(upload.size) }}</span>
        <span v-else-if="editing" class="muted">{{ editing.file_name }} · {{ fmtSize(editing.size) }} · {{ t('software.new_version_hint') }}</span>
      </div>

      <div class="row">
        <div class="field">
          <label>{{ t('common.name') }}</label>
          <input v-model="form.name" required maxlength="200" />
        </div>
        <div class="field">
          <label>{{ t('software.version') }}</label>
          <input v-model="form.version" maxlength="64" />
        </div>
        <div class="field">
          <label>{{ t('software.publisher') }}</label>
          <input v-model="form.publisher" maxlength="200" />
        </div>
      </div>

      <div class="field">
        <label>{{ t('software.install_args') }}</label>
        <input v-model="form.install_args" class="mono" maxlength="2000"
               :placeholder="kind === 'exe' ? '/S' : 'ALLUSERS=1'" />
        <span class="muted small-text">{{ kind === 'exe' ? t('software.install_args_exe') : t('software.install_args_msi') }}</span>
      </div>

      <div class="field">
        <label>{{ t('software.detection') }}</label>
        <select v-model="form.detType">
          <option value="msi" :disabled="kind === 'exe'">{{ t('software.det.msi') }}</option>
          <option value="registry">{{ t('software.det.registry') }}</option>
          <option value="file">{{ t('software.det.file') }}</option>
        </select>
      </div>
      <div v-if="form.detType === 'msi'" class="field">
        <label>{{ t('software.product_code') }}</label>
        <input v-model="form.productCode" class="mono" placeholder="{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" />
        <span class="muted small-text">{{ t('software.product_code_hint') }}</span>
      </div>
      <template v-if="form.detType === 'registry'">
        <div class="field">
          <label>{{ t('software.reg_key') }}</label>
          <input v-model="form.regKey" class="mono" required
                 placeholder="HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\7-Zip" />
        </div>
        <div class="row">
          <div class="field">
            <label>{{ t('software.reg_value') }}</label>
            <input v-model="form.regValue" class="mono" placeholder="DisplayVersion" />
          </div>
          <div class="field">
            <label>{{ t('software.reg_equals') }}</label>
            <input v-model="form.regEquals" class="mono" :disabled="!form.regValue.trim()" placeholder="24.08" />
          </div>
        </div>
        <span class="muted small-text">{{ t('software.reg_hint') }}</span>
      </template>
      <div v-if="form.detType === 'file'" class="field">
        <label>{{ t('software.file_path') }}</label>
        <input v-model="form.filePath" class="mono" required placeholder="%ProgramFiles%\VideoLAN\VLC\vlc.exe" />
      </div>

      <div v-if="kind === 'exe'" class="field">
        <label>{{ t('software.uninstall_command') }}</label>
        <input v-model="form.uninstall_command" class="mono" maxlength="2000"
               placeholder='"%ProgramFiles%\VideoLAN\VLC\uninstall.exe" /S' />
        <span class="muted small-text">{{ t('software.uninstall_hint') }}</span>
      </div>

      <div class="field">
        <label>{{ t('software.restart') }}</label>
        <select v-model="form.reboot">
          <option value="never">{{ t('software.reboot.never') }}</option>
          <option value="if_required">{{ t('software.reboot.if_required') }}</option>
          <option value="always">{{ t('software.reboot.always') }}</option>
        </select>
        <span class="muted small-text">{{ t(`software.reboot_hint.${form.reboot}`) }}</span>
      </div>

      <details>
        <summary>{{ t('software.advanced') }}</summary>
        <div class="stack" style="margin-top: 8px">
          <div class="row">
            <div class="field">
              <label>{{ t('software.timeout') }}</label>
              <input v-model.number="form.timeoutMin" type="number" min="1" max="360" />
            </div>
            <div class="field">
              <label>{{ t('software.success_codes') }}</label>
              <input v-model="form.successCodes" class="mono" placeholder="1, 3011" />
            </div>
          </div>
          <div class="field">
            <label>{{ t('software.notes') }}</label>
            <textarea v-model="form.notes" maxlength="5000" style="min-height: 60px"></textarea>
          </div>
        </div>
      </details>

      <p class="muted small-text">{{ t('software.system_warning') }}</p>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="dialogOpen = false">{{ t('common.cancel') }}</button>
        <button :disabled="uploading !== null || (!editing && !upload)">{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>
</template>

<style scoped>
tr.clickable {
  cursor: pointer;
}
tr.selected td {
  background: var(--accent-soft, rgba(80, 120, 255, 0.08));
}
.size {
  margin-left: 6px;
}
.small-text {
  font-size: 0.85em;
}
.card + .card,
.alert + .card {
  margin-top: 16px;
}
</style>
