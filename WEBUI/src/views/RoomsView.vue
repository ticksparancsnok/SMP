<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Room } from '../api/types'
import Modal from '../components/Modal.vue'
import { can } from '../session'

const rooms = ref<Room[]>([])
const error = ref('')
const canWrite = computed(() => can('core.rooms.write'))

async function load() {
  try {
    rooms.value = await api.get<Room[]>('/rooms')
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)

const edit = ref<Room | null>(null)
const open = ref(false)
const form = reactive({ name: '', building: '', description: '', subnets: '' })
const formError = ref('')

function openEdit(r: Room | null) {
  edit.value = r
  Object.assign(form, {
    name: r?.name ?? '',
    building: r?.building ?? '',
    description: r?.description ?? '',
    subnets: (r?.subnets ?? []).join('\n'),
  })
  formError.value = ''
  open.value = true
}

async function save() {
  const body = {
    name: form.name,
    building: form.building,
    description: form.description,
    subnets: form.subnets.split(/[\s,]+/).filter(Boolean),
  }
  try {
    if (edit.value) await api.patch(`/rooms/${edit.value.id}`, body)
    else await api.post('/rooms', body)
    open.value = false
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function remove(r: Room) {
  const extra = r.device_count ? ` A ${r.device_count} gép terem nélkül marad.` : ''
  if (!confirm(`Biztosan törlöd a termet: ${r.name}?${extra}`)) return
  try {
    await api.del(`/rooms/${r.id}`)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>Termek</h1>
      <p>Tantermek és gépes laborok. Az alhálózat alapján a DNS szűrő gép-ügynök nélkül is felismeri a termet.</p>
    </div>
    <button v-if="canWrite" @click="openEdit(null)">+ Új terem</button>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>Név</th>
          <th>Épület</th>
          <th>Alhálózatok</th>
          <th>Gépek</th>
          <th>Leírás</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rooms" :key="r.id">
          <td><strong>{{ r.name }}</strong></td>
          <td>{{ r.building }}</td>
          <td><div class="chips">
            <span v-for="s in r.subnets" :key="s" class="badge mono">{{ s }}</span>
          </div></td>
          <td>
            <RouterLink :to="{ path: '/devices', query: { room_id: r.id } }">{{ r.device_count }} gép</RouterLink>
          </td>
          <td class="muted">{{ r.description }}</td>
          <td>
            <div v-if="canWrite" class="actions">
              <button class="ghost small" @click="openEdit(r)">Szerkesztés</button>
              <button class="danger small" @click="remove(r)">Törlés</button>
            </div>
          </td>
        </tr>
        <tr v-if="!rooms.length">
          <td colspan="6" class="empty">Még nincs terem.</td>
        </tr>
      </tbody>
    </table>
  </div>

  <Modal v-if="open" :title="edit ? 'Terem szerkesztése' : 'Új terem'" @close="open = false">
    <form class="stack" @submit.prevent="save">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="row">
        <div class="field">
          <label>Név</label>
          <input v-model="form.name" required placeholder="pl. Informatika 1" />
        </div>
        <div class="field">
          <label>Épület</label>
          <input v-model="form.building" />
        </div>
      </div>
      <div class="field">
        <label>Alhálózatok (CIDR, soronként egy)</label>
        <textarea v-model="form.subnets" class="mono" placeholder="10.0.12.0/24" style="min-height: 70px"></textarea>
      </div>
      <div class="field">
        <label>Leírás</label>
        <input v-model="form.description" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="open = false">Mégse</button>
        <button>Mentés</button>
      </div>
    </form>
  </Modal>
</template>
