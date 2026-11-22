<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Token, TokenCreated } from '../api/types'
import SchemaForm from '../components/SchemaForm.vue'
import { fmtDate, fmtRelative, fromLocalInput } from '../format'
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
    status[name] = { ok: true, text: 'Mentve.' }
  } catch (e) {
    status[name] = { ok: false, text: errorText(e) }
  }
}

// ---------------------------------------------------------------- API tokens
const tokens = ref<Token[]>([])
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

async function revoke(t: Token) {
  if (!confirm(`Visszavonod a(z) "${t.name}" tokent? Az ezt használó szolgáltatás leáll.`)) return
  try {
    await api.del(`/tokens/${t.id}`)
    loadTokens()
  } catch (e) {
    tokenError.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head">
    <h1>Beállítások</h1>
    <p>Modulonkénti beállítások és a szolgáltatások (pl. DNS szerver) API tokenjei.</p>
  </div>

  <div class="grid">
    <form v-for="m in withSettings" :key="m.name" class="card stack" @submit.prevent="saveSettings(m.name)">
      <h2>{{ m.title }} <span class="badge mono">{{ m.name }}</span></h2>
      <div v-if="status[m.name]" class="alert" :class="status[m.name]!.ok ? 'ok' : 'err'">{{ status[m.name]!.text }}</div>
      <SchemaForm v-if="values[m.name]" v-model="values[m.name]!" :schema="m.settings_schema!" :disabled="!canWrite" />
      <div v-if="canWrite"><button>Mentés</button></div>
    </form>
  </div>

  <div v-if="can('core.tokens.manage')" class="card">
    <h2>API tokenek</h2>
    <p class="muted small">
      Szolgáltatások (DNS szerver, scriptek) ezzel azonosítják magukat: <code class="mono">Authorization: Bearer …</code>.
      Csak a szükséges jogosultságokat add meg.
    </p>
    <div v-if="tokenError" class="alert err">{{ tokenError }}</div>
    <div v-if="created" class="alert ok">
      Token létrehozva. Most másold ki, később nem lesz látható:<br />
      <span class="mono">{{ created.token }}</span>
      <button class="ghost small" style="margin-left: 8px" @click="created = null">OK</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Név</th>
            <th>Jogosultságok</th>
            <th>Létrehozva</th>
            <th>Utoljára használva</th>
            <th>Lejár</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in tokens" :key="t.id">
            <td>
              <strong>{{ t.name }}</strong><br /><span class="mono muted small">{{ t.prefix }}…</span>
            </td>
            <td><div class="chips">
              <span v-for="p in t.permissions" :key="p" class="badge mono">{{ p }}</span>
            </div></td>
            <td class="small">{{ fmtDate(t.created_at) }}</td>
            <td class="small">{{ fmtRelative(t.last_used_at) }}</td>
            <td class="small">{{ fmtDate(t.expires_at) }}</td>
            <td>
              <span v-if="t.revoked_at" class="badge closed">Visszavonva</span>
              <div v-else class="actions"><button class="danger small" @click="revoke(t)">Visszavonás</button></div>
            </td>
          </tr>
          <tr v-if="!tokens.length">
            <td colspan="6" class="empty">Nincs token.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <form class="stack" style="margin-top: 16px" @submit.prevent="createToken">
      <h3>Új token</h3>
      <div class="row">
        <div class="field">
          <label>Név</label>
          <input v-model="tokenForm.name" required placeholder="pl. dns-szerver" />
        </div>
        <div class="field">
          <label>Lejárat (opcionális)</label>
          <input v-model="tokenForm.expires_at" type="datetime-local" />
        </div>
      </div>
      <div class="check-list">
        <template v-for="m in session.modules" :key="m.name">
          <div v-if="m.permissions.length" class="nav-group" style="padding-left: 0">{{ m.title }}</div>
          <label v-for="p in m.permissions" :key="p.code" class="check" style="padding: 2px 0">
            <input v-model="tokenForm.permissions" type="checkbox" :value="p.code" />
            <span class="mono small">{{ p.code }}</span> <span class="muted small">– {{ p.description }}</span>
          </label>
        </template>
      </div>
      <div><button :disabled="!tokenForm.permissions.length">Token létrehozása</button></div>
    </form>
  </div>
</template>
