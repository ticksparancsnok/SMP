<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// A freshly issued one-time password, shown once (big, copyable) with its expiry.
import { ref } from 'vue'
import { copyText } from '../clipboard'
import { fmtDate } from '../format'
import { t } from '../i18n'

const props = defineProps<{ username: string; otp: string; until: string | null; showExportHint?: boolean }>()
const copied = ref(false)

async function copy() {
  if (await copyText(props.otp, t('otp.label'))) {
    copied.value = true
    window.setTimeout(() => (copied.value = false), 1500)
  }
}
</script>

<template>
  <div class="stack">
    <div class="alert ok">
      {{ t('otp.issued_before') }} <strong class="mono">{{ props.username }}</strong>{{ t('otp.issued_after') }}
    </div>
    <div class="row" style="align-items: center">
      <span class="mono otp">{{ props.otp }}</span>
      <button type="button" class="ghost small" @click="copy">{{ copied ? t('common.copied') : t('common.copy') }}</button>
    </div>
    <p class="muted small" style="margin: 0">
      {{ t('otp.valid_until', { until: fmtDate(props.until) }) }}
      <template v-if="props.showExportHint">{{ t('otp.export_hint') }}</template>
    </p>
  </div>
</template>

<style scoped>
.otp {
  font-size: 26px;
  letter-spacing: 0.06em;
}
</style>
