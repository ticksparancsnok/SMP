<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { t } from '../i18n'
import { PLANNED, plannedBlurb, routeTitle, session } from '../session'

const route = useRoute()
const planned = computed(() => PLANNED.find((p) => p.path === route.path))
// A backend module is enabled and has this UI route, but nobody wrote its page yet.
const moduleRoute = computed(() =>
  session.modules.flatMap((m) => m.ui_routes.map((r) => ({ ...r, module: m }))).find((r) => r.path === route.path),
)
</script>

<template>
  <div v-if="planned" class="card stack" style="max-width: 640px">
    <h1>{{ planned.icon }} {{ routeTitle(planned) }}</h1>
    <p>{{ plannedBlurb(planned.path) }}</p>
    <span class="badge warning" style="align-self: flex-start">{{ t('common.in_development') }}</span>
  </div>
  <div v-else-if="moduleRoute" class="card stack" style="max-width: 640px">
    <h1>{{ moduleRoute.icon }} {{ routeTitle(moduleRoute) }}</h1>
    <p class="muted">{{ t('placeholder.module_without_ui', { module: moduleRoute.module.name }) }}</p>
  </div>
  <div v-else class="card stack" style="max-width: 640px">
    <h1>{{ t('placeholder.not_found') }}</h1>
    <RouterLink to="/">{{ t('placeholder.back_home') }}</RouterLink>
  </div>
</template>
