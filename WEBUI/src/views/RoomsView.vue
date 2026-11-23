<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import { RouterLink } from 'vue-router'
import { api, errorText } from '../api/client'
import type { Room } from '../api/types'
import Modal from '../components/Modal.vue'
import Pager from '../components/Pager.vue'
import { t } from '../i18n'
import { usePage } from '../paging'
import { can } from '../session'

const rooms = shallowRef<Room[]>([])
const roomPage = usePage(rooms, { key: 'rooms' })
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
  if (!confirm(t('rooms.confirm_delete', { name: r.name, devices: r.device_count }))) return
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
      <h1>{{ t('nav./rooms') }}</h1>
      <p>{{ t('rooms.subtitle') }}</p>
    </div>
    <button v-if="canWrite" @click="openEdit(null)">{{ t('rooms.new') }}</button>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('common.name') }}</th>
          <th>{{ t('rooms.building') }}</th>
          <th>{{ t('rooms.subnets') }}</th>
          <th>{{ t('rooms.devices') }}</th>
          <th>{{ t('common.description') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in roomPage.rows" :key="r.id">
          <td><strong>{{ r.name }}</strong></td>
          <td>{{ r.building }}</td>
          <td><div class="chips">
            <span v-for="s in r.subnets" :key="s" class="badge mono">{{ s }}</span>
          </div></td>
          <td>
            <RouterLink :to="{ path: '/devices', query: { room_id: r.id } }">{{ t('rooms.device_count', { count: r.device_count }) }}</RouterLink>
          </td>
          <td class="muted">{{ r.description }}</td>
          <td>
            <div v-if="canWrite" class="actions">
              <button class="ghost small" @click="openEdit(r)">{{ t('common.edit') }}</button>
              <button class="danger small" @click="remove(r)">{{ t('common.delete') }}</button>
            </div>
          </td>
        </tr>
        <tr v-if="!rooms.length">
          <td colspan="6" class="empty">{{ t('rooms.empty') }}</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="roomPage.offset" v-model:limit="roomPage.limit" :total="roomPage.total" />
  </div>

  <Modal v-if="open" :title="edit ? t('rooms.edit_title') : t('rooms.new_title')" @close="open = false">
    <form class="stack" @submit.prevent="save">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('common.name') }}</label>
          <input v-model="form.name" required :placeholder="t('rooms.name_placeholder')" />
        </div>
        <div class="field">
          <label>{{ t('rooms.building') }}</label>
          <input v-model="form.building" />
        </div>
      </div>
      <div class="field">
        <label>{{ t('rooms.subnets_label') }}</label>
        <textarea v-model="form.subnets" class="mono" placeholder="10.0.12.0/24" style="min-height: 70px"></textarea>
      </div>
      <div class="field">
        <label>{{ t('common.description') }}</label>
        <input v-model="form.description" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="open = false">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>
</template>
