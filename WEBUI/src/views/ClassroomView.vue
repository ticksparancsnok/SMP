<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Monitoring: the screens of a room's computers.
//
// While this page is open and visible it "watches" the room: it sends a keep-alive
// every few seconds, and only then do the room's PCs send screens (thumbnails; full
// size only for the one enlarged). Hidden tab / leaving the page stops watching at
// once, so PCs stop sending and the server drops the frames (it keeps none).
//
// Screen lock: teachers lock the whole room or single PCs with a message; the PCs show
// it with the school's logo and block keyboard and mouse until unlocked. Unlike
// watching, a lock stays when this page closes (it's stored on the server).
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ApiError, api, errorText } from '../api/client'
import type {
  ClassroomLayout,
  ClassroomRoom,
  ClassroomRoomView,
  FrameInfo,
  LockOut,
  ScreenDevice,
  WatchDevice,
  WatchOut,
} from '../api/types'
import Modal from '../components/Modal.vue'
import { fmtRelative } from '../format'
import { t } from '../i18n'

const rooms = ref<ClassroomRoom[]>([])
const roomId = ref<string>(localStorageGet('smp.classroom.room') ?? '')
const view = ref<ClassroomRoomView | null>(null)
const live = ref<Record<string, WatchDevice>>({})
const status = ref<WatchOut | null>(null)
const error = ref('')
const enlarged = ref<ScreenDevice | null>(null)
const session = crypto.randomUUID()
let timer: number | undefined
let watching = '' // room id we told the server we watch
let generation = 0 // bumped by every tick/stop: only the newest keep-alive loop continues

function localStorageGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

// ---------------------------------------------------------------- loading
async function loadRooms() {
  try {
    rooms.value = await api.get<ClassroomRoom[]>('/classroom/rooms')
    if (!rooms.value.some((r) => r.id === roomId.value)) roomId.value = rooms.value[0]?.id ?? ''
  } catch (e) {
    error.value = errorText(e)
  }
}

async function loadRoom() {
  view.value = null
  live.value = {}
  status.value = null
  if (!roomId.value) return
  try {
    view.value = await api.get<ClassroomRoomView>(`/classroom/rooms/${roomId.value}`)
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}

// ---------------------------------------------------------------- watching (keep-alive)
async function tick() {
  window.clearTimeout(timer)
  const mine = ++generation
  const room = roomId.value
  if (document.hidden || !room) return
  try {
    const out = await api.post<WatchOut>(`/classroom/rooms/${room}/watch`, {
      session,
      detail_device_id: enlarged.value?.id ?? null,
    })
    watching = room
    if (mine !== generation) return // stopped, or a newer tick started meanwhile
    status.value = out
    live.value = Object.fromEntries(out.devices.map((d) => [d.id, d]))
    error.value = ''
  } catch (e) {
    if (mine !== generation) return
    if (e instanceof ApiError && e.status === 404 && enlarged.value) {
      enlarged.value = null // the enlarged PC left the room: close it and carry on
      timer = window.setTimeout(tick, 0)
      return
    }
    error.value = errorText(e)
  }
  // Fast while a screen is enlarged; at most 5 s so the server keeps the session.
  const every = enlarged.value ? (status.value?.detail_interval_s ?? 2) : (status.value?.interval_s ?? 5)
  timer = window.setTimeout(tick, Math.min(Math.max(every, 1), 5) * 1000)
}

function stopWatching(keepalive = false) {
  window.clearTimeout(timer)
  generation++
  if (!watching) return
  const url = `/api/v1/classroom/rooms/${watching}/watch?session=${encodeURIComponent(session)}`
  watching = ''
  // keepalive: the request survives the page closing
  fetch(url, { method: 'DELETE', credentials: 'same-origin', keepalive }).catch(() => {})
}

function onVisibility() {
  if (document.hidden) stopWatching()
  else tick()
}
const onPageHide = () => stopWatching(true)

watch(roomId, async (id, old) => {
  if (old) stopWatching()
  try {
    if (id) localStorage.setItem('smp.classroom.room', id)
  } catch {
    /* not remembered */
  }
  enlarged.value = null
  editing.value = false
  await loadRoom()
  tick()
})

onMounted(async () => {
  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('pagehide', onPageHide)
  const stored = roomId.value
  await loadRooms() // may pick another room: then the roomId watcher loads it
  if (roomId.value === stored) {
    await loadRoom()
    tick()
  }
})
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  window.removeEventListener('pagehide', onPageHide)
  stopWatching()
})

// ---------------------------------------------------------------- screens
const naturalSort = (a: ScreenDevice, b: ScreenDevice) =>
  a.hostname.localeCompare(b.hostname, undefined, { numeric: true, sensitivity: 'base' })

// The keep-alive answer has the room's PCs as they are now (laptops that moved
// rooms, new names, who is logged in); the first load is only the starting point.
const devices = computed<ScreenDevice[]>(() => [...(status.value?.devices ?? view.value?.devices ?? [])].sort(naturalSort))
const layout = computed<ClassroomLayout | null>(() => (editing.value ? draft : view.value?.layout) ?? null)

function cellDevice(r: number, c: number): ScreenDevice | undefined {
  const positions = layout.value?.positions ?? {}
  const id = Object.keys(positions).find((k) => positions[k]![0] === r && positions[k]![1] === c)
  return id ? devices.value.find((d) => d.id === id) : undefined
}
const unplaced = computed(() => devices.value.filter((d) => !(d.id in (layout.value?.positions ?? {}))))

function frameUrl(id: string, kind: 'thumb' | 'detail', f: FrameInfo): string {
  // the timestamp changes the URL only when a new frame arrived: no needless downloads
  return `/api/v1/classroom/devices/${id}/frame?kind=${kind}&t=${f.at}`
}

function aspect(f: FrameInfo | null | undefined): string {
  return f && f.screen_width && f.screen_height ? `${f.screen_width} / ${f.screen_height}` : '16 / 9'
}

function userOf(d: ScreenDevice): string | null {
  return (live.value[d.id] ?? d).user // null: nobody logged in, or the PC is off
}

function isOnline(d: ScreenDevice): boolean {
  return (live.value[d.id] ?? d).online
}

function isStale(f: FrameInfo): boolean {
  // age_s comes from the server: a wrong clock on this computer doesn't matter
  return f.age_s > Math.max(3 * (status.value?.interval_s ?? 5), 15)
}

function enlarge(d: ScreenDevice) {
  if (editing.value) return
  enlarged.value = d
  tick() // ask for the full-size screen right away
}

function closeEnlarged() {
  enlarged.value = null
  tick()
}

// ---------------------------------------------------------------- screen lock
const lockTarget = ref<{ device: ScreenDevice | null } | null>(null) // device null: the whole room
const lockMessage = ref('')
const lockError = ref('')
const lockBusy = ref(false)

function isLocked(d: ScreenDevice): boolean {
  return (live.value[d.id] ?? d).locked
}
const lockedCount = computed(() => devices.value.filter(isLocked).length)

function openLock(device: ScreenDevice | null) {
  lockMessage.value = view.value?.lock_message ?? '' // the school's suggestion (settings)
  lockError.value = ''
  lockTarget.value = { device }
}

/** Show the server's answer at once, without waiting for the next keep-alive. */
function showLocked(out: LockOut) {
  const locked = new Set(out.locked)
  for (const d of [...(view.value?.devices ?? []), ...Object.values(live.value)]) d.locked = locked.has(d.id)
}

async function lock() {
  if (!lockTarget.value) return
  const device = lockTarget.value.device
  lockBusy.value = true
  try {
    showLocked(await api.post<LockOut>(`/classroom/rooms/${roomId.value}/lock`, {
      message: lockMessage.value,
      device_ids: device ? [device.id] : null,
    }))
    lockTarget.value = null
  } catch (e) {
    lockError.value = errorText(e)
  } finally {
    lockBusy.value = false
  }
}

async function unlock(device: ScreenDevice | null) {
  try {
    showLocked(await api.post<LockOut>(`/classroom/rooms/${roomId.value}/unlock`, {
      device_ids: device ? [device.id] : null,
    }))
  } catch (e) {
    error.value = errorText(e)
  }
}

function toggleLock(d: ScreenDevice) {
  if (isLocked(d)) unlock(d)
  else openLock(d)
}

// ---------------------------------------------------------------- seating plan editor
const editing = ref(false)
const draft = reactive<ClassroomLayout>({ mode: 'auto', rows: 4, cols: 6, positions: {} })
const editError = ref('')
const dragging = ref<string | null>(null)

function startEditing() {
  const l = view.value!.layout
  // drop PCs that have left the room since the plan was saved
  const here = new Set(devices.value.map((d) => d.id))
  const positions = Object.fromEntries(Object.entries(l.positions).filter(([id]) => here.has(id)))
  Object.assign(draft, { mode: 'grid', rows: l.rows, cols: l.cols, positions })
  editError.value = ''
  editing.value = true
}

function place(id: string, r: number, c: number) {
  const occupant = cellDevice(r, c)
  if (occupant && occupant.id !== id) delete draft.positions[occupant.id]
  draft.positions[id] = [r, c]
}

function unplace(id: string) {
  delete draft.positions[id]
}

function onDrop(r: number | null, c: number | null) {
  if (!dragging.value) return
  if (r === null || c === null) unplace(dragging.value)
  else place(dragging.value, r, c)
  dragging.value = null
}

function autoFill() {
  // place every unplaced PC into the free cells, in hostname order
  for (const d of unplaced.value) {
    outer: for (let r = 0; r < draft.rows; r++)
      for (let c = 0; c < draft.cols; c++)
        if (!cellDevice(r, c)) {
          draft.positions[d.id] = [r, c]
          break outer
        }
  }
}

watch(
  () => [draft.rows, draft.cols],
  () => {
    for (const [id, [r, c]] of Object.entries(draft.positions))
      if (r >= draft.rows || c >= draft.cols) delete draft.positions[id]
  },
)

async function saveLayout() {
  try {
    const saved = await api.put<ClassroomLayout>(`/classroom/rooms/${roomId.value}/layout`, draft)
    view.value!.layout = saved
    editing.value = false
  } catch (e) {
    editError.value = errorText(e)
  }
}

async function setMode(mode: 'auto' | 'grid') {
  if (!view.value) return
  try {
    view.value.layout = await api.put<ClassroomLayout>(`/classroom/rooms/${roomId.value}/layout`, { ...view.value.layout, mode })
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>{{ t('nav./classroom') }}</h1>
      <p>{{ t('classroom.subtitle') }}</p>
    </div>
    <div v-if="rooms.length" class="field">
      <label>{{ t('common.room') }}</label>
      <select v-model="roomId">
        <option v-for="r in rooms" :key="r.id" :value="r.id">
          {{ t('classroom.room_option', { name: r.name, online: r.online, total: r.devices }) }}
        </option>
      </select>
    </div>
  </div>

  <div v-if="error" class="alert err">{{ error }}</div>
  <p v-if="!rooms.length && !error" class="empty">{{ t('classroom.no_rooms') }}</p>

  <template v-if="view">
    <div class="toolbar">
      <span v-if="status && !status.enabled" class="badge warning">{{ t('classroom.disabled') }}</span>
      <span v-else-if="status" class="badge ok" :title="t('classroom.privacy_note')">
        ● {{ t('classroom.live', { seconds: status.interval_s }) }}
      </span>
      <span v-if="lockedCount" class="badge warning">{{ t('classroom.locked_count', { count: lockedCount }) }}</span>
      <span class="grow"></span>
      <template v-if="!editing && view.can_lock && devices.length">
        <button class="small" @click="openLock(null)">{{ t('classroom.lock_all') }}</button>
        <button v-if="lockedCount" class="ghost small" @click="unlock(null)">{{ t('classroom.unlock_all') }}</button>
      </template>
      <template v-if="!editing">
        <div class="segmented">
          <button class="ghost small" :class="{ active: view.layout.mode === 'auto' }" :disabled="!view.can_edit_layout"
                  @click="setMode('auto')">{{ t('classroom.mode_auto') }}</button>
          <button class="ghost small" :class="{ active: view.layout.mode === 'grid' }" :disabled="!view.can_edit_layout"
                  @click="setMode('grid')">{{ t('classroom.mode_grid') }}</button>
        </div>
        <button v-if="view.can_edit_layout" class="ghost small" @click="startEditing">{{ t('classroom.edit_layout') }}</button>
      </template>
      <template v-else>
        <div class="field compact">
          <label>{{ t('classroom.rows') }}</label>
          <input v-model.number="draft.rows" type="number" min="1" max="20" />
        </div>
        <div class="field compact">
          <label>{{ t('classroom.cols') }}</label>
          <input v-model.number="draft.cols" type="number" min="1" max="20" />
        </div>
        <button class="ghost small" @click="autoFill">{{ t('classroom.auto_fill') }}</button>
        <button class="ghost small" @click="editing = false">{{ t('common.cancel') }}</button>
        <button class="small" @click="saveLayout">{{ t('common.save') }}</button>
      </template>
    </div>
    <div v-if="editError" class="alert err">{{ editError }}</div>
    <p v-if="editing" class="muted small">{{ t('classroom.edit_hint') }}</p>
    <p v-if="!devices.length" class="empty">{{ t('classroom.no_devices') }}</p>

    <!-- seating plan: rows x cols as the PCs stand in the room -->
    <div v-if="layout && layout.mode === 'grid' && devices.length" class="seat-grid"
         :style="{ gridTemplateColumns: `repeat(${layout.cols}, minmax(0, 280px))` }">
      <template v-for="r in layout.rows" :key="r">
        <div v-for="c in layout.cols" :key="`${r}-${c}`" class="seat"
             :class="{ droppable: editing }"
             @dragover.prevent @drop="onDrop(r - 1, c - 1)">
          <template v-if="cellDevice(r - 1, c - 1)">
            <figure class="screen" :class="{ offline: !isOnline(cellDevice(r - 1, c - 1)!) }"
                    :draggable="editing" @dragstart="dragging = cellDevice(r - 1, c - 1)!.id"
                    @click="enlarge(cellDevice(r - 1, c - 1)!)">
              <div class="shot" :style="{ aspectRatio: aspect(live[cellDevice(r - 1, c - 1)!.id]?.thumb) }">
                <img v-if="live[cellDevice(r - 1, c - 1)!.id]?.thumb" draggable="false"
                     :src="frameUrl(cellDevice(r - 1, c - 1)!.id, 'thumb', live[cellDevice(r - 1, c - 1)!.id]!.thumb!)"
                     :class="{ stale: isStale(live[cellDevice(r - 1, c - 1)!.id]!.thumb!) }" alt="" />
                <span v-else class="placeholder">{{ isOnline(cellDevice(r - 1, c - 1)!) ? '…' : t('classroom.offline') }}</span>
                <span v-if="isLocked(cellDevice(r - 1, c - 1)!)" class="lock-badge" :title="t('classroom.locked')">🔒</span>
              </div>
              <figcaption>
                <strong class="mono">{{ cellDevice(r - 1, c - 1)!.hostname }}</strong>
                <span class="muted">👤 {{ userOf(cellDevice(r - 1, c - 1)!) ?? '–' }}</span>
                <button v-if="editing" class="ghost small" @click.stop="unplace(cellDevice(r - 1, c - 1)!.id)">✕</button>
                <button v-else-if="view.can_lock" class="ghost small lock-toggle"
                        :title="isLocked(cellDevice(r - 1, c - 1)!) ? t('classroom.unlock') : t('classroom.lock')"
                        @click.stop="toggleLock(cellDevice(r - 1, c - 1)!)">{{ isLocked(cellDevice(r - 1, c - 1)!) ? '🔓' : '🔒' }}</button>
              </figcaption>
            </figure>
          </template>
          <select v-else-if="editing && unplaced.length" class="seat-select" @change="place(($event.target as HTMLSelectElement).value, r - 1, c - 1)">
            <option value="">+</option>
            <option v-for="d in unplaced" :key="d.id" :value="d.id">{{ d.hostname }}</option>
          </select>
        </div>
      </template>
    </div>

    <!-- auto order (and PCs not placed on the seating plan) -->
    <template v-if="layout && devices.length && (layout.mode === 'auto' || unplaced.length)">
      <h3 v-if="layout.mode === 'grid'" class="muted small">{{ t('classroom.unplaced') }}</h3>
      <div class="screen-grid" @dragover.prevent @drop="onDrop(null, null)">
        <figure v-for="d in layout.mode === 'auto' ? devices : unplaced" :key="d.id" class="screen"
                :class="{ offline: !isOnline(d) }" :draggable="editing" @dragstart="dragging = d.id" @click="enlarge(d)">
          <div class="shot" :style="{ aspectRatio: aspect(live[d.id]?.thumb) }">
            <img v-if="live[d.id]?.thumb" draggable="false" :src="frameUrl(d.id, 'thumb', live[d.id]!.thumb!)"
                 :class="{ stale: isStale(live[d.id]!.thumb!) }" alt="" />
            <span v-else class="placeholder">{{ isOnline(d) ? '…' : t('classroom.offline') }}</span>
            <span v-if="isLocked(d)" class="lock-badge" :title="t('classroom.locked')">🔒</span>
          </div>
          <figcaption>
            <strong class="mono">{{ d.hostname }}</strong>
            <span class="muted">👤 {{ userOf(d) ?? '–' }}</span>
            <button v-if="view.can_lock && !editing" class="ghost small lock-toggle"
                    :title="isLocked(d) ? t('classroom.unlock') : t('classroom.lock')"
                    @click.stop="toggleLock(d)">{{ isLocked(d) ? '🔓' : '🔒' }}</button>
          </figcaption>
        </figure>
      </div>
    </template>
  </template>

  <Modal v-if="enlarged" :title="enlarged.hostname" wide @close="closeEnlarged">
    <div class="shot big" :style="{ aspectRatio: aspect(live[enlarged.id]?.detail ?? live[enlarged.id]?.thumb) }">
      <img v-if="live[enlarged.id]?.detail" :src="frameUrl(enlarged.id, 'detail', live[enlarged.id]!.detail!)" alt="" />
      <img v-else-if="live[enlarged.id]?.thumb" :src="frameUrl(enlarged.id, 'thumb', live[enlarged.id]!.thumb!)" alt="" />
      <span v-else class="placeholder">…</span>
    </div>
    <div class="spread" style="margin: 8px 0 0">
      <p class="muted small" style="margin: 0">
        👤 {{ userOf(enlarged) ?? t('classroom.nobody') }}
        <template v-if="live[enlarged.id]?.detail"> · {{ fmtRelative(new Date(Date.now() - live[enlarged.id]!.detail!.age_s * 1000).toISOString()) }}</template>
        <template v-if="isLocked(enlarged)"> · 🔒 {{ t('classroom.locked') }}</template>
      </p>
      <button v-if="view?.can_lock" class="small" :class="{ ghost: isLocked(enlarged) }" @click="toggleLock(enlarged)">
        {{ isLocked(enlarged) ? t('classroom.unlock') : t('classroom.lock') }}
      </button>
    </div>
  </Modal>

  <Modal v-if="lockTarget" :title="lockTarget.device
           ? t('classroom.lock_title_one', { hostname: lockTarget.device.hostname })
           : t('classroom.lock_title_room', { room: view?.name ?? '' })" @close="lockTarget = null">
    <form class="stack" @submit.prevent="lock">
      <div v-if="lockError" class="alert err">{{ lockError }}</div>
      <div class="field">
        <label>{{ t('classroom.lock_message') }}</label>
        <!-- lock_default: what the PCs show without a text, in the school's block page
             language (not this browser's): the server decides it -->
        <textarea v-model="lockMessage" maxlength="500" rows="3" autofocus
                  :placeholder="view?.lock_default"></textarea>
        <span v-if="!lockMessage.trim()" class="muted small">
          {{ t('classroom.lock_default_hint', { text: view?.lock_default ?? '' }) }}
        </span>
      </div>
      <p class="muted small" style="margin: 0">{{ t('classroom.lock_hint') }}</p>
      <div class="actions">
        <button type="button" class="ghost" @click="lockTarget = null">{{ t('common.cancel') }}</button>
        <button :disabled="lockBusy">🔒 {{ t('classroom.lock') }}</button>
      </div>
    </form>
  </Modal>
</template>

<style scoped>
.screen-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  align-items: start; /* each card as tall as its own screen */
  gap: 12px;
  margin-bottom: 16px;
}
.seat-grid {
  display: grid;
  align-items: start;
  gap: 10px;
  margin-bottom: 16px;
}
.toolbar > .field.compact {
  flex: 0 0 auto;
}
.toolbar > .field.compact input {
  width: 70px;
}
.seat {
  min-height: 90px;
  border-radius: var(--radius);
}
.seat.droppable {
  border: 1px dashed var(--border);
  display: grid;
  place-items: center;
}
.seat.droppable > .screen {
  width: 100%;
}
.seat-select {
  width: 70px;
}
.screen {
  margin: 0;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}
.screen:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}
.screen.offline {
  opacity: 0.5;
}
.shot {
  position: relative; /* for the lock badge */
  background: #000;
  display: grid;
  place-items: center;
  width: 100%;
  overflow: hidden;
}
.lock-badge {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 2px 6px;
  border-radius: var(--radius);
  background: rgba(0, 0, 0, 0.65);
  font-size: 14px;
  line-height: 1.3;
}
.lock-toggle {
  padding: 0 6px;
}
.shot img {
  width: 100%;
  height: 100%;
  object-fit: contain; /* any aspect ratio, never cropped or stretched */
  display: block;
}
.shot img.stale {
  filter: grayscale(1) brightness(0.6);
}
.shot.big {
  max-height: 75vh;
}
.placeholder {
  color: var(--muted);
  font-size: 13px;
}
figcaption {
  display: flex;
  flex-wrap: wrap; /* narrow tiles: the user moves below the host name */
  align-items: center;
  column-gap: 8px;
  padding: 6px 10px;
  font-size: 13px;
}
figcaption strong,
figcaption .muted {
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
figcaption .muted {
  margin-left: auto;
}
.segmented {
  display: inline-flex;
  gap: 0;
}
.segmented button.active {
  background: var(--accent-soft);
}
@media (prefers-reduced-motion: reduce) {
  .screen {
    transition: none;
  }
}
</style>
