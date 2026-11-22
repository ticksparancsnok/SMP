<script setup lang="ts">
// HU | EN toggle. The choice is remembered in this browser (see i18n/index.ts).
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
