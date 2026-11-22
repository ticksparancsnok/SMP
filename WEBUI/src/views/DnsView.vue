<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { DnsEntry, DnsEntryIn, DnsHostsPreview, DnsStatus } from '../api/types'
import Modal from '../components/Modal.vue'
import { can } from '../session'

const entries = ref<DnsEntry[]>([])
const status = ref<DnsStatus | null>(null)
const error = ref('')
const busy = ref(false)
const canWrite = can('dns.blocklist.write')

const createOpen = ref(false)
const createError = ref('')
const form = reactive({
  domain: '',
  includeSubdomains: true,
  reason: '',
  confidence: '',
})

const importOpen = ref(false)
const importFile = ref<File | null>(null)
const preview = ref<DnsHostsPreview | null>(null)
const importError = ref('')

async function load() {
  busy.value = true
  try {
    ;[entries.value, status.value] = await Promise.all([
      api.get<DnsEntry[]>('/dns/blocklist'),
      api.get<DnsStatus>('/dns/status'),
    ])
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function createEntry() {
  createError.value = ''
  try {
    await api.post('/dns/blocklist', {
      domain: form.domain,
      include_subdomains: form.includeSubdomains,
      reason: form.reason,
      confidence: form.confidence === '' ? null : Number(form.confidence),
    } satisfies DnsEntryIn)
    createOpen.value = false
    await load()
  } catch (e) {
    createError.value = errorText(e)
  }
}

async function removeEntry(entry: DnsEntry) {
  if (!confirm(`Biztosan törlöd: ${entry.domain}?`)) return
  try {
    await api.del(`/dns/blocklist/${entry.id}`)
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
}

async function previewImport(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  importFile.value = file
  preview.value = null
  importError.value = ''
  if (!file) return
  busy.value = true
  try {
    preview.value = await api.upload<DnsHostsPreview>(
      '/dns/blocklist/import-preview',
      file,
      file.type || 'text/plain',
    )
  } catch (e) {
    importError.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function confirmImport() {
  if (!importFile.value) return
  busy.value = true
  importError.value = ''
  try {
    await api.upload<DnsHostsPreview>('/dns/blocklist/import', importFile.value, 'text/plain')
    importOpen.value = false
    await load()
  } catch (e) {
    importError.value = errorText(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>Oldal blokkoló</h1>
      <p>DNS blokklista kezelése és szinkronizáció a CoreDNS sinkhole-lal.</p>
    </div>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>
  <div v-if="status" class="card" style="margin-bottom: 16px">
    <strong>DNS sinkhole:</strong>
    {{ status.sync_configured ? 'Elérhető' : 'Nem elérhető' }}
    <span class="muted"> · {{ status.entries }} blokklista-bejegyzés · {{ status.sinkhole_url }}</span>
  </div>

  <div class="toolbar">
    <span class="grow"></span>
    <button v-if="canWrite" class="ghost" @click="importOpen = true">Hosts import</button>
    <button v-if="canWrite" @click="createOpen = true">+ Kézi blokkolás</button>
  </div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          <th>Indok</th>
          <th>Hozzáadta</th>
          <th>Bizonyosság</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="entry in entries" :key="entry.id">
          <td>
            <strong>{{ entry.domain }}</strong>
            <span v-if="entry.includeSubdomains" class="badge" style="margin-left: 6px">aldomainokkal</span>
          </td>
          <td>{{ entry.reason || '–' }}</td>
          <td>{{ entry.addedBy }}</td>
          <td>{{ entry.confidence === null ? '–' : `${Math.round(entry.confidence * 100)}%` }}</td>
          <td>
            <button v-if="canWrite" class="danger small" @click="removeEntry(entry)">Törlés</button>
          </td>
        </tr>
        <tr v-if="!entries.length">
          <td colspan="5" class="empty">Nincs blokklista-bejegyzés.</td>
        </tr>
      </tbody>
    </table>
  </div>

  <Modal v-if="createOpen" title="Domain kézi blokkolása" @close="createOpen = false">
    <form class="stack" @submit.prevent="createEntry">
      <div v-if="createError" class="alert err">{{ createError }}</div>
      <div class="field">
        <label>Domain</label>
        <input v-model="form.domain" required placeholder="example-blocked.com" />
      </div>
      <label class="check">
        <input v-model="form.includeSubdomains" type="checkbox" /> Aldomaineket is blokkolja
      </label>
      <div class="field">
        <label>Indok</label>
        <input v-model="form.reason" placeholder="Tanórai zavaró tartalom" />
      </div>
      <div class="field">
        <label>AI bizonyosság (opcionális, 0–1)</label>
        <input v-model="form.confidence" type="number" min="0" max="1" step="0.01" placeholder="0.94" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="createOpen = false">Mégse</button>
        <button>Hozzáadás</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="importOpen" title="Hosts blokklista importálása" @close="importOpen = false">
    <div class="stack">
      <div v-if="importError" class="alert err">{{ importError }}</div>
      <div class="field">
        <label>Hosts fájl</label>
        <input type="file" accept=".txt,text/plain" :disabled="busy" @change="previewImport" />
      </div>
      <div v-if="preview" class="alert ok">
        {{ preview.unique_count }} egyedi domain került elő.
      </div>
      <details v-if="preview?.domains.length">
        <summary>Domain előnézet</summary>
        <div class="chips" style="margin-top: 8px">
          <span v-for="domain in preview.domains" :key="domain" class="badge mono">{{ domain }}</span>
        </div>
      </details>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="importOpen = false">Mégse</button>
        <button :disabled="!preview || busy" @click="confirmImport">Importálás</button>
      </div>
    </div>
  </Modal>
</template>
