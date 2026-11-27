<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Widget } from '../api/types'
import { fmtRelative, fmtTime } from '../format'
import { t, tOr } from '../i18n'
import { hasModule, isStaff, routeTitle, session } from '../session'

// Cards come from GET /dashboard: every backend module contributes its own via
// Module.dashboard(). Placeholders for planned modules (none at the moment) go below
// and disappear once their module exists. Texts: planned.card.<link> in i18n.
const PLANNED_CARDS: { module: string; icon: string; link: string }[] = []

// The server sends English title/hint; translate by widget id, rebuilding the hint
// from widget.data so numbers and plurals fit the language.
const cardTitle = (w: Widget) => tOr(`widget.${w.id}`, w.title)
const cardHint = (w: Widget) => (w.hint ? tOr(`widget.${w.id}.hint`, w.hint, w.data ?? {}) : '')

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
      <h1>{{ routeTitle({ path: '/', title: 'Home' }) }}</h1>
      <p>{{ t('dashboard.greeting', { name: session.me?.display_name || session.me?.username || '', org: session.me?.org_name ?? '' }) }}</p>
    </div>
    <div class="row" style="align-items: center">
      <span v-if="updatedAt" class="muted small" :title="t('dashboard.auto_refresh')">
        {{ t('dashboard.updated', { time: fmtTime(updatedAt) }) }}
      </span>
      <button class="ghost small" :disabled="busy" :aria-label="t('dashboard.refresh')" @click="load">↻</button>
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
      <span class="stat-title">{{ cardTitle(w) }}</span>
      <span v-if="w.hint" class="muted small">{{ cardHint(w) }}</span>
    </component>
  </div>
  <p v-if="loaded && !widgets.length && !error" class="empty">{{ t('dashboard.no_stats') }}</p>

  <div class="grid" style="align-items: start">
    <div v-for="w in lists" :key="w.id" class="card">
      <div class="spread">
        <h2>{{ w.icon }} {{ cardTitle(w) }}</h2>
        <RouterLink v-if="w.link" :to="w.link" class="small">{{ t('dashboard.see_all') }}</RouterLink>
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
      <p v-else class="empty">{{ t('common.no_data') }}</p>
    </div>

    <RouterLink v-for="p in planned" :key="p.module" :to="p.link" class="card card-link planned">
      <h2>{{ p.icon }} {{ tOr(`planned.card.${p.link}`, p.module) }}</h2>
      <p class="muted" style="margin: 0">{{ tOr(`planned.card.${p.link}.text`, '') }}</p>
      <span class="badge low" style="margin-top: 10px">{{ t('common.in_development') }}</span>
    </RouterLink>
  </div>
</template>
