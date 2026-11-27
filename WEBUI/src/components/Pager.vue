<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Page controls + rows-per-page picker for every list. Works on offset/limit:
//   - known total (client-side lists, /users, /devices): first/prev/next/last;
//   - total = null (keyset paging, /audit): prev/next only, `hasMore` says if
//     there is a next page and `shown` is the row count on this page.
import { computed } from 'vue'
import { intlLocale, t } from '../i18n'
import { PAGE_SIZES } from '../paging'

const props = withDefaults(
  defineProps<{
    total: number | null
    offset: number
    limit: number
    hasMore?: boolean
    shown?: number
    pageSizes?: readonly number[]
  }>(),
  { hasMore: false, shown: 0, pageSizes: () => PAGE_SIZES },
)
const emit = defineEmits<{ 'update:offset': [value: number]; 'update:limit': [value: number] }>()

const page = computed(() => Math.floor(props.offset / props.limit) + 1)
const pages = computed(() => (props.total === null ? null : Math.max(1, Math.ceil(props.total / props.limit))))
const canPrev = computed(() => props.offset > 0)
const canNext = computed(() =>
  props.total === null ? props.hasMore : props.offset + props.limit < props.total,
)
// Hidden while everything fits on the smallest page (nothing to page or resize).
const visible = computed(() =>
  props.total === null
    ? canPrev.value || canNext.value || props.shown > props.pageSizes[0]
    : props.total > props.pageSizes[0],
)

const fmt = (n: number) => n.toLocaleString(intlLocale())
const range = computed(() => {
  const last = props.total ?? props.offset + props.shown
  if (!last) return ''
  const span = `${fmt(props.offset + 1)}–${fmt(Math.min(props.offset + props.limit, last))}`
  return props.total === null ? span : `${span} / ${fmt(props.total)}`
})

const go = (offset: number) => emit('update:offset', Math.max(0, offset))

function setLimit(e: Event) {
  const limit = Number((e.target as HTMLSelectElement).value)
  emit('update:limit', limit)
  // Keep the first row that was on screen on screen.
  go(Math.floor(props.offset / limit) * limit)
}
</script>

<template>
  <div v-if="visible" class="pager">
    <span class="muted">{{ range }}</span>
    <span class="grow"></span>
    <label class="muted">
      {{ t('pager.per_page') }}
      <select :value="props.limit" :aria-label="t('pager.per_page')" @change="setLimit">
        <option v-for="size in props.pageSizes" :key="size" :value="size">{{ size }}</option>
      </select>
    </label>
    <button v-if="pages !== null" class="ghost small" :disabled="!canPrev" :title="t('pager.first')" @click="go(0)">«</button>
    <button class="ghost small" :disabled="!canPrev" @click="go(props.offset - props.limit)">
      {{ t('pager.prev') }}
    </button>
    <span v-if="pages !== null">{{ fmt(page) }} / {{ fmt(pages) }}</span>
    <span v-else>{{ fmt(page) }}</span>
    <button class="ghost small" :disabled="!canNext" @click="go(props.offset + props.limit)">
      {{ t('pager.next') }}
    </button>
    <button
      v-if="pages !== null"
      class="ghost small"
      :disabled="!canNext"
      :title="t('pager.last')"
      @click="go((pages - 1) * props.limit)"
    >
      »
    </button>
  </div>
</template>
