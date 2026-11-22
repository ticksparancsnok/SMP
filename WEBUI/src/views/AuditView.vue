<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, errorText } from '../api/client'
import type { AuditEntry } from '../api/types'
import { fmtDate, fromLocalInput } from '../format'
import { t } from '../i18n'

const LIMIT = 100
const route = useRoute()
const filters = reactive({
  action: typeof route.query.action === 'string' ? route.query.action : '',
  target_type: '',
  since: '',
  until: '',
})
const entries = ref<AuditEntry[]>([])
const more = ref(false)
const error = ref('')

async function load(append = false) {
  try {
    const page = await api.get<AuditEntry[]>('/audit', {
      action: filters.action.trim(),
      target_type: filters.target_type.trim(),
      since: fromLocalInput(filters.since),
      until: fromLocalInput(filters.until),
      before_id: append ? entries.value.at(-1)?.id : undefined,
      limit: LIMIT,
    })
    entries.value = append ? [...entries.value, ...page] : page
    more.value = page.length === LIMIT
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(() => load())
</script>

<template>
  <div class="page-head">
    <h1>{{ t('nav./audit') }}</h1>
    <p>{{ t('audit.subtitle') }}</p>
  </div>

  <form class="toolbar" @submit.prevent="load()">
    <div class="field">
      <label>{{ t('audit.action_filter') }}</label>
      <input v-model="filters.action" class="mono" placeholder="core.user." />
    </div>
    <div class="field">
      <label>{{ t('audit.target_type') }}</label>
      <input v-model="filters.target_type" class="mono" placeholder="device" />
    </div>
    <div class="field">
      <label>{{ t('audit.since') }}</label>
      <input v-model="filters.since" type="datetime-local" />
    </div>
    <div class="field">
      <label>{{ t('audit.until') }}</label>
      <input v-model="filters.until" type="datetime-local" />
    </div>
    <button>{{ t('audit.filter') }}</button>
  </form>

  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('audit.time') }}</th>
          <th>{{ t('audit.who') }}</th>
          <th>{{ t('audit.action') }}</th>
          <th>{{ t('audit.target') }}</th>
          <th>{{ t('audit.details') }}</th>
          <th>IP</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in entries" :key="e.id">
          <td class="small" style="white-space: nowrap">{{ fmtDate(e.at) }}</td>
          <td>{{ e.actor_name || e.actor_type }}</td>
          <td>
            <a href="#" class="mono" @click.prevent="(filters.action = e.action), load()">{{ e.action }}</a>
          </td>
          <td class="mono small">{{ e.target_type }}<br />{{ e.target_id }}</td>
          <td style="max-width: 360px">
            <details v-if="Object.keys(e.details).length">
              <summary>{{ t('audit.open') }}</summary>
              <pre class="json">{{ JSON.stringify(e.details, null, 2) }}</pre>
            </details>
          </td>
          <td class="mono small">{{ e.ip }}</td>
        </tr>
        <tr v-if="!entries.length">
          <td colspan="6" class="empty">{{ t('audit.empty') }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="more" class="pager">
      <button class="ghost small" @click="load(true)">{{ t('audit.more') }}</button>
    </div>
  </div>
</template>
