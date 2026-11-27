<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import { api, errorText } from '../api/client'
import type { Token, TokenCreated } from '../api/types'
import LockLogoSettings from '../components/LockLogoSettings.vue'
import Pager from '../components/Pager.vue'
import SchemaForm from '../components/SchemaForm.vue'
import { fmtDate, fmtRelative, fromLocalInput, moduleTitle } from '../format'
import { t, tOr } from '../i18n'
import { usePage } from '../paging'
import { can, session } from '../session'

const canWrite = computed(() => can('core.settings.write'))
const withSettings = computed(() => session.modules.filter((m) => m.settings_schema))

// ---------------------------------------------------------------- module settings
const values = reactive<Record<string, Record<string, unknown>>>({})
const status = reactive<Record<string, { ok: boolean; text: string } | undefined>>({})

onMounted(async () => {
  for (const m of withSettings.value) {
    try {
      values[m.name] = await api.get<Record<string, unknown>>(`/modules/${m.name}/settings`)
    } catch (e) {
      status[m.name] = { ok: false, text: errorText(e) }
    }
  }
  if (can('core.tokens.manage')) loadTokens()
})

async function saveSettings(name: string) {
  status[name] = undefined
  try {
    values[name] = await api.put<Record<string, unknown>>(`/modules/${name}/settings`, values[name])
    status[name] = { ok: true, text: t('common.saved') }
  } catch (e) {
    status[name] = { ok: false, text: errorText(e) }
  }
}

// ---------------------------------------------------------------- API tokens
const tokens = shallowRef<Token[]>([])
const tokenPage = usePage(tokens, { key: 'tokens' })
const tokenForm = reactive({ name: '', permissions: [] as string[], expires_at: '' })
const tokenError = ref('')
const created = ref<TokenCreated | null>(null)

async function loadTokens() {
  try {
    tokens.value = await api.get<Token[]>('/tokens')
  } catch (e) {
    tokenError.value = errorText(e)
  }
}

async function createToken() {
  tokenError.value = ''
  try {
    created.value = await api.post<TokenCreated>('/tokens', {
      name: tokenForm.name,
      permissions: tokenForm.permissions,
      expires_at: fromLocalInput(tokenForm.expires_at),
    })
    Object.assign(tokenForm, { name: '', permissions: [], expires_at: '' })
    loadTokens()
  } catch (e) {
    tokenError.value = errorText(e)
  }
}

async function revoke(token: Token) {
  if (!confirm(t('tokens.confirm_revoke', { name: token.name }))) return
  try {
    await api.del(`/tokens/${token.id}`)
    loadTokens()
  } catch (e) {
    tokenError.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head">
    <h1>{{ t('nav./settings') }}</h1>
    <p>{{ t('settings.subtitle') }}</p>
  </div>

  <div class="grid">
    <form v-for="m in withSettings" :key="m.name" class="card stack" @submit.prevent="saveSettings(m.name)">
      <h2>{{ moduleTitle(m) }} <span class="badge mono">{{ m.name }}</span></h2>
      <div v-if="status[m.name]" class="alert" :class="status[m.name]!.ok ? 'ok' : 'err'">{{ status[m.name]!.text }}</div>
      <SchemaForm v-if="values[m.name]" v-model="values[m.name]!" :schema="m.settings_schema!" :disabled="!canWrite" :i18n-prefix="`settings.${m.name}`" />
      <div v-if="canWrite"><button>{{ t('common.save') }}</button></div>
      <!-- not a setting field: its own upload, saved at once (the buttons don't submit this form) -->
      <LockLogoSettings v-if="m.name === 'classroom'" :disabled="!canWrite" />
    </form>
  </div>

  <div v-if="can('core.tokens.manage')" class="card">
    <h2>{{ t('tokens.title') }}</h2>
    <p class="muted small">
      {{ t('tokens.intro_before') }} <code class="mono">Authorization: Bearer …</code>.
      {{ t('tokens.intro_after') }}
    </p>
    <div v-if="tokenError" class="alert err">{{ tokenError }}</div>
    <div v-if="created" class="alert ok">
      {{ t('tokens.created') }}<br />
      <span class="mono">{{ created.token }}</span>
      <button class="ghost small" style="margin-left: 8px" @click="created = null">{{ t('common.ok') }}</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{{ t('common.name') }}</th>
            <th>{{ t('common.permissions') }}</th>
            <th>{{ t('tokens.created_at') }}</th>
            <th>{{ t('tokens.last_used') }}</th>
            <th>{{ t('common.expires') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tok in tokenPage.rows" :key="tok.id">
            <td>
              <strong>{{ tok.name }}</strong><br /><span class="mono muted small">{{ tok.prefix }}…</span>
            </td>
            <td><div class="chips">
              <span v-for="p in tok.permissions" :key="p" class="badge mono">{{ p }}</span>
            </div></td>
            <td class="small">{{ fmtDate(tok.created_at) }}</td>
            <td class="small">{{ fmtRelative(tok.last_used_at) }}</td>
            <td class="small">{{ fmtDate(tok.expires_at) }}</td>
            <td>
              <span v-if="tok.revoked_at" class="badge closed">{{ t('tokens.revoked') }}</span>
              <div v-else class="actions"><button class="danger small" @click="revoke(tok)">{{ t('tokens.revoke') }}</button></div>
            </td>
          </tr>
          <tr v-if="!tokens.length">
            <td colspan="6" class="empty">{{ t('tokens.empty') }}</td>
          </tr>
        </tbody>
      </table>
      <Pager v-model:offset="tokenPage.offset" v-model:limit="tokenPage.limit" :total="tokenPage.total" />
    </div>

    <form class="stack" style="margin-top: 16px" @submit.prevent="createToken">
      <h3>{{ t('tokens.new') }}</h3>
      <div class="row">
        <div class="field">
          <label>{{ t('common.name') }}</label>
          <input v-model="tokenForm.name" required :placeholder="t('tokens.name_placeholder')" />
        </div>
        <div class="field">
          <label>{{ t('tokens.expiry') }}</label>
          <input v-model="tokenForm.expires_at" type="datetime-local" />
        </div>
      </div>
      <div class="check-list">
        <template v-for="m in session.modules" :key="m.name">
          <div v-if="m.permissions.length" class="nav-group" style="padding-left: 0">{{ moduleTitle(m) }}</div>
          <label v-for="p in m.permissions" :key="p.code" class="check" style="padding: 2px 0">
            <input v-model="tokenForm.permissions" type="checkbox" :value="p.code" />
            <span class="mono small">{{ p.code }}</span> <span class="muted small">– {{ tOr(`perm.${p.code}`, p.description) }}</span>
          </label>
        </template>
      </div>
      <div><button :disabled="!tokenForm.permissions.length">{{ t('tokens.create') }}</button></div>
    </form>
  </div>
</template>
