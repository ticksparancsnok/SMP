<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Student import from .xlsx: choose a file -> server-side dry run (preview, nothing
// written) -> confirm -> real import. Any invalid row blocks the whole file.
import { computed, ref, watch } from 'vue'
import { api, ApiError, errorText } from '../api/client'
import type { Building, ImportResult } from '../api/types'
import { locale, t, tOr } from '../i18n'
import { can } from '../session'
import Modal from './Modal.vue'

const props = defineProps<{ buildings: Building[] }>()
const emit = defineEmits<{ close: []; imported: [] }>()
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

// Where the imported users go. Admins and unit managers may choose none (the whole
// school) or some buildings; a manager's users always go to their own buildings, all of
// them unless a subset is chosen.
const orgWide = can('core.users.write')
const buildingIds = ref(new Set<string>(orgWide ? [] : props.buildings.map((b) => b.id)))
function toggleBuilding(id: string) {
  const s = new Set(buildingIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  buildingIds.value = s
}
// The preview depends on the buildings (a class may exist in another one): redo it.
watch(buildingIds, async () => {
  if (file.value && !done.value) preview.value = await run(true)
})

const file = ref<File | null>(null)
const preview = ref<ImportResult | null>(null)
const done = ref<ImportResult | null>(null)
const error = ref('')
const busy = ref(false)

const rowErrors = computed(() => preview.value?.errors ?? [])
const canImport = computed(() => !!preview.value && !rowErrors.value.length && preview.value.created.length > 0)
// Row problems come with a code; the English `error` is the fallback.
const issueText = (e: ImportResult['errors'][number]) => tOr(`code.${e.code}`, e.error, e.params ?? {})

async function run(dryRun: boolean): Promise<ImportResult | null> {
  if (!file.value) return null
  busy.value = true
  error.value = ''
  try {
    return await api.upload<ImportResult>('/users/import', file.value, XLSX, {
      dry_run: dryRun,
      building_ids: [...buildingIds.value],
    })
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
  api.download('/users/credentials.xlsx', { lang: locale.value }).catch((e) => (error.value = errorText(e)))
}

function sample() {
  api.download('/users/import-sample.xlsx', { lang: locale.value }).catch((e) => (error.value = errorText(e)))
}
</script>

<template>
  <Modal :title="t('import.title')" wide @close="emit('close')">
    <div class="stack">
      <!-- Files use the UI language's column names; the server accepts both languages. -->
      <p class="muted small" style="margin: 0">
        {{ t('import.intro_columns') }} <span class="mono">{{ t('import.columns') }}</span> {{ t('import.intro') }}
        {{ t('import.columns_other_before') }} <span class="mono">{{ t('import.columns_other') }}</span>{{ t('import.columns_other_after') }}
        <a href="#" @click.prevent="sample">{{ t('import.sample') }}</a>
      </p>

      <div v-if="error" class="alert err">{{ error }}</div>

      <template v-if="!done">
        <div v-if="buildings.length" class="field">
          <label>{{ t('import.buildings') }}</label>
          <div class="chips">
            <label
              v-for="b in buildings"
              :key="b.id"
              class="check badge"
              :class="{ info: buildingIds.has(b.id) }"
              style="cursor: pointer"
            >
              <input type="checkbox" :checked="buildingIds.has(b.id)" :disabled="busy" style="display: none" @change="toggleBuilding(b.id)" />{{ b.name }}
            </label>
          </div>
          <span class="muted small">{{ orgWide ? t('import.buildings_hint_org') : t('import.buildings_hint_manager') }}</span>
        </div>
        <div class="field">
          <label>{{ t('import.file') }}</label>
          <input type="file" :accept="`.xlsx,${XLSX}`" :disabled="busy" @change="onFile" />
        </div>
        <p v-if="busy" class="muted">{{ t('import.processing') }}</p>

        <template v-if="preview && !busy">
          <div v-if="rowErrors.length" class="alert err">
            <strong>{{ t('import.cannot_import') }}</strong>
            <ul style="margin: 6px 0 0; padding-left: 20px">
              <li v-for="(e, i) in rowErrors" :key="i">
                <template v-if="e.row > 0">{{ t('import.row', { row: e.row }) }}</template>{{ issueText(e) }}
              </li>
            </ul>
          </div>
          <template v-else>
            <div class="alert ok">
              {{
                t('import.summary', {
                  created: preview.created.length,
                  skipped: preview.skipped.length,
                  groups: preview.groups_created.join(', '),
                })
              }}
            </div>
            <details v-if="preview.created.length">
              <summary>{{ t('import.new_users') }}</summary>
              <div class="chips" style="margin-top: 6px">
                <span v-for="u in preview.created" :key="u" class="badge mono">{{ u }}</span>
              </div>
            </details>
            <details v-if="preview.skipped.length">
              <summary>{{ t('import.skipped_users') }}</summary>
              <div class="chips" style="margin-top: 6px">
                <span v-for="u in preview.skipped" :key="u" class="badge mono">{{ u }}</span>
              </div>
            </details>
          </template>
        </template>

        <div class="modal-foot">
          <button type="button" class="ghost" @click="emit('close')">{{ t('common.cancel') }}</button>
          <button :disabled="!canImport || busy" @click="doImport">
            {{ t('import.submit', { count: canImport ? preview!.created.length : 0 }) }}
          </button>
        </div>
      </template>

      <template v-else>
        <div class="alert ok">
          {{ t('import.done', { created: done.created.length, groups: done.groups_created.join(', ') }) }}
        </div>
        <div class="modal-foot">
          <button type="button" class="ghost" @click="downloadCredentials">{{ t('import.download_credentials') }}</button>
          <button @click="emit('close')">{{ t('common.close') }}</button>
        </div>
      </template>
    </div>
  </Modal>
</template>
