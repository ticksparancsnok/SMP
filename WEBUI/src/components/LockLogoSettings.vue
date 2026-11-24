<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// The school's logo on locked screens (Settings -> Monitoring).
//
// Any image the browser can open is accepted (PNG, JPEG, SVG, WebP, ...): it is drawn
// onto a canvas, shrunk to at most 1024 px and uploaded as PNG, because SMP stores
// PNG only and the PC agent decodes PNG only. Transparency is kept. The preview shows
// it on the lock screen's own background colour.
import { onMounted, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { LockLogoInfo } from '../api/types'
import { t } from '../i18n'

const props = defineProps<{ disabled?: boolean }>()

const MAX_SIDE = 1024
const info = ref<LockLogoInfo | null>(null)
const loaded = ref(false)
const busy = ref(false)
const status = ref<{ ok: boolean; text: string } | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

onMounted(async () => {
  try {
    info.value = await api.get<LockLogoInfo | null>('/classroom/lock-logo/info')
  } catch (e) {
    status.value = { ok: false, text: errorText(e) }
  } finally {
    loaded.value = true
  }
})

/** Any image file -> a PNG of at most MAX_SIDE pixels per side. */
async function toPng(file: File): Promise<Blob> {
  const url = URL.createObjectURL(file)
  try {
    const img = new Image()
    img.src = url
    await img.decode() // rejects when the browser can't read it
    // An SVG may have no size of its own: draw it at 512 px.
    const width = img.naturalWidth || 512
    const height = img.naturalHeight || 512
    const scale = Math.min(1, MAX_SIDE / Math.max(width, height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(width * scale))
    canvas.height = Math.max(1, Math.round(height * scale))
    canvas.getContext('2d')!.drawImage(img, 0, 0, canvas.width, canvas.height)
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('png'))), 'image/png'),
    )
  } finally {
    URL.revokeObjectURL(url)
  }
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // choosing the same file again still triggers a change
  if (!file) return
  status.value = null
  let png: Blob
  try {
    png = await toPng(file)
  } catch {
    status.value = { ok: false, text: t('settings.lock_logo.bad_image') }
    return
  }
  busy.value = true
  try {
    info.value = await api.upload<LockLogoInfo>('/classroom/lock-logo', png, 'image/png')
    status.value = { ok: true, text: t('settings.lock_logo.saved') }
  } catch (e) {
    status.value = { ok: false, text: errorText(e) }
  } finally {
    busy.value = false
  }
}

async function remove() {
  status.value = null
  busy.value = true
  try {
    await api.del('/classroom/lock-logo')
    info.value = null
    status.value = { ok: true, text: t('common.saved') }
  } catch (e) {
    status.value = { ok: false, text: errorText(e) }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="stack">
    <h3>{{ t('settings.lock_logo.title') }}</h3>
    <p class="muted small" style="margin: 0">{{ t('settings.lock_logo.intro') }}</p>
    <div v-if="status" class="alert" :class="status.ok ? 'ok' : 'err'">{{ status.text }}</div>
    <div v-if="loaded" class="logo-row">
      <div class="preview">
        <!-- the sha256 in the URL: a new logo is a new URL, no stale cached copy -->
        <img v-if="info" :src="`/api/v1/classroom/lock-logo?v=${info.sha256.slice(0, 16)}`" alt="" />
        <img v-else src="../../Assets/SMP-logo.png" alt="SMP" />
      </div>
      <div class="stack">
        <span class="muted small">
          {{ info
            ? t('settings.lock_logo.size', { width: info.width, height: info.height, kb: Math.ceil(info.size / 1024) })
            : t('settings.lock_logo.none') }}
        </span>
        <div v-if="!props.disabled" class="actions">
          <button type="button" class="small" :disabled="busy" @click="fileInput?.click()">
            {{ info ? t('settings.lock_logo.replace') : t('settings.lock_logo.upload') }}
          </button>
          <button v-if="info" type="button" class="ghost small" :disabled="busy" @click="remove">
            {{ t('settings.lock_logo.remove') }}
          </button>
          <input ref="fileInput" type="file" accept="image/*" hidden @change="onFile" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.logo-row {
  display: flex;
  gap: 16px;
  align-items: center;
  flex-wrap: wrap;
}
.preview {
  /* the lock screen's background (agent: capabilities/lock/picture.rs, BACKGROUND) */
  background: #f1f5f9;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  width: 200px;
  height: 120px;
  display: grid;
  place-items: center;
  padding: 10px;
}
.preview img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
</style>
