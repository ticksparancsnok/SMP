<!--
  SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
  SPDX-License-Identifier: AGPL-3.0-or-later
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import { api, errorText } from '../api/client'
import type { Building } from '../api/types'
import Modal from '../components/Modal.vue'
import Pager from '../components/Pager.vue'
import { t } from '../i18n'
import { usePage } from '../paging'
import { can } from '../session'

const buildings = shallowRef<Building[]>([])
const buildingPage = usePage(buildings, { key: 'buildings' })
const error = ref('')
// Creating, renaming and deleting buildings is a school-wide decision.
const canWrite = computed(() => can('core.buildings.write'))

async function load() {
  try {
    buildings.value = await api.get<Building[]>('/buildings')
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)

const edit = ref<Building | null>(null)
const open = ref(false)
const form = reactive({ name: '', description: '' })
const formError = ref('')

function openEdit(b: Building | null) {
  edit.value = b
  Object.assign(form, { name: b?.name ?? '', description: b?.description ?? '' })
  formError.value = ''
  open.value = true
}

async function save() {
  const body = { name: form.name, description: form.description }
  try {
    if (edit.value) await api.patch(`/buildings/${edit.value.id}`, body)
    else await api.post('/buildings', body)
    open.value = false
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function remove(b: Building) {
  if (!confirm(t('buildings.confirm_delete', { name: b.name }))) return
  try {
    await api.del(`/buildings/${b.id}`)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>{{ t('nav./buildings') }}</h1>
      <p>{{ t('buildings.subtitle') }}</p>
    </div>
    <button v-if="canWrite" @click="openEdit(null)">{{ t('buildings.new') }}</button>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('common.name') }}</th>
          <th>{{ t('common.description') }}</th>
          <th>{{ t('buildings.rooms') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in buildingPage.rows" :key="b.id">
          <td><strong>{{ b.name }}</strong></td>
          <td class="muted">{{ b.description }}</td>
          <td>{{ t('buildings.room_count', { count: b.room_count }) }}</td>
          <td>
            <div v-if="canWrite" class="actions">
              <button class="ghost small" @click="openEdit(b)">{{ t('common.edit') }}</button>
              <button class="danger small" @click="remove(b)">{{ t('common.delete') }}</button>
            </div>
          </td>
        </tr>
        <tr v-if="!buildings.length">
          <td colspan="4" class="empty">{{ t('buildings.empty') }}</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="buildingPage.offset" v-model:limit="buildingPage.limit" :total="buildingPage.total" />
  </div>

  <Modal v-if="open" :title="edit ? t('buildings.edit_title') : t('buildings.new_title')" @close="open = false">
    <form class="stack" @submit.prevent="save">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="field">
        <label>{{ t('common.name') }}</label>
        <input v-model="form.name" required maxlength="200" :placeholder="t('buildings.name_placeholder')" />
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
