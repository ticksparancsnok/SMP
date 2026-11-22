<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { PLANNED, session } from '../session'

const route = useRoute()
const planned = computed(() => PLANNED.find((p) => p.path === route.path))
// A backend module is enabled and has this UI route, but nobody wrote its page yet.
const moduleRoute = computed(() =>
  session.modules.flatMap((m) => m.ui_routes.map((r) => ({ ...r, module: m }))).find((r) => r.path === route.path),
)
</script>

<template>
  <div v-if="planned" class="card stack" style="max-width: 640px">
    <h1>{{ planned.icon }} {{ planned.title }}</h1>
    <p>{{ planned.blurb }}</p>
    <span class="badge warning" style="align-self: flex-start">Fejlesztés alatt</span>
  </div>
  <div v-else-if="moduleRoute" class="card stack" style="max-width: 640px">
    <h1>{{ moduleRoute.icon }} {{ moduleRoute.title }}</h1>
    <p class="muted">
      A(z) <span class="mono">{{ moduleRoute.module.name }}</span> modul be van kapcsolva, de a felülete még nem készült el.
    </p>
  </div>
  <div v-else class="card stack" style="max-width: 640px">
    <h1>Az oldal nem található</h1>
    <RouterLink to="/">Vissza a kezdőlapra</RouterLink>
  </div>
</template>
