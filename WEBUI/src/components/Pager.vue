<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { t } from '../i18n'

const props = defineProps<{ total: number; offset: number; limit: number }>()
const emit = defineEmits<{ 'update:offset': [value: number] }>()
</script>

<template>
  <div v-if="props.total > props.limit" class="pager">
    <span class="muted">
      {{ props.offset + 1 }}–{{ Math.min(props.offset + props.limit, props.total) }} / {{ props.total }}
    </span>
    <button
      class="ghost small"
      :disabled="props.offset === 0"
      @click="emit('update:offset', Math.max(0, props.offset - props.limit))"
    >
      {{ t('pager.prev') }}
    </button>
    <button
      class="ghost small"
      :disabled="props.offset + props.limit >= props.total"
      @click="emit('update:offset', props.offset + props.limit)"
    >
      {{ t('pager.next') }}
    </button>
  </div>
</template>
