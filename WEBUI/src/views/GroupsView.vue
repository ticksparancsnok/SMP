<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Group, Member, Page, User } from '../api/types'
import Modal from '../components/Modal.vue'
import { GROUP_KINDS } from '../format'
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
  if (!confirm(`Biztosan törlöd a csoportot: ${g.name}? (A felhasználók megmaradnak.)`)) return
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
      <h1>Csoportok</h1>
      <p>Osztályok, tanári kar és egyéb csoportok</p>
    </div>
    <button v-if="canWrite" @click="openEdit(null)">+ Új csoport</button>
  </div>
  <div v-if="error" class="alert err">{{ error }}</div>

  <div class="card table-wrap">
    <table>
      <thead>
        <tr>
          <th>Név</th>
          <th>Típus</th>
          <th>Leírás</th>
          <th>Tagok</th>
          <th>Tanárok</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="g in groups" :key="g.id">
          <td><strong>{{ g.name }}</strong></td>
          <td><span class="badge">{{ GROUP_KINDS[g.kind] ?? g.kind }}</span></td>
          <td class="muted">{{ g.description }}</td>
          <td>{{ g.member_count }}</td>
          <td class="small">{{ (g.teachers ?? []).map((t) => t.display_name || t.username).join(', ') || '–' }}</td>
          <td>
            <div class="actions">
              <button class="ghost small" @click="openMembers(g)">Tagok</button>
              <button v-if="canWrite" class="ghost small" @click="openTeachers(g)">Tanárok</button>
              <template v-if="canWrite">
                <button class="ghost small" @click="openEdit(g)">Szerkesztés</button>
                <button class="danger small" @click="remove(g)">Törlés</button>
              </template>
            </div>
          </td>
        </tr>
        <tr v-if="!groups.length">
          <td colspan="6" class="empty">Még nincs csoport.</td>
        </tr>
      </tbody>
    </table>
  </div>

  <Modal v-if="editOpen" :title="edit ? 'Csoport szerkesztése' : 'Új csoport'" @close="editOpen = false">
    <form class="stack" @submit.prevent="saveGroup">
      <div v-if="formError" class="alert err">{{ formError }}</div>
      <div class="row">
        <div class="field">
          <label>Név</label>
          <input v-model="form.name" required placeholder="pl. 10.B" />
        </div>
        <div class="field">
          <label>Típus</label>
          <select v-model="form.kind">
            <option v-for="(label, key) in GROUP_KINDS" :key="key" :value="key">{{ label }}</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label>Leírás</label>
        <input v-model="form.description" />
      </div>
      <div class="modal-foot">
        <button type="button" class="ghost" @click="editOpen = false">Mégse</button>
        <button>Mentés</button>
      </div>
    </form>
  </Modal>

  <Modal v-if="teachersOf" :title="`Tanárok: ${teachersOf.name}`" @close="teachersOf = null">
    <div class="stack">
      <p class="muted small" style="margin: 0">
        A csoport tanárai (pl. osztályfőnök) a csoport diákjainak új egyszeri jelszót adhatnak, ha elfelejtették a
        jelszavukat. A tanárok nem lesznek a csoport tagjai.
      </p>
      <div v-if="teacherError" class="alert err">{{ teacherError }}</div>
      <div class="check-list">
        <label v-for="t in allTeachers" :key="t.id" class="check" style="padding: 3px 0">
          <input type="checkbox" :checked="selectedTeachers.has(t.id)" @change="toggleTeacher(t.id)" />
          {{ t.display_name || t.username }} <span class="muted small mono">{{ t.username }}</span>
        </label>
        <p v-if="!allTeachers.length" class="empty">Nincs tanár szerepkörű felhasználó.</p>
      </div>
      <div class="modal-foot">
        <button class="ghost" @click="teachersOf = null">Mégse</button>
        <button @click="saveTeachers">Mentés</button>
      </div>
    </div>
  </Modal>

  <Modal v-if="membersOf" :title="`Tagok: ${membersOf.name}`" @close="membersOf = null">
    <div class="stack">
      <div v-if="memberError" class="alert err">{{ memberError }}</div>
      <div class="field">
        <label>Szűrés</label>
        <input v-model="memberFilter" placeholder="név vagy felhasználónév" />
      </div>
      <div class="check-list">
        <label v-for="u in filteredUsers" :key="u.id" class="check" style="padding: 3px 0">
          <input type="checkbox" :checked="selected.has(u.id)" :disabled="!canWrite" @change="toggle(u.id)" />
          {{ u.display_name || u.username }} <span class="muted small mono">{{ u.username }}</span>
        </label>
        <p v-if="!filteredUsers.length" class="empty">Nincs találat.</p>
      </div>
      <div class="modal-foot">
        <span class="muted small" style="margin-right: auto">{{ selected.size }} tag kiválasztva</span>
        <button class="ghost" @click="membersOf = null">Bezárás</button>
        <button v-if="canWrite" @click="saveMembers">Mentés</button>
      </div>
    </div>
  </Modal>
</template>
