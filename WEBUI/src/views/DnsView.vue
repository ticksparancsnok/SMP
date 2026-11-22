<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Decision order (same in the server and the Go sinkhole): allowlist -> blocklist ->
// everything else allowed. Public suffixes (com, co.uk, ...) can't go on either list.
import { onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type {
  DnsAllowEntry,
  DnsAllowIn,
  DnsAllowlist,
  DnsEntry,
  DnsEntryIn,
  DnsHostsPreview,
  DnsStatus,
} from '../api/types'
import Modal from '../components/Modal.vue'
import { t, tOr } from '../i18n'
import { can } from '../session'

const tab = ref<'blocklist' | 'allowlist'>('blocklist')
const entries = ref<DnsEntry[]>([])
const allowlist = ref<DnsAllowlist | null>(null)
const status = ref<DnsStatus | null>(null)
const error = ref('')
const busy = ref(false)
const canWrite = can('dns.blocklist.write')

// ---------------------------------------------------------------- blocklist entry dialog
const createOpen = ref(false)
const createError = ref('')
const form = reactive({ domain: '', includeSubdomains: true, reason: '', confidence: '' })

// ---------------------------------------------------------------- allowlist entry dialog
const allowOpen = ref(false)
const allowError = ref('')
const allowForm = reactive({ domain: '', includeSubdomains: true, reason: '' })

// ---------------------------------------------------------------- hosts import
const importOpen = ref(false)
const importFile = ref<File | null>(null)
const preview = ref<DnsHostsPreview | null>(null)
const importError = ref('')

async function load() {
  busy.value = true
  try {
    ;[entries.value, allowlist.value, status.value] = await Promise.all([
      api.get<DnsEntry[]>('/dns/blocklist'),
      api.get<DnsAllowlist>('/dns/allowlist'),
      api.get<DnsStatus>('/dns/status'),
    ])
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

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
  } catch (e) {
    createError.value = errorText(e)
    return
  } finally {
    await load() // also after a sync error: the entry is saved either way
  }
}

async function removeEntry(entry: DnsEntry) {
  if (!confirm(t('common.confirm_delete', { what: entry.domain }))) return
  try {
    await api.del(`/dns/blocklist/${entry.id}`)
  } catch (e) {
    error.value = errorText(e)
  }
  await load()
}

function openAllow() {
  Object.assign(allowForm, { domain: '', includeSubdomains: true, reason: '' })
  allowError.value = ''
  allowOpen.value = true
}

async function createAllow() {
  allowError.value = ''
  try {
    await api.post('/dns/allowlist', {
      domain: allowForm.domain,
      include_subdomains: allowForm.includeSubdomains,
      reason: allowForm.reason,
    } satisfies DnsAllowIn)
    allowOpen.value = false
  } catch (e) {
    allowError.value = errorText(e)
    return
  } finally {
    await load()
  }
}

async function removeAllow(entry: DnsAllowEntry) {
  if (!confirm(t('common.confirm_delete', { what: entry.domain }))) return
  try {
    await api.del(`/dns/allowlist/${entry.id}`)
  } catch (e) {
    error.value = errorText(e)
  }
  await load()
}

function openImport() {
  importFile.value = preview.value = null
  importError.value = ''
  importOpen.value = true
}

async function previewImport(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  importFile.value = file
  preview.value = null
  importError.value = ''
  if (!file) return
  busy.value = true
  try {
    preview.value = await api.upload<DnsHostsPreview>('/dns/blocklist/import-preview', file, file.type || 'text/plain')
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
  } catch (e) {
    importError.value = errorText(e)
  } finally {
    busy.value = false
    await load()
  }
}

onMounted(load)
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
  </div>

  <nav class="tabs">
    <a href="#" :class="{ active: tab === 'blocklist' }" @click.prevent="tab = 'blocklist'">{{ t('dns.tab.blocklist') }}</a>
    <a href="#" :class="{ active: tab === 'allowlist' }" @click.prevent="tab = 'allowlist'">{{ t('dns.tab.allowlist') }}</a>
  </nav>

  <!-- ------------------------------------------------------------ blocklist -->
  <template v-if="tab === 'blocklist'">
    <div class="toolbar">
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
            <td colspan="5" class="empty">{{ t('dns.empty') }}</td>
          </tr>
        </tbody>
      </table>
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
          <tr v-for="entry in allowlist?.entries ?? []" :key="entry.id">
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

  <Modal v-if="importOpen" :title="t('dns.import_title')" @close="importOpen = false">
    <div class="stack">
      <div v-if="importError" class="alert err">{{ importError }}</div>
      <div class="field">
        <label>{{ t('dns.hosts_file') }}</label>
        <input type="file" accept=".txt,text/plain" :disabled="busy" @change="previewImport" />
      </div>
      <div v-if="preview" class="alert ok">
        {{ t('dns.import_found', { count: preview.unique_count }) }}
        <template v-if="preview.skipped?.length">{{ t('dns.import_skipped', { count: preview.skipped.length }) }}</template>
      </div>
      <details v-if="preview?.domains.length">
        <summary>{{ t('dns.import_preview') }}</summary>
        <div class="chips" style="margin-top: 8px">
          <span v-for="domain in preview.domains" :key="domain" class="badge mono">{{ domain }}</span>
        </div>
      </details>
      <details v-if="preview?.skipped?.length">
        <summary>{{ t('dns.import_skipped_list') }}</summary>
        <div class="chips" style="margin-top: 8px">
          <span v-for="domain in preview.skipped" :key="domain" class="badge mono">{{ domain }}</span>
        </div>
      </details>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="importOpen = false">{{ t('common.cancel') }}</button>
        <button :disabled="!preview || !preview.domains.length || busy" @click="confirmImport">{{ t('common.import') }}</button>
      </div>
    </div>
  </Modal>
</template>
