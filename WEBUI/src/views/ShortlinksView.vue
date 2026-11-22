<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { api, errorText } from '../api/client'
import { copyText } from '../clipboard'
import type { ShortLink } from '../api/types'
import { fmtDate, fromLocalInput } from '../format'
import { t } from '../i18n'
import { can } from '../session'

const links = ref<ShortLink[]>([])
const showAll = ref(false)
const error = ref('')
const form = reactive({ target_url: '', code: '', title: '', expires_at: '' })
const formError = ref('')
const copied = ref('')

const shortUrl = (code: string) => `${window.location.origin}/s/${code}`
const expired = (l: ShortLink) => !!l.expires_at && Date.parse(l.expires_at) <= Date.now()

async function load() {
  try {
    links.value = await api.get<ShortLink[]>('/shortlinks/links', { all: showAll.value || undefined })
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)
watch(showAll, load)

async function create() {
  formError.value = ''
  try {
    await api.post('/shortlinks/links', {
      target_url: form.target_url,
      code: form.code || null,
      title: form.title,
      expires_at: fromLocalInput(form.expires_at),
    })
    Object.assign(form, { target_url: '', code: '', title: '', expires_at: '' })
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function toggle(l: ShortLink) {
  try {
    await api.patch(`/shortlinks/links/${l.id}`, { disabled: !l.disabled })
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}

async function remove(l: ShortLink) {
  if (!confirm(t('common.confirm_delete', { what: `/s/${l.code}` }))) return
  try {
    await api.del(`/shortlinks/links/${l.id}`)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}

async function copy(code: string) {
  if (!(await copyText(shortUrl(code), t('shortlinks.copy_prompt')))) return
  copied.value = code
  window.setTimeout(() => (copied.value = ''), 1500)
}
</script>

<template>
  <div class="page-head">
    <h1>{{ t('nav./shortlinks') }}</h1>
    <p>{{ t('shortlinks.subtitle') }}</p>
  </div>

  <form class="card row" @submit.prevent="create">
    <div v-if="formError" class="alert err" style="flex-basis: 100%">{{ formError }}</div>
    <div class="field" style="flex: 3 1 280px">
      <label>{{ t('shortlinks.target_url') }}</label>
      <input v-model="form.target_url" type="url" required placeholder="https://…" />
    </div>
    <div class="field">
      <label>{{ t('shortlinks.code') }}</label>
      <input v-model="form.code" class="mono" pattern="[A-Za-z0-9_\-]{3,64}" :placeholder="t('shortlinks.code_placeholder')" />
    </div>
    <div class="field">
      <label>{{ t('shortlinks.title_field') }}</label>
      <input v-model="form.title" />
    </div>
    <div class="field">
      <label>{{ t('common.expires') }}</label>
      <input v-model="form.expires_at" type="datetime-local" />
    </div>
    <button>{{ t('shortlinks.create') }}</button>
  </form>

  <div class="toolbar">
    <label v-if="can('shortlinks.links.manage_all')" class="check">
      <input v-model="showAll" type="checkbox" /> {{ t('shortlinks.show_all') }}
    </label>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('shortlinks.short_link') }}</th>
          <th>{{ t('shortlinks.target') }}</th>
          <th>{{ t('shortlinks.clicks') }}</th>
          <th>{{ t('common.expires') }}</th>
          <th>{{ t('common.status') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="l in links" :key="l.id">
          <td>
            <a :href="shortUrl(l.code)" target="_blank" class="mono">/s/{{ l.code }}</a>
            <br v-if="l.title" /><span class="muted small">{{ l.title }}</span>
          </td>
          <td class="mono small" style="max-width: 360px">{{ l.target_url }}</td>
          <td>{{ l.clicks }}</td>
          <td class="small">{{ fmtDate(l.expires_at) }}</td>
          <td>
            <span v-if="l.disabled" class="badge closed">{{ t('shortlinks.disabled') }}</span>
            <span v-else-if="expired(l)" class="badge warning">{{ t('shortlinks.expired') }}</span>
            <span v-else class="badge ok">{{ t('common.active') }}</span>
          </td>
          <td>
            <div class="actions">
              <button class="ghost small" @click="copy(l.code)">{{ copied === l.code ? t('common.copied') : t('common.copy') }}</button>
              <button class="ghost small" @click="toggle(l)">{{ l.disabled ? t('shortlinks.enable') : t('shortlinks.disable') }}</button>
              <button class="danger small" @click="remove(l)">{{ t('common.delete') }}</button>
            </div>
          </td>
        </tr>
        <tr v-if="!links.length">
          <td colspan="6" class="empty">{{ t('shortlinks.empty') }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
