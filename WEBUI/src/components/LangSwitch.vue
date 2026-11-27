<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// HU | EN toggle.
//   <LangSwitch />                the UI's language: remembered in this browser (i18n/index.ts)
//                                 and, when signed in, on the user's account (session.ts)
//   <LangSwitch v-model="lang" /> any other HU/EN choice (e.g. the block page's language)
import { computed } from 'vue'
import { locale, LOCALES, t, type Locale } from '../i18n'

const props = defineProps<{ modelValue?: Locale; disabled?: boolean; label?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: Locale] }>()

const current = computed(() => props.modelValue ?? locale.value)
function choose(code: Locale) {
  if (props.modelValue === undefined) locale.value = code
  else if (code !== props.modelValue) emit('update:modelValue', code)
}
</script>

<template>
  <div class="lang-switch" role="group" :aria-label="label ?? t('lang.label')">
    <button
      v-for="l in LOCALES"
      :key="l.code"
      type="button"
      :class="{ active: current === l.code }"
      :aria-pressed="current === l.code"
      :lang="l.code"
      :title="l.name"
      :disabled="disabled"
      @click="choose(l.code)"
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
