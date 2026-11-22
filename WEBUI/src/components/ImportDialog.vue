<script setup lang="ts">
// Student import from .xlsx: choose a file -> server-side dry run (preview, nothing
// written) -> confirm -> real import. Any invalid row blocks the whole file.
import { computed, ref } from 'vue'
import { api, ApiError, errorText } from '../api/client'
import type { ImportResult } from '../api/types'
import Modal from './Modal.vue'

const emit = defineEmits<{ close: []; imported: [] }>()
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

const file = ref<File | null>(null)
const preview = ref<ImportResult | null>(null)
const done = ref<ImportResult | null>(null)
const error = ref('')
const busy = ref(false)

const rowErrors = computed(() => preview.value?.errors ?? [])
const canImport = computed(() => !!preview.value && !rowErrors.value.length && preview.value.created.length > 0)

async function run(dryRun: boolean): Promise<ImportResult | null> {
  if (!file.value) return null
  busy.value = true
  error.value = ''
  try {
    return await api.upload<ImportResult>('/users/import', file.value, XLSX, { dry_run: dryRun })
  } catch (e) {
    // 422 carries the same ImportResult shape with the row errors.
    const detail = e instanceof ApiError ? (e.detail as ImportResult | undefined) : undefined
    if (detail && Array.isArray(detail.errors)) return detail
    error.value = errorText(e)
    return null
  } finally {
    busy.value = false
  }
}

async function onFile(ev: Event) {
  file.value = (ev.target as HTMLInputElement).files?.[0] ?? null
  preview.value = done.value = null
  if (file.value) preview.value = await run(true)
}

async function doImport() {
  const r = await run(false)
  if (r && !r.errors.length) {
    done.value = r
    emit('imported')
  } else if (r) {
    preview.value = r // data changed since the preview
  }
}

function downloadCredentials() {
  api.download('/users/credentials.xlsx').catch((e) => (error.value = errorText(e)))
}

function sample() {
  api.download('/users/import-sample.xlsx').catch((e) => (error.value = errorText(e)))
}
</script>

<template>
  <Modal title="Diákok importálása (.xlsx)" wide @close="emit('close')">
    <div class="stack">
      <p class="muted small" style="margin: 0">
        Oszlopok: <span class="mono">Név | Felhasználónév | Osztály</span> (több osztály vesszővel elválasztva). Minden
        importált felhasználó <strong>diák</strong> lesz, és <strong>saját egyszeri jelszót</strong> kap, amivel első
        belépéskor saját jelszót választ. A már létező felhasználónevek kimaradnak, a hiányzó osztályok létrejönnek.
        <a href="#" @click.prevent="sample">Minta letöltése</a>
      </p>

      <div v-if="error" class="alert err">{{ error }}</div>

      <template v-if="!done">
        <div class="field">
          <label>Fájl</label>
          <input type="file" :accept="`.xlsx,${XLSX}`" :disabled="busy" @change="onFile" />
        </div>
        <p v-if="busy" class="muted">Feldolgozás…</p>

        <template v-if="preview && !busy">
          <div v-if="rowErrors.length" class="alert err">
            <strong>A fájl nem importálható – javítsd ezeket, majd válaszd ki újra:</strong>
            <ul style="margin: 6px 0 0; padding-left: 20px">
              <li v-for="(e, i) in rowErrors" :key="i">
                <template v-if="e.row > 0">{{ e.row }}. sor: </template>{{ e.error }}
              </li>
            </ul>
          </div>
          <template v-else>
            <div class="alert ok">
              <strong>{{ preview.created.length }}</strong> új diák lesz létrehozva<template v-if="preview.skipped.length">,
                <strong>{{ preview.skipped.length }}</strong> kimarad (már létezik)</template
              ><template v-if="preview.groups_created.length">, új osztályok: {{ preview.groups_created.join(', ') }}</template>.
            </div>
            <details v-if="preview.created.length">
              <summary>Új felhasználók</summary>
              <div class="chips" style="margin-top: 6px">
                <span v-for="u in preview.created" :key="u" class="badge mono">{{ u }}</span>
              </div>
            </details>
            <details v-if="preview.skipped.length">
              <summary>Kimaradnak (már léteznek)</summary>
              <div class="chips" style="margin-top: 6px">
                <span v-for="u in preview.skipped" :key="u" class="badge mono">{{ u }}</span>
              </div>
            </details>
          </template>
        </template>

        <div class="modal-foot">
          <button type="button" class="ghost" @click="emit('close')">Mégse</button>
          <button :disabled="!canImport || busy" @click="doImport">
            Importálás{{ canImport ? ` (${preview!.created.length})` : '' }}
          </button>
        </div>
      </template>

      <template v-else>
        <div class="alert ok">
          {{ done.created.length }} diák létrehozva<template v-if="done.groups_created.length">,
            új osztályok: {{ done.groups_created.join(', ') }}</template
          >. Mindenki saját egyszeri jelszót kapott; ezeket a belépési adatokban találod (belépőlapok nyomtatásához).
          Később is letölthetők: Exportálás → Belépési adatok.
        </div>
        <div class="modal-foot">
          <button type="button" class="ghost" @click="downloadCredentials">⬇ Belépési adatok (.xlsx)</button>
          <button @click="emit('close')">Bezárás</button>
        </div>
      </template>
    </div>
  </Modal>
</template>
