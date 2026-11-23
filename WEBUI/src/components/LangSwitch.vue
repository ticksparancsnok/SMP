<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// HU | EN toggle. Remembered in this browser (i18n/index.ts) and, when signed in, on the
// user's account (session.ts), so it follows them to any computer.
import { locale, LOCALES, t } from '../i18n'
</script>

<template>
  <div class="lang-switch" role="group" :aria-label="t('lang.label')">
    <button
      v-for="l in LOCALES"
      :key="l.code"
      type="button"
      :class="{ active: locale === l.code }"
      :aria-pressed="locale === l.code"
      :lang="l.code"
      :title="l.name"
      @click="locale = l.code"
    >
      {{ l.label }}
    </button>
  </div>
</template>

<style scoped>
.lang-switch {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  flex: none;
}
.lang-switch button {
  background: transparent;
  color: var(--muted);
  border-radius: 0;
  padding: 4px 9px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.lang-switch button + button {
  border-left: 1px solid var(--border);
}
.lang-switch button.active {
  background: var(--accent-soft);
  color: var(--text);
}
</style>
