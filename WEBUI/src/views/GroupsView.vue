<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Group, Member, Page, User } from '../api/types'
import Modal from '../components/Modal.vue'
import { GROUP_KINDS, groupKindLabel } from '../format'
import { t } from '../i18n'
import { can } from '../session'

const groups = ref<Group[]>([])
const error = ref('')
const canWrite = computed(() => can('core.groups.write'))

async function load() {
  try {
    groups.value = await api.get<Group[]>('/groups')
    error.value = ''
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)

// ---------------------------------------------------------------- create / edit
const edit = ref<Group | null>(null)
const editOpen = ref(false)
const form = reactive({ name: '', kind: 'class', description: '' })
const formError = ref('')

function openEdit(g: Group | null) {
  edit.value = g
  Object.assign(form, { name: g?.name ?? '', kind: g?.kind ?? 'class', description: g?.description ?? '' })
  formError.value = ''
  editOpen.value = true
}

async function saveGroup() {
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
const allTeachers = ref<User[]>([])
const selectedTeachers = ref(new Set<string>())
const teacherError = ref('')

async function openTeachers(g: Group) {
  teachersOf.value = g
  teacherError.value = ''
  selectedTeachers.value = new Set((g.teachers ?? []).map((t) => t.id))
  try {
    allTeachers.value = (await api.get<Page<User>>('/users', { role: 'teacher', limit: 1000 })).items
  } catch (e) {
    teacherError.value = errorText(e)
  }
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
const allUsers = ref<User[]>([])
const selected = ref(new Set<string>())
const memberFilter = ref('')
const memberError = ref('')

const filteredUsers = computed(() => {
  const f = memberFilter.value.toLowerCase()
  return allUsers.value.filter((u) => !f || u.username.toLowerCase().includes(f) || u.display_name.toLowerCase().includes(f))
})

async function openMembers(g: Group) {
  membersOf.value = g
  memberFilter.value = ''
  memberError.value = ''
  try {
    const [members, users] = await Promise.all([
      api.get<Member[]>(`/groups/${g.id}/members`),
      api.get<Page<User>>('/users', { limit: 1000 }),
    ])
    selected.value = new Set(members.map((m) => m.id))
    allUsers.value = users.items
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
          <th>{{ t('groups.members') }}</th>
          <th>{{ t('groups.teachers') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="g in groups" :key="g.id">
          <td><strong>{{ g.name }}</strong></td>
          <td><span class="badge">{{ groupKindLabel(g.kind) }}</span></td>
          <td class="muted">{{ g.description }}</td>
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
          <td colspan="6" class="empty">{{ t('groups.empty') }}</td>
        </tr>
      </tbody>
    </table>
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
      <div class="modal-foot">
        <button type="button" class="ghost" @click="editOpen = false">{{ t('common.cancel') }}</button>
        <button>{{ t('common.save') }}</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="teachersOf" :title="t('groups.teachers_title', { name: teachersOf.name })" @close="teachersOf = null">
    <div class="stack">
      <p class="muted small" style="margin: 0">{{ t('groups.teachers_intro') }}</p>
      <div v-if="teacherError" class="alert err">{{ teacherError }}</div>
      <div class="check-list">
        <label v-for="t in allTeachers" :key="t.id" class="check" style="padding: 3px 0">
          <input type="checkbox" :checked="selectedTeachers.has(t.id)" @change="toggleTeacher(t.id)" />
          {{ t.display_name || t.username }} <span class="muted small mono">{{ t.username }}</span>
        </label>
        <p v-if="!allTeachers.length" class="empty">{{ t('groups.no_teachers') }}</p>
      </div>
      <div class="modal-foot">
        <button class="ghost" @click="teachersOf = null">{{ t('common.cancel') }}</button>
        <button @click="saveTeachers">{{ t('common.save') }}</button>
      </div>
    </div>
  </Modal>

  <Modal v-if="membersOf" :title="t('groups.members_title', { name: membersOf.name })" @close="membersOf = null">
    <div class="stack">
      <div v-if="memberError" class="alert err">{{ memberError }}</div>
      <div class="field">
        <label>{{ t('groups.filter') }}</label>
        <input v-model="memberFilter" :placeholder="t('groups.filter_placeholder')" />
      </div>
      <div class="check-list">
        <label v-for="u in filteredUsers" :key="u.id" class="check" style="padding: 3px 0">
          <input type="checkbox" :checked="selected.has(u.id)" :disabled="!canWrite" @change="toggle(u.id)" />
          {{ u.display_name || u.username }} <span class="muted small mono">{{ u.username }}</span>
        </label>
        <p v-if="!filteredUsers.length" class="empty">{{ t('common.no_matches') }}</p>
      </div>
      <div class="modal-foot">
        <span class="muted small" style="margin-right: auto">{{ t('groups.selected', { count: selected.size }) }}</span>
        <button class="ghost" @click="membersOf = null">{{ t('common.close') }}</button>
        <button v-if="canWrite" @click="saveMembers">{{ t('common.save') }}</button>
      </div>
    </div>
  </Modal>
</template>
