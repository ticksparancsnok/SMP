<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Decision order (same in the server and the Go sinkhole): allowlist -> blocklist ->
// everything else allowed. Public suffixes (com, co.uk, ...) can't go on either list.
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { api, ApiError, errorText } from '../api/client'
import type {
  DnsAllowEntry,
  DnsAllowIn,
  DnsAllowlist,
  DnsEntry,
  DnsEntryIn,
  DnsImportJob,
  DnsStatus,
  Page,
} from '../api/types'
import LangSwitch from '../components/LangSwitch.vue'
import Modal from '../components/Modal.vue'
import Pager from '../components/Pager.vue'
import { t, tOr, type Params } from '../i18n'
import { DNS_PAGE_SIZES, usePage, usePageSize } from '../paging'
import { can } from '../session'

const tab = ref<'blocklist' | 'allowlist'>('blocklist')
const allowlist = shallowRef<DnsAllowlist | null>(null)
const status = ref<DnsStatus | null>(null)
const error = ref('')
const canWrite = can('dns.blocklist.write')

/** The change was saved but the sinkhole could not be told: the lists did change. */
const savedAnyway = (e: unknown) => e instanceof ApiError && e.code === 'dns.sync_failed'

// ---------------------------------------------------------------- blocklist (paged on the server)
// It can hold 100k+ imported domains: only one page is ever downloaded and rendered.
const entries = shallowRef<DnsEntry[]>([])
const total = ref(0)
const offset = ref(0)
const limit = usePageSize('dns.blocklist', DNS_PAGE_SIZES)
const searchQuery = ref('')
let loadSeq = 0

async function loadBlocklist() {
  const seq = ++loadSeq
  try {
    const page = await api.get<Page<DnsEntry>>('/dns/blocklist', {
      q: searchQuery.value.trim(),
      limit: limit.value,
      offset: offset.value,
    })
    if (seq !== loadSeq) return // a newer page/search was requested meanwhile
    // Deleted the last row of the last page: show the page before it.
    if (!page.items.length && offset.value > 0) {
      offset.value = Math.max(0, offset.value - limit.value)
      return
    }
    entries.value = page.items
    total.value = page.total
    error.value = ''
  } catch (e) {
    if (seq === loadSeq) error.value = errorText(e)
  }
}

/** Back to the first page (new search); the [limit, offset] watcher loads it. */
function firstPage() {
  if (offset.value) offset.value = 0
  else loadBlocklist()
}

let debounce: number | undefined
watch(searchQuery, () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(firstPage, 250)
})
watch([limit, offset], loadBlocklist)

// ---------------------------------------------------------------- status & allowlist
// Status pings the sinkhole (up to a few seconds), so the list never waits for it.
async function loadStatus() {
  try {
    status.value = await api.get<DnsStatus>('/dns/status')
  } catch (e) {
    error.value = errorText(e)
  }
}

// The block page's default language (a DNS module setting, admins only). Saving it
// re-syncs the sinkhole; visitors can still switch the language on the page itself.
const canSettings = can('core.settings.write')
const savingLanguage = ref(false)

async function setBlockPageLanguage(language: 'hu' | 'en') {
  savingLanguage.value = true
  try {
    await api.put('/modules/dns/settings', { block_page_language: language })
    if (status.value) status.value = { ...status.value, block_page_language: language }
  } catch (e) {
    error.value = errorText(e)
  } finally {
    savingLanguage.value = false
  }
}

/** Keep the counts in the status bar right without asking the sinkhole again. */
function count(field: 'entries' | 'allowlist_entries', delta: number) {
  if (status.value) status.value = { ...status.value, [field]: Math.max(0, (status.value[field] ?? 0) + delta) }
}

const allowPage = usePage(() => allowlist.value?.entries ?? [], { key: 'dns.allowlist' })

async function loadAllowlist() {
  try {
    allowlist.value = await api.get<DnsAllowlist>('/dns/allowlist')
  } catch (e) {
    error.value = errorText(e)
  }
}
// Fetched the first time its tab is opened.
watch(tab, (v) => v === 'allowlist' && !allowlist.value && loadAllowlist())

function setAllowEntries(list: DnsAllowEntry[]) {
  if (allowlist.value) allowlist.value = { ...allowlist.value, entries: list }
}

// ---------------------------------------------------------------- blocklist entry dialog
const createOpen = ref(false)
const createError = ref('')
const form = reactive({ domain: '', includeSubdomains: true, reason: '', confidence: '' })

// ---------------------------------------------------------------- allowlist entry dialog
const allowOpen = ref(false)
const allowError = ref('')
const allowForm = reactive({ domain: '', includeSubdomains: true, reason: '' })

// ---------------------------------------------------------------- hosts import
// A job on the server (imports.py): upload (progress from the browser) -> checking ->
// ready (preview, paged from the server: the browser never holds 100k domains) ->
// saving -> syncing -> done. The site stays usable meanwhile; so does this page.
const importOpen = ref(false)
const importJob = ref<DnsImportJob | null>(null)
const uploading = ref<number | null>(null) // 0..1 while the file goes up
const importError = ref('')
type PreviewKind = 'new' | 'existing' | 'skipped'
const previewKind = ref<PreviewKind>('new')
const previewRows = ref<string[]>([])
const previewTotal = ref(0)
const previewOffset = ref(0)
const previewLimit = usePageSize('dns.import_preview', DNS_PAGE_SIZES)
let poller: number | undefined

const PHASES = ['uploading', 'checking', 'saving', 'syncing'] as const
/** What the progress bar shows: the step, and how far it is (null: can't tell). */
const importStep = computed(() => {
  const job = importJob.value
  const phase = uploading.value !== null ? 'uploading' : job && PHASES.find((p) => p === job.state)
  if (!phase) return null
  const fraction = phase === 'uploading' ? uploading.value : phase === 'syncing' ? null : (job?.progress ?? 0)
  const count = ['checking', 'saving'].includes(phase) && job
    ? { current: job.progress_current, total: job.progress_total }
    : null
  return { step: PHASES.indexOf(phase) + 1, phase, fraction, count }
})

function stopPolling() {
  window.clearInterval(poller)
  poller = undefined
}

function poll() {
  stopPolling()
  poller = window.setInterval(async () => {
    const job = importJob.value
    if (!job) return stopPolling()
    try {
      importJob.value = await api.get<DnsImportJob>(`/dns/blocklist/imports/${job.id}`)
    } catch (e) {
      importError.value = errorText(e)
      return stopPolling()
    }
    const now = importJob.value
    if (now.state === 'ready') {
      stopPolling()
      previewKind.value = 'new'
      await loadPreview()
    } else if (now.state === 'done') {
      stopPolling()
      count('entries', now.added ?? 0)
      await loadBlocklist()
    } else if (now.state === 'failed') {
      stopPolling()
      importError.value = now.error ? tOr(`code.${now.error.code}`, now.error.detail, (now.error.params ?? {}) as Params) : ''
    }
  }, 400)
}

async function loadPreview() {
  const job = importJob.value
  if (!job) return
  try {
    const page = await api.get<Page<string>>(`/dns/blocklist/imports/${job.id}/domains`, {
      kind: previewKind.value,
      limit: previewLimit.value,
      offset: previewOffset.value,
    })
    previewRows.value = page.items
    previewTotal.value = page.total
  } catch (e) {
    importError.value = errorText(e)
  }
}
watch([previewLimit, previewOffset], loadPreview)
function showPreview(kind: PreviewKind) {
  previewKind.value = kind
  if (previewOffset.value) previewOffset.value = 0 // the watcher loads it
  else loadPreview()
}

/** A job nobody needs any more (another file chosen, dialog closed before importing). */
function forget(job: DnsImportJob | null) {
  if (job && !['saving', 'syncing'].includes(job.state)) api.del(`/dns/blocklist/imports/${job.id}`).catch(() => {})
}

function openImport() {
  importJob.value = null
  uploading.value = null
  importError.value = ''
  previewRows.value = []
  importOpen.value = true
}

function closeImport() {
  importOpen.value = false
  const job = importJob.value
  // Saving goes on on the server; keep polling so the list refreshes when it's done.
  if (job && ['saving', 'syncing'].includes(job.state)) return
  stopPolling()
  forget(job)
  importJob.value = null
}

async function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  forget(importJob.value)
  stopPolling()
  importJob.value = null
  importError.value = ''
  previewRows.value = []
  uploading.value = 0
  try {
    importJob.value = await api.uploadWithProgress<DnsImportJob>(
      '/dns/blocklist/imports',
      file,
      file.type || 'text/plain',
      (sent) => (uploading.value = sent),
    )
    poll()
  } catch (e) {
    importError.value = errorText(e)
  } finally {
    uploading.value = null
    input.value = '' // the same file can be chosen again
  }
}

async function confirmImport() {
  const job = importJob.value
  if (!job) return
  importError.value = ''
  try {
    importJob.value = await api.post<DnsImportJob>(`/dns/blocklist/imports/${job.id}/confirm`)
    poll()
  } catch (e) {
    importError.value = errorText(e)
  }
}

onBeforeUnmount(stopPolling)

function openCreate() {
  Object.assign(form, { domain: '', includeSubdomains: true, reason: '', confidence: '' })
  createError.value = ''
  createOpen.value = true
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
    count('entries', 1)
  } catch (e) {
    createError.value = errorText(e)
    if (!savedAnyway(e)) return
    count('entries', 1)
  }
  await loadBlocklist() // just the visible page
}

async function removeEntry(entry: DnsEntry) {
  if (!confirm(t('common.confirm_delete', { what: entry.domain }))) return
  try {
    await api.del(`/dns/blocklist/${entry.id}`)
  } catch (e) {
    error.value = errorText(e)
    if (!savedAnyway(e)) return
  }
  count('entries', -1)
  await loadBlocklist()
}

function openAllow() {
  Object.assign(allowForm, { domain: '', includeSubdomains: true, reason: '' })
  allowError.value = ''
  allowOpen.value = true
}

async function createAllow() {
  allowError.value = ''
  try {
    const entry = await api.post<DnsAllowEntry>('/dns/allowlist', {
      domain: allowForm.domain,
      include_subdomains: allowForm.includeSubdomains,
      reason: allowForm.reason,
    } satisfies DnsAllowIn)
    allowOpen.value = false
    const list = [...(allowlist.value?.entries ?? []), entry].sort((a, b) => a.domain.localeCompare(b.domain))
    setAllowEntries(list)
  } catch (e) {
    allowError.value = errorText(e)
    if (!savedAnyway(e)) return
    await loadAllowlist() // saved, but we didn't get the row back
  }
  count('allowlist_entries', 1)
  await loadBlocklist() // the "allowlisted" marks on the visible page may change
}

async function removeAllow(entry: DnsAllowEntry) {
  if (!confirm(t('common.confirm_delete', { what: entry.domain }))) return
  try {
    await api.del(`/dns/allowlist/${entry.id}`)
  } catch (e) {
    error.value = errorText(e)
    if (!savedAnyway(e)) return
  }
  setAllowEntries((allowlist.value?.entries ?? []).filter((x) => x.id !== entry.id))
  count('allowlist_entries', -1)
  await loadBlocklist()
}


onMounted(() => {
  loadBlocklist()
  loadStatus()
})
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>{{ t('nav./dns') }}</h1>
      <p>{{ t('dns.subtitle') }}</p>
    </div>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>
  <div v-if="status" class="card" style="margin-bottom: 16px">
    <strong>{{ t('dns.sinkhole') }}</strong>
    {{ status.sync_configured ? t('dns.reachable') : t('dns.unreachable') }}
    <span class="muted">
      · {{ t('dns.entries', { count: status.entries }) }} · {{ t('dns.allow_entries', { count: status.allowlist_entries ?? 0 }) }}
      · {{ status.sinkhole_url }}
    </span>
    <div class="block-page-lang">
      <span>{{ t('dns.block_page_language') }}</span>
      <LangSwitch
        :model-value="status.block_page_language"
        :disabled="!canSettings || savingLanguage"
        :label="t('dns.block_page_language')"
        @update:model-value="setBlockPageLanguage"
      />
      <span class="muted small">
        {{ canSettings ? t('dns.block_page_language_hint') : t('dns.block_page_language_admins') }}
      </span>
    </div>
  </div>

  <nav class="tabs">
    <a href="#" :class="{ active: tab === 'blocklist' }" @click.prevent="tab = 'blocklist'">{{ t('dns.tab.blocklist') }}</a>
    <a href="#" :class="{ active: tab === 'allowlist' }" @click.prevent="tab = 'allowlist'">{{ t('dns.tab.allowlist') }}</a>
  </nav>

  <!-- ------------------------------------------------------------ blocklist -->
  <template v-if="tab === 'blocklist'">
    <div class="toolbar" style="flex-wrap: wrap; gap: 8px">
      <input
        v-model="searchQuery"
        type="search"
        :placeholder="t('dns.filter_placeholder')"
        style="max-width: 260px"
      />
      <span class="grow"></span>
      <button v-if="canWrite" class="ghost" @click="openImport">{{ t('dns.hosts_import') }}</button>
      <button v-if="canWrite" @click="openCreate">{{ t('dns.manual_block') }}</button>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Domain</th>
            <th>{{ t('dns.reason') }}</th>
            <th>{{ t('dns.added_by') }}</th>
            <th>{{ t('dns.confidence') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in entries" :key="entry.id" :class="{ muted: entry.active === false }">
            <td>
              <strong>{{ entry.domain }}</strong>
              <span v-if="entry.includeSubdomains" class="badge" style="margin-left: 6px">{{ t('dns.with_subdomains') }}</span>
              <span
                v-if="entry.active === false"
                class="badge warning"
                style="margin-left: 6px"
                :title="tOr(`dns.inactive.${entry.inactiveReason}.title`, '')"
              >
                {{ t('dns.inactive') }}: {{ tOr(`dns.inactive.${entry.inactiveReason}`, entry.inactiveReason ?? '') }}
              </span>
            </td>
            <td>{{ entry.reason || '–' }}</td>
            <td>{{ entry.addedBy }}</td>
            <td>{{ entry.confidence === null ? '–' : `${Math.round(entry.confidence * 100)}%` }}</td>
            <td>
              <button v-if="canWrite" class="danger small" @click="removeEntry(entry)">{{ t('common.delete') }}</button>
            </td>
          </tr>
          <tr v-if="!entries.length">
            <td colspan="5" class="empty">{{ searchQuery ? t('common.no_matches') : t('dns.empty') }}</td>
          </tr>
        </tbody>
      </table>
      <Pager v-model:offset="offset" v-model:limit="limit" :total="total" :page-sizes="DNS_PAGE_SIZES" />
    </div>

  </template>

  <!-- ------------------------------------------------------------ allowlist -->
  <template v-if="tab === 'allowlist'">
    <p class="muted small">{{ t('dns.allow_intro') }}</p>
    <div class="toolbar">
      <span class="grow"></span>
      <button v-if="canWrite" @click="openAllow">{{ t('dns.allow_add') }}</button>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Domain</th>
            <th>{{ t('dns.reason') }}</th>
            <th>{{ t('dns.added_by') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in allowPage.rows" :key="entry.id">
            <td>
              <strong>{{ entry.domain }}</strong>
              <span v-if="entry.includeSubdomains" class="badge" style="margin-left: 6px">{{ t('dns.with_subdomains') }}</span>
            </td>
            <td>{{ entry.reason || '–' }}</td>
            <td>{{ entry.addedBy }}</td>
            <td>
              <button v-if="canWrite" class="danger small" @click="removeAllow(entry)">{{ t('common.delete') }}</button>
            </td>
          </tr>
          <tr v-if="allowlist">
            <td colspan="4" class="muted small">
              <span class="badge info">{{ t('dns.builtin') }}</span>
              {{ t('dns.public_suffixes', { count: allowlist.public_suffixes.toLocaleString() }) }}
              <span class="mono">({{ allowlist.public_suffix_source }})</span>
            </td>
          </tr>
        </tbody>
      </table>
      <Pager v-model:offset="allowPage.offset" v-model:limit="allowPage.limit" :total="allowPage.total" />
    </div>
  </template>

  <!-- ------------------------------------------------------------ dialogs -->
  <Modal v-if="createOpen" :title="t('dns.manual_title')" @close="createOpen = false">
    <form class="stack" @submit.prevent="createEntry">
      <div v-if="createError" class="alert err">{{ createError }}</div>
      <div class="field">
        <label>Domain</label>
        <input v-model="form.domain" required placeholder="example-blocked.com" />
      </div>
      <label class="check">
        <input v-model="form.includeSubdomains" type="checkbox" /> {{ t('dns.include_subdomains') }}
      </label>
      <div class="field">
        <label>{{ t('dns.reason') }}</label>
        <input v-model="form.reason" :placeholder="t('dns.reason_placeholder')" />
      </div>
      <div class="field">
        <label>{{ t('dns.confidence_label') }}</label>
        <input v-model="form.confidence" type="number" min="0" max="1" step="0.01" placeholder="0.94" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="createOpen = false">{{ t('common.cancel') }}</button>
        <button>{{ t('dns.add') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="allowOpen" :title="t('dns.allow_title')" @close="allowOpen = false">
    <form class="stack" @submit.prevent="createAllow">
      <div v-if="allowError" class="alert err">{{ allowError }}</div>
      <div class="field">
        <label>Domain</label>
        <input v-model="allowForm.domain" required placeholder="iskola.hu" />
      </div>
      <label class="check">
        <input v-model="allowForm.includeSubdomains" type="checkbox" /> {{ t('dns.allow_include_subdomains') }}
      </label>
      <div class="field">
        <label>{{ t('dns.reason') }}</label>
        <input v-model="allowForm.reason" :placeholder="t('dns.allow_reason_placeholder')" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="allowOpen = false">{{ t('common.cancel') }}</button>
        <button>{{ t('dns.add') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="importOpen" :title="t('dns.import_title')" @close="closeImport">
    <div class="stack">
      <div v-if="importError" class="alert err">{{ importError }}</div>
      <div class="field">
        <label>{{ t('dns.hosts_file') }}</label>
        <input
          type="file"
          accept=".txt,text/plain"
          :disabled="uploading !== null || ['checking', 'saving', 'syncing'].includes(importJob?.state ?? '')"
          @change="chooseFile"
        />
      </div>

      <div v-if="importStep" class="field">
        <label>
          {{ t('dns.import_step', { step: importStep.step, what: t(`dns.import_phase.${importStep.phase}`) }) }}
          <template v-if="importStep.fraction !== null"> · {{ Math.round(importStep.fraction * 100) }}%</template>
          <template v-if="importStep.count"> · {{ t('dns.import_count', importStep.count) }}</template>
        </label>
        <progress class="import-progress" max="1" :value="importStep.fraction ?? undefined"></progress>
        <p v-if="importStep.phase === 'saving' || importStep.phase === 'syncing'" class="muted small">
          {{ t('dns.import_background') }}
        </p>
      </div>

      <template v-if="importJob?.state === 'ready'">
        <div class="alert" :class="importJob.new ? 'ok' : 'warn'">
          {{ t('dns.import_summary', { new: importJob.new, existing: importJob.existing, skipped: importJob.skipped }) }}
          <template v-if="!importJob.new"> {{ t('dns.import_nothing_new') }}</template>
        </div>
        <div class="actions">
          <button
            v-for="kind in ['new', 'existing', 'skipped'] as const"
            :key="kind"
            type="button"
            class="small"
            :class="previewKind === kind ? '' : 'ghost'"
            :disabled="!importJob[kind]"
            @click="showPreview(kind)"
          >
            {{ t(`dns.import_kind.${kind}`, { count: importJob[kind] }) }}
          </button>
        </div>
        <div v-if="previewRows.length" class="chips">
          <span v-for="domain in previewRows" :key="domain" class="badge mono">{{ domain }}</span>
        </div>
        <Pager
          v-if="previewTotal > DNS_PAGE_SIZES[0]"
          v-model:offset="previewOffset"
          v-model:limit="previewLimit"
          :total="previewTotal"
          :page-sizes="DNS_PAGE_SIZES"
        />
      </template>

      <template v-if="importJob?.state === 'done'">
        <div class="alert ok">{{ t('dns.import_done', { added: importJob.added ?? 0 }) }}</div>
        <div v-if="importJob.sync_ok === false" class="alert warn">{{ t('code.dns.sync_failed') }}</div>
      </template>

      <div class="modal-foot">
        <button type="button" class="ghost" @click="closeImport">
          {{ importJob?.state === 'done' ? t('common.close') : t('common.cancel') }}
        </button>
        <button v-if="importJob?.state === 'ready'" :disabled="!importJob.new" @click="confirmImport">
          {{ t('dns.import_confirm', { count: importJob.new }) }}
        </button>
      </div>
    </div>
  </Modal>
</template>
<style scoped>
.block-page-lang {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
</style>
