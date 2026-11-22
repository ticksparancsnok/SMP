<script setup lang="ts">
// A freshly issued one-time password, shown once (big, copyable) with its expiry.
import { ref } from 'vue'
import { copyText } from '../clipboard'
import { fmtDate } from '../format'

const props = defineProps<{ username: string; otp: string; until: string | null; showExportHint?: boolean }>()
const copied = ref(false)

async function copy() {
  if (await copyText(props.otp, 'Egyszeri jelszó:')) {
    copied.value = true
    window.setTimeout(() => (copied.value = false), 1500)
  }
}
</script>

<template>
  <div class="stack">
    <div class="alert ok">
      Egyszeri jelszó a(z) <strong class="mono">{{ props.username }}</strong> felhasználónak. Első belépéskor saját
      jelszót kell választania; a korábbi jelszava már nem működik.
    </div>
    <div class="row" style="align-items: center">
      <span class="mono otp">{{ props.otp }}</span>
      <button type="button" class="ghost small" @click="copy">{{ copied ? 'Másolva ✓' : 'Másolás' }}</button>
    </div>
    <p class="muted small" style="margin: 0">
      Érvényes eddig: {{ fmtDate(props.until) }}.
      <template v-if="props.showExportHint">
        Amíg nem használják fel, a belépési adatok később is letölthetők (Exportálás → Belépési adatok).
      </template>
    </p>
  </div>
</template>

<style scoped>
.otp {
  font-size: 26px;
  letter-spacing: 0.06em;
}
</style>
