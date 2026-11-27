<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts" generic="T extends { id: string }">
// Pick one item from a list too big to download whole (devices, users): a search box
// that asks the server, and a <select> of the first matches. The chosen item stays in
// the select even when the search no longer matches it.
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { errorText } from '../api/client'
import { t } from '../i18n'

const MATCHES = 50

const props = defineProps<{
  modelValue: string
  /** One page of matches for the search text (e.g. GET /devices?q=…&limit=…). */
  search: (q: string, limit: number) => Promise<{ items: T[]; total: number }>
  label: (item: T) => string
  /** The already chosen item, so it can be shown before any search (e.g. a suggestion). */
  initial?: T | null
  required?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const q = ref('')
const items = shallowRef<T[]>([])
const total = ref(0)
const error = ref('')
const chosen = shallowRef<T | null>(props.initial ?? null)
watch(
  () => props.initial,
  (v) => v && (chosen.value = v),
)

let seq = 0
async function fetchMatches() {
  const mine = ++seq
  try {
    const page = await props.search(q.value.trim(), MATCHES)
    if (mine !== seq) return
    items.value = page.items
    total.value = page.total
    error.value = ''
  } catch (e) {
    if (mine === seq) error.value = errorText(e)
  }
}

let debounce: number | undefined
watch(q, () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(fetchMatches, 250)
})
onMounted(fetchMatches)

const options = computed(() => {
  const c = chosen.value
  return c && c.id === props.modelValue && !items.value.some((i) => i.id === c.id) ? [c, ...items.value] : items.value
})

function pick(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  chosen.value = options.value.find((o) => o.id === id) ?? null
  emit('update:modelValue', id)
}
</script>

<template>
  <div class="remote-picker">
    <input v-model="q" type="search" :placeholder="t('picker.search')" :aria-label="t('picker.search')" />
    <select :value="props.modelValue" :required="props.required" @change="pick">
      <option value="" disabled>{{ t('policies.choose') }}</option>
      <option v-for="o in options" :key="o.id" :value="o.id">{{ props.label(o) }}</option>
    </select>
    <span v-if="error" class="muted small">{{ error }}</span>
    <span v-else-if="total > items.length" class="muted small">
      {{ t('picker.more', { shown: items.length, total: total.toLocaleString() }) }}
    </span>
    <span v-else-if="!items.length" class="muted small">{{ t('common.no_matches') }}</span>
  </div>
</template>

<style scoped>
.remote-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
