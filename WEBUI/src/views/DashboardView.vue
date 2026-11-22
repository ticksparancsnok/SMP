<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Widget } from '../api/types'
import { fmtRelative } from '../format'
import { hasModule, isStaff, session } from '../session'

// Cards come from GET /dashboard: every backend module contributes its own via
// Module.dashboard(). The placeholders below disappear once their module exists.
const PLANNED_CARDS = [
  { module: 'classroom', title: 'Monitorozott termek', icon: '🖥️', link: '/classroom', text: 'Aktív órák, zárolt képernyők termenként.' },
  { module: 'software', title: 'Telepítések', icon: '⏬', link: '/software', text: 'Folyamatban lévő és sikertelen telepítések.' },
]

// Every card refreshes on this interval while the tab is visible, and right away when
// the user comes back to the tab. ~7 COUNT queries per refresh, cheap at school scale.
const REFRESH_MS = 10_000

const widgets = ref<Widget[]>([])
const error = ref('')
const loaded = ref(false)
const updatedAt = ref<Date | null>(null)
const busy = ref(false)
let timer: number | undefined

const stats = computed(() => widgets.value.filter((w) => w.kind !== 'list'))
const lists = computed(() => widgets.value.filter((w) => w.kind === 'list'))
const planned = computed(() => (isStaff() ? PLANNED_CARDS.filter((p) => !hasModule(p.module)) : []))

async function load() {
  if (busy.value) return // a slow request is still running; skip this tick
  busy.value = true
  try {
    widgets.value = await api.get<Widget[]>('/dashboard')
    updatedAt.value = new Date()
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
    loaded.value = true
  }
}

function start() {
  stop()
  timer = window.setInterval(load, REFRESH_MS)
}
function stop() {
  window.clearInterval(timer)
  timer = undefined
}
// No polling in background tabs; catch up immediately when the tab is shown again.
function onVisibility() {
  if (document.hidden) stop()
  else {
    load()
    start()
  }
}

onMounted(() => {
  load()
  start()
  document.addEventListener('visibilitychange', onVisibility)
})
onBeforeUnmount(() => {
  stop()
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>Kezdőlap</h1>
      <p>Szia, {{ session.me?.display_name || session.me?.username }}! Áttekintés – {{ session.me?.org_name }}</p>
    </div>
    <div class="row" style="align-items: center">
      <span v-if="updatedAt" class="muted small" title="Automatikus frissítés 10 másodpercenként">
        Frissítve: {{ updatedAt.toLocaleTimeString('hu-HU') }}
      </span>
      <button class="ghost small" :disabled="busy" aria-label="Frissítés" @click="load">↻</button>
    </div>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="stat-grid">
    <component
      :is="w.link ? RouterLink : 'div'"
      v-for="w in stats"
      :key="w.id"
      :to="w.link ?? undefined"
      class="card stat-card"
    >
      <span class="stat-icon">{{ w.icon }}</span>
      <span class="stat-value">{{ w.value ?? '–' }}</span>
      <span class="stat-title">{{ w.title }}</span>
      <span v-if="w.hint" class="muted small">{{ w.hint }}</span>
    </component>
  </div>
  <p v-if="loaded && !widgets.length && !error" class="empty">Nincs megjeleníthető statisztika.</p>

  <div class="grid" style="align-items: start">
    <div v-for="w in lists" :key="w.id" class="card">
      <div class="spread">
        <h2>{{ w.icon }} {{ w.title }}</h2>
        <RouterLink v-if="w.link" :to="w.link" class="small">Összes ›</RouterLink>
      </div>
      <ul v-if="w.items?.length" class="item-list">
        <li v-for="(it, i) in w.items" :key="i">
          <RouterLink v-if="it.link" :to="it.link" class="mono item-label">{{ it.label }}</RouterLink>
          <span v-else class="mono item-label">{{ it.label }}</span>
          <span class="muted small item-value">
            {{ it.value }}<template v-if="it.at"> · {{ fmtRelative(it.at) }}</template>
          </span>
        </li>
      </ul>
      <p v-else class="empty">Nincs adat.</p>
    </div>

    <RouterLink v-for="p in planned" :key="p.module" :to="p.link" class="card card-link planned">
      <h2>{{ p.icon }} {{ p.title }}</h2>
      <p class="muted" style="margin: 0">{{ p.text }}</p>
      <span class="badge low" style="margin-top: 10px">Fejlesztés alatt</span>
    </RouterLink>
  </div>
</template>
