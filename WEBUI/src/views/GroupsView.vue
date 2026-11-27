<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import { api, errorText } from '../api/client'
import type { Building, Group, Member, Page, User } from '../api/types'
import Modal from '../components/Modal.vue'
import Pager from '../components/Pager.vue'
import { GROUP_KINDS, groupKindLabel, roleLabel, TEACHING_ROLES } from '../format'
import { t } from '../i18n'
import { usePage, useServerPage } from '../paging'
import { can, canAnywhere, canInBuilding } from '../session'

const groups = shallowRef<Group[]>([])
const groupPage = usePage(groups, { key: 'groups' })
const error = ref('')
// A manager writes the groups of their buildings (the permission exists per building).
const canWrite = computed(() => canAnywhere('core.groups.write'))
const orgWide = computed(() => can('core.groups.write'))
const buildings = shallowRef<Building[]>([])
const buildingName = (id: string) => buildings.value.find((b) => b.id === id)?.name
/** The buildings a group may be given: all for school-wide writers, else the manager's own. */
const writableBuildings = computed(() =>
  buildings.value.filter((b) => orgWide.value || canInBuilding('core.groups.write', b.id)),
)

async function load() {
  try {
    groups.value = await api.get<Group[]>('/groups')
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(() => {
  load()
  api.get<Building[]>('/buildings').then((b) => (buildings.value = b)).catch(() => {})
})

// ---------------------------------------------------------------- create / edit
const edit = ref<Group | null>(null)
const editOpen = ref(false)
const form = reactive({ name: '', kind: 'class', description: '', building_ids: [] as string[] })
const formError = ref('')

function openEdit(g: Group | null) {
  edit.value = g
  Object.assign(form, {
    name: g?.name ?? '',
    kind: g?.kind ?? 'class',
    description: g?.description ?? '',
    // none = school-wide (school-wide writers only); a manager starts with their only building
    building_ids: g
      ? [...g.building_ids]
      : !orgWide.value && writableBuildings.value.length === 1
        ? [writableBuildings.value[0]!.id]
        : [],
  })
  formError.value = ''
  editOpen.value = true
}

function toggleBuilding(id: string) {
  form.building_ids = form.building_ids.includes(id) ? form.building_ids.filter((b) => b !== id) : [...form.building_ids, id]
}

async function saveGroup() {
  if (!orgWide.value && !form.building_ids.length) {
    formError.value = t('groups.buildings_required')
    return
  }
  try {
    if (edit.value) await api.patch(`/groups/${edit.value.id}`, form)
    else await api.post('/groups', form)
    editOpen.value = false
    load()
  } catch (e) {
    formError.value = errorText(e)
  }
}

async function remove(g: Group) {
  if (!confirm(t('groups.confirm_delete', { name: g.name }))) return
  try {
    await api.del(`/groups/${g.id}`)
    load()
  } catch (e) {
    error.value = errorText(e)
  }
}

// ---------------------------------------------------------------- class teachers
// Teachers responsible for the group (not members). They can issue new one-time
// passwords to its students.
const teachersOf = ref<Group | null>(null)
const selectedTeachers = ref(new Set<string>())
const teacherFilter = ref('')
// The users list filters by one role at a time: pick which kind of teacher to list.
const teacherRole = ref<string>(TEACHING_ROLES[0])
const teacherError = ref('')
// Paged and searched on the server: a school can have any number of teachers.
const teacherPage = useServerPage(
  (limit, offset) => api.get<Page<User>>('/users', { role: teacherRole.value, q: teacherFilter.value.trim(), limit, offset }),
  { key: 'groups.teachers', resetOn: [teacherFilter, teacherRole], onError: errorText },
)

function openTeachers(g: Group) {
  teachersOf.value = g
  teacherError.value = ''
  teacherFilter.value = ''
  teacherRole.value = TEACHING_ROLES[0]
  selectedTeachers.value = new Set((g.teachers ?? []).map((t) => t.id))
  if (teacherPage.offset) teacherPage.offset = 0 // the page watcher loads
  else teacherPage.reload()
}

function toggleTeacher(id: string) {
  const s = new Set(selectedTeachers.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  selectedTeachers.value = s
}

async function saveTeachers() {
  try {
    await api.put(`/groups/${teachersOf.value!.id}/teachers`, [...selectedTeachers.value])
    teachersOf.value = null
    load()
  } catch (e) {
    teacherError.value = errorText(e)
  }
}

// ---------------------------------------------------------------- members
const membersOf = ref<Group | null>(null)
const selected = ref(new Set<string>())
const memberFilter = ref('')
const memberError = ref('')
// Every user of the school can be picked, so this is paged and searched on the server.
const memberPage = useServerPage(
  (limit, offset) => api.get<Page<User>>('/users', { q: memberFilter.value.trim(), limit, offset }),
  { key: 'groups.members', resetOn: [memberFilter], onError: errorText },
)

async function openMembers(g: Group) {
  membersOf.value = g
  memberFilter.value = ''
  memberError.value = ''
  if (memberPage.offset) memberPage.offset = 0 // the page watcher loads
  else memberPage.reload()
  try {
    // the current members (a class: dozens) mark the checkboxes on every page
    const members = await api.get<Member[]>(`/groups/${g.id}/members`)
    selected.value = new Set(members.map((m) => m.id))
  } catch (e) {
    memberError.value = errorText(e)
  }
}

function toggle(id: string) {
  const s = new Set(selected.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  selected.value = s
}

async function saveMembers() {
  try {
    await api.put(`/groups/${membersOf.value!.id}/members`, [...selected.value])
    membersOf.value = null
    load()
  } catch (e) {
    memberError.value = errorText(e)
  }
}
</script>

<template>
  <div class="page-head spread">
    <div>
      <h1>{{ t('nav./groups') }}</h1>
      <p>{{ t('groups.subtitle') }}</p>
    </div>
    <button v-if="canWrite" @click="openEdit(null)">{{ t('groups.new') }}</button>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>{{ t('common.name') }}</th>
          <th>{{ t('common.type') }}</th>
          <th>{{ t('common.description') }}</th>
          <th>{{ t('groups.buildings') }}</th>
          <th>{{ t('groups.members') }}</th>
          <th>{{ t('groups.teachers') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="g in groupPage.rows" :key="g.id">
          <td><strong>{{ g.name }}</strong></td>
          <td><span class="badge">{{ groupKindLabel(g.kind) }}</span></td>
          <td class="muted">{{ g.description }}</td>
          <td>
            <span v-if="!g.building_ids.length" class="muted">{{ t('groups.school_wide') }}</span>
            <div v-else class="chips">
              <span v-for="id in g.building_ids" :key="id" class="badge">{{ buildingName(id) ?? '?' }}</span>
            </div>
          </td>
          <td>{{ g.member_count }}</td>
          <td class="small">{{ (g.teachers ?? []).map((t) => t.display_name || t.username).join(', ') || '–' }}</td>
          <td>
            <div class="actions">
              <button class="ghost small" @click="openMembers(g)">{{ t('groups.members') }}</button>
              <button v-if="canWrite" class="ghost small" @click="openTeachers(g)">{{ t('groups.teachers') }}</button>
              <template v-if="canWrite">
                <button class="ghost small" @click="openEdit(g)">{{ t('common.edit') }}</button>
                <button class="danger small" @click="remove(g)">{{ t('common.delete') }}</button>
              </template>
            </div>
          </td>
        </tr>
        <tr v-if="!groups.length">
          <td colspan="7" class="empty">{{ t('groups.empty') }}</td>
        </tr>
      </tbody>
    </table>
    <Pager v-model:offset="groupPage.offset" v-model:limit="groupPage.limit" :total="groupPage.total" />
  </div>

  <Modal v-if="editOpen" :title="edit ? t('groups.edit_title') : t('groups.new_title')" @close="editOpen = false">
    <form class="stack" @submit.prevent="saveGroup">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('common.name') }}</label>
          <input v-model="form.name" required :placeholder="t('groups.name_placeholder')" />
        </div>
        <div class="field">
          <label>{{ t('common.type') }}</label>
          <select v-model="form.kind">
            <option v-for="k in GROUP_KINDS" :key="k" :value="k">{{ groupKindLabel(k) }}</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label>{{ t('common.description') }}</label>
        <input v-model="form.description" />
      </div>
      <div v-if="buildings.length" class="field">
        <label>{{ t('groups.buildings') }}</label>
        <div class="chips">
          <label
            v-for="b in writableBuildings"
            :key="b.id"
            class="check badge"
            :class="{ info: form.building_ids.includes(b.id) }"
            style="cursor: pointer"
          >
            <input type="checkbox" :checked="form.building_ids.includes(b.id)" style="display: none" @change="toggleBuilding(b.id)" />{{ b.name }}
          </label>
        </div>
        <span class="muted small">{{ orgWide ? t('groups.buildings_hint_org') : t('groups.buildings_hint_manager') }}</span>
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="editOpen = false">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="teachersOf" :title="t('groups.teachers_title', { name: teachersOf.name })" @close="teachersOf = null">
    <div class="stack">
      <p class="muted small" style="margin: 0">{{ t('groups.teachers_intro') }}</p>
      <div v-if="teacherError || teacherPage.error" class="alert err">{{ teacherError || teacherPage.error }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('users.role') }}</label>
          <select v-model="teacherRole">
            <option v-for="r in TEACHING_ROLES" :key="r" :value="r">{{ roleLabel(r) }}</option>
          </select>
        </div>
        <div class="field">
          <label>{{ t('groups.filter') }}</label>
          <input v-model="teacherFilter" type="search" :placeholder="t('groups.filter_placeholder')" />
        </div>
      </div>
      <div class="check-list">
        <label v-for="t in teacherPage.rows" :key="t.id" class="check" style="padding: 3px 0">
          <input type="checkbox" :checked="selectedTeachers.has(t.id)" @change="toggleTeacher(t.id)" />
          {{ t.display_name || t.username }} <span class="muted small mono">{{ t.username }}</span>
        </label>
        <p v-if="!teacherPage.rows.length && !teacherPage.loading" class="empty">
          {{ teacherFilter ? t('common.no_matches') : t('groups.no_teachers') }}
        </p>
      </div>
      <Pager v-model:offset="teacherPage.offset" v-model:limit="teacherPage.limit" :total="teacherPage.total" />
      <div class="modal-foot">
        <button class="ghost" @click="teachersOf = null">{{ t('common.cancel') }}</button>
        <button @click="saveTeachers">{{ t('common.save') }}</button>
      </div>
    </div>
  </Modal>

  <Modal v-if="membersOf" :title="t('groups.members_title', { name: membersOf.name })" @close="membersOf = null">
    <div class="stack">
      <div v-if="memberError || memberPage.error" class="alert err">{{ memberError || memberPage.error }}</div>
      <div class="field">
        <label>{{ t('groups.filter') }}</label>
        <input v-model="memberFilter" type="search" :placeholder="t('groups.filter_placeholder')" />
      </div>
      <div class="check-list">
        <label v-for="u in memberPage.rows" :key="u.id" class="check" style="padding: 3px 0">
          <input type="checkbox" :checked="selected.has(u.id)" :disabled="!canWrite" @change="toggle(u.id)" />
          {{ u.display_name || u.username }} <span class="muted small mono">{{ u.username }}</span>
        </label>
        <p v-if="!memberPage.rows.length && !memberPage.loading" class="empty">{{ t('common.no_matches') }}</p>
      </div>
      <Pager v-model:offset="memberPage.offset" v-model:limit="memberPage.limit" :total="memberPage.total" />
      <div class="modal-foot">
        <span class="muted small" style="margin-right: auto">{{ t('groups.selected', { count: selected.size }) }}</span>
        <button class="ghost" @click="membersOf = null">{{ t('common.close') }}</button>
        <button v-if="canWrite" @click="saveMembers">{{ t('common.save') }}</button>
      </div>
    </div>
  </Modal>
</template>
