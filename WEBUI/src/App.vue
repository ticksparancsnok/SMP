<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import LangSwitch from './components/LangSwitch.vue'
import LegalNotice from './components/LegalNotice.vue'
import { roleLabel } from './format'
import { t } from './i18n'
import { logout, navSections, session } from './session'

const route = useRoute()
const router = useRouter()
const navOpen = ref(false)

const sections = computed(() => navSections())
// The server's version (core module, [server] of Version.toml); the UI's own is compiled in
const version = computed(() => session.modules.find((m) => m.name === 'core')?.version ?? '')
const versionTitle = computed(() => t('app.versions', { server: version.value || '?', ui: __SMP_WEBUI_VERSION__ }))
const roleText = computed(() =>
  [...new Set((session.me?.roles ?? []).map((r) => roleLabel(r.role)))].join(', '),
)

watch(
  () => route.fullPath,
  () => (navOpen.value = false),
)

async function signOut() {
  await logout()
  router.push('/login')
}
</script>

<template>
  <RouterView v-if="route.meta.public || route.meta.bare" />
  <div v-else-if="session.me" class="shell" :class="{ 'nav-open': navOpen }">
    <header class="topbar">
      <button class="ghost" :aria-label="t('app.menu')" :aria-expanded="navOpen" @click="navOpen = !navOpen">☰</button>
      <img class="topbar-logo" src="../Assets/SMP-logo-dark.png" alt="SMP" width="164" height="144" />
    </header>
    <div class="backdrop" @click="navOpen = false"></div>
    <aside class="sidebar">
      <div class="brand">
        <!-- width/height: the file's real size, so the browser reserves the space (no
             jump while loading); CSS sets the displayed size (app.css, "brand") -->
        <img class="brand-logo" src="../Assets/SMP-logo-dark.png" alt="SMP" width="164" height="144" />
        <small :title="versionTitle">{{ version }}</small>
      </div>
      <nav class="nav">
        <template v-for="s in sections" :key="s.title">
          <div class="nav-group">{{ s.title }}</div>
          <RouterLink
            v-for="e in s.entries"
            :key="e.path"
            :to="e.path"
            :class="{ off: e.planned }"
            :title="e.planned ? t('common.in_development') : ''"
          >
            <span class="ico">{{ e.icon }}</span>{{ e.title }}
          </RouterLink>
        </template>
      </nav>
      <div class="sidebar-foot">
        <div class="who">
          <strong>{{ session.me.display_name || session.me.username }}</strong><br />
          {{ roleText }} · {{ session.me.org_name }}
        </div>
        <div class="row" style="align-items: center; justify-content: space-between">
          <button class="ghost small" @click="signOut">{{ t('common.sign_out') }}</button>
          <LangSwitch />
        </div>
        <LegalNotice class="sidebar-legal" />
      </div>
    </aside>
    <main class="main">
      <RouterView :key="route.path" />
    </main>
  </div>
</template>
