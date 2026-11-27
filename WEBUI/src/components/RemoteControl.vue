<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Remote control of one computer (Monitoring page -> 🖱).
//
// One WebSocket to SMP (server/smp/modules/classroom/api.py, remote_control): the
// server pushes each new picture (a JSON message with its size, then the JPEG as a
// binary message), and we send the mouse and keyboard as small JSON batches every
// 40 ms. Positions are 0..1 across the picture, which is the PC's primary screen;
// keys are physical keys (KeyboardEvent.code), so the PC's own layout applies.
// Closing this view (or losing the connection) ends the session on the PC.
//
// "Block student input": the student's own keyboard and mouse stop working while the
// teacher's still do. The button shows what the server confirmed, not just what was
// clicked. Ctrl+Alt+Del still works for the student; back from it, the block holds.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { ScreenDevice } from '../api/types'
import { t, tOr } from '../i18n'

const props = defineProps<{ device: ScreenDevice }>()
const emit = defineEmits<{ close: [] }>()

const SEND_EVERY_MS = 40
const STALE_AFTER_S = 5

type InputEvent =
  | { t: 'move'; x: number; y: number }
  | { t: 'button'; b: number; down: boolean }
  | { t: 'wheel'; dy: number; dx: number }
  | { t: 'key'; code: string; down: boolean }

const state = ref<'connecting' | 'live' | 'ended'>('connecting')
const studentBlocked = ref(false) // as the server confirmed it
const endText = ref('')
const pictureUrl = ref('')
const lastPictureAt = ref(0)
const now = ref(Date.now())
const img = ref<HTMLImageElement | null>(null)
const surface = ref<HTMLDivElement | null>(null)

let socket: WebSocket | null = null
let queue: InputEvent[] = []
let sendTimer: number | undefined
let clockTimer: number | undefined
const keysDown = new Set<string>()
const buttonsDown = new Set<number>()

const staleSeconds = computed(() =>
  lastPictureAt.value ? Math.floor((now.value - lastPictureAt.value) / 1000) : 0,
)

// ---------------------------------------------------------------- connection
function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${scheme}://${location.host}/api/v1/classroom/devices/${props.device.id}/remote`)
  socket.binaryType = 'blob'
  socket.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'ready') state.value = 'live'
      else if (msg.type === 'block_input') studentBlocked.value = !!msg.on
      return // "frame": the picture follows as a binary message (its size is in the JPEG too)
    }
    const url = URL.createObjectURL(ev.data as Blob)
    if (pictureUrl.value) URL.revokeObjectURL(pictureUrl.value)
    pictureUrl.value = url
    lastPictureAt.value = Date.now()
  }
  socket.onclose = (ev) => {
    state.value = 'ended'
    // Our close codes carry an error code as the reason (e.g. "classroom.remote_busy").
    endText.value = tOr(`classroom.remote_reason.${ev.reason}`, t('classroom.remote_reason.lost'))
  }
}

function queueEvent(e: InputEvent) {
  // Of several moves in a row only the last matters.
  const last = queue[queue.length - 1]
  if (e.t === 'move' && last?.t === 'move') queue[queue.length - 1] = e
  else queue.push(e)
}

function flush() {
  if (queue.length && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ events: queue }))
  }
  queue = []
}

onMounted(() => {
  connect()
  sendTimer = window.setInterval(flush, SEND_EVERY_MS)
  clockTimer = window.setInterval(() => (now.value = Date.now()), 1000)
  surface.value?.focus()
})

onBeforeUnmount(() => {
  letGo()
  flush()
  window.clearInterval(sendTimer)
  window.clearInterval(clockTimer)
  socket?.close()
  if (pictureUrl.value) URL.revokeObjectURL(pictureUrl.value)
})

// ---------------------------------------------------------------- mouse
/** Where the pointer is on the PC's screen (0..1), or null outside the picture. The
 *  picture keeps its shape inside its box (object-fit: contain), so the empty bands
 *  around it are left out. */
function position(e: MouseEvent): { x: number; y: number } | null {
  const el = img.value
  if (!el || !el.naturalWidth) return null
  const box = el.getBoundingClientRect()
  const scale = Math.min(box.width / el.naturalWidth, box.height / el.naturalHeight)
  const w = el.naturalWidth * scale
  const h = el.naturalHeight * scale
  const x = (e.clientX - (box.left + (box.width - w) / 2)) / w
  const y = (e.clientY - (box.top + (box.height - h) / 2)) / h
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null
}

function onMove(e: MouseEvent) {
  const p = position(e)
  if (p) queueEvent({ t: 'move', ...p })
}

function onButton(e: MouseEvent, down: boolean) {
  e.preventDefault()
  surface.value?.focus()
  const p = position(e)
  if (down && !p) return // a click beside the picture isn't for the PC
  if (p) queueEvent({ t: 'move', ...p })
  if (e.button > 2) return
  if (down) buttonsDown.add(e.button)
  else if (!buttonsDown.delete(e.button)) return // pressed outside: nothing to let go
  queueEvent({ t: 'button', b: e.button, down })
}

function onWheel(e: WheelEvent) {
  e.preventDefault()
  // Windows: 120 per notch, + = up / right (the browser's deltaY is + = down).
  const dy = -Math.sign(e.deltaY) * 120
  const dx = Math.sign(e.deltaX) * 120
  if (dy || dx) queueEvent({ t: 'wheel', dy, dx })
}

// ---------------------------------------------------------------- keyboard
function onKey(e: KeyboardEvent, down: boolean) {
  if (!e.code) return
  e.preventDefault() // the key is for the PC, not for this page
  if (down) keysDown.add(e.code)
  else keysDown.delete(e.code)
  queueEvent({ t: 'key', code: e.code, down })
}

/** Let go of everything held: leaving the view mid-press mustn't leave the PC with
 *  Shift or a mouse button stuck down. */
function letGo() {
  for (const code of keysDown) queueEvent({ t: 'key', code, down: false })
  for (const b of buttonsDown) queueEvent({ t: 'button', b, down: false })
  keysDown.clear()
  buttonsDown.clear()
}

function toggleStudentInput() {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ block_input: !studentBlocked.value }))
  surface.value?.focus()
}

/** Keys this computer keeps for itself (Windows key, Alt+Tab): sent with a button. */
function sendKeys(...codes: string[]) {
  for (const code of codes) queueEvent({ t: 'key', code, down: true })
  for (const code of [...codes].reverse()) queueEvent({ t: 'key', code, down: false })
  surface.value?.focus()
}
</script>

<template>
  <Teleport to="body">
    <div class="remote-backdrop" role="dialog" aria-modal="true">
      <div class="remote card">
        <div class="remote-head">
          <h2>{{ t('classroom.control_title', { hostname: device.hostname }) }}</h2>
          <span v-if="state === 'live'" class="badge ok">● {{ t('classroom.control_live') }}</span>
          <span v-else-if="state === 'connecting'" class="badge">{{ t('classroom.control_connecting') }}</span>
          <span class="grow"></span>
          <button class="small" :class="{ ghost: !studentBlocked }" :disabled="state !== 'live'"
                  :title="t('classroom.control_block_hint')" @click="toggleStudentInput">
            {{ studentBlocked ? t('classroom.control_unblock') : t('classroom.control_block') }}
          </button>
          <button class="ghost small" :disabled="state !== 'live'" @click="sendKeys('MetaLeft')">⊞ Win</button>
          <button class="ghost small" :disabled="state !== 'live'" @click="sendKeys('AltLeft', 'Tab')">Alt+Tab</button>
          <button class="small" @click="emit('close')">{{ t('classroom.control_stop') }}</button>
        </div>

        <div v-if="state === 'ended'" class="alert err">{{ endText }}</div>
        <p v-else-if="staleSeconds >= STALE_AFTER_S" class="alert stale">
          {{ t('classroom.control_stale', { seconds: staleSeconds }) }}
        </p>

        <div ref="surface" class="surface" tabindex="0"
             @mousemove="onMove" @mousedown="onButton($event, true)" @mouseup="onButton($event, false)"
             @wheel="onWheel" @contextmenu.prevent
             @keydown="onKey($event, true)" @keyup="onKey($event, false)" @blur="letGo">
          <img v-if="pictureUrl" ref="img" :src="pictureUrl" alt="" draggable="false" />
          <span v-else-if="state !== 'ended'" class="placeholder">{{ t('classroom.control_waiting') }}</span>
        </div>
        <p class="muted small" style="margin: 6px 0 0">{{ t('classroom.control_hint') }}</p>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.remote-backdrop {
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(0, 0, 0, 0.6);
  display: grid;
  place-items: center;
  padding: 12px;
}
.remote {
  width: min(100%, 1800px);
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 12px;
}
.remote-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.remote-head h2 {
  margin: 0;
}
.grow {
  flex: 1;
}
.surface {
  flex: 1;
  min-height: 0;
  background: #000;
  display: grid;
  place-items: center;
  overflow: hidden;
  border-radius: var(--radius);
  cursor: crosshair; /* the PC's own pointer is in the picture */
  outline: none;
}
.surface:focus-visible {
  box-shadow: 0 0 0 2px var(--accent);
}
.surface img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  user-select: none;
}
.placeholder {
  color: var(--muted);
}
.alert.stale {
  border: 1px solid var(--warn);
}
</style>
