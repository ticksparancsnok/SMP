<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import LangSwitch from './components/LangSwitch.vue'
import { roleLabel } from './format'
import { t } from './i18n'
import { logout, navSections, session } from './session'

const route = useRoute()
const router = useRouter()
const navOpen = ref(false)

const sections = computed(() => navSections())
const version = computed(() => session.modules.find((m) => m.name === 'core')?.version ?? '')
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
      <strong>SMP</strong>
    </header>
    <div class="backdrop" @click="navOpen = false"></div>
    <aside class="sidebar">
      <div class="brand">
        <span><img src="../Assets/SMP-transpararent.png" alt="logo" height="20px"></span>
        <div>
          SMP<small>{{ version }}</small>
        </div>
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
      </div>
    </aside>
    <main class="main">
      <RouterView :key="route.path" />
    </main>
  </div>
</template>
