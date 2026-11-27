<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Building, Group, Role, Room, User } from '../api/types'
import { groupKindLabel, passwordRules, roleLabel, ROLE_TABLE, ROLES } from '../format'
import { t } from '../i18n'
import { can, canAnywhere, grantableRoles } from '../session'
import Modal from './Modal.vue'
import OtpDisplay from './OtpDisplay.vue'

const props = defineProps<{ user: User | null; rooms: Room[]; groups: Group[]; buildings: Building[] }>()
// saved: form submitted (closes the dialog); changed: something already stored while
// the dialog stays open (password reset) so the list can refresh.
const emit = defineEmits<{ close: []; saved: []; changed: [] }>()

const isNew = !props.user
// New users get a generated one-time password (recommended) or one the admin types.
// Either way the user must choose their own at first login.
const passwordMode = ref<'otp' | 'custom'>('otp')
// A freshly generated one-time password, shown once (also exportable until used).
const issued = ref<{ username: string; otp: string; until: string | null } | null>(null)
const form = reactive({
  username: props.user?.username ?? '',
  display_name: props.user?.display_name ?? '',
  email: props.user?.email ?? '',
  password: '',
  disabled: props.user?.disabled ?? false,
})
// Each role row has a scope: '' = the whole school, 'b:<id>' = a building, 'r:<id>' = a
// room. Where a role may sit comes from the role table (format.ts); only school-wide
// callers (admins, unit managers) may pick the whole school.
const orgWide = can('core.users.write')
const mayEdit = isNew || props.user!.can_edit
const grantable = grantableRoles()
const buildingChoices = props.buildings
const roomChoices = props.rooms.filter((r) => can('core.users.write', r.id))
const scopeKinds = (role: Role) => ROLE_TABLE[role].scopes
const orgAllowed = (role: Role) => orgWide && scopeKinds(role).includes('org')
const buildingsFor = (role: Role) => (scopeKinds(role).includes('building') ? buildingChoices : [])
const roomsFor = (role: Role) => (scopeKinds(role).includes('room') ? roomChoices : [])
const scopeValues = (role: Role) => [
  ...(orgAllowed(role) ? [''] : []),
  ...buildingsFor(role).map((b) => `b:${b.id}`),
  ...roomsFor(role).map((r) => `r:${r.id}`),
]
const firstScope = (role: Role) => scopeValues(role)[0] ?? ''
const scopeLocked = (role: Role) => scopeValues(role).length <= 1 && !scopeKinds(role).includes('room')
/** Roles to offer in a row: the ones the caller may grant, plus the row's current one. */
const roleChoices = (current: Role) => ROLES.filter((r) => r === current || grantable.includes(r))
const scopeKnown = (r: { role: Role; scope: string }) => scopeValues(r.role).includes(r.scope)

const roles = ref<{ role: Role; scope: string }[]>(
  props.user
    ? props.user.roles.map((r) => ({
        role: r.role as Role,
        scope: r.room_id ? `r:${r.room_id}` : r.building_id ? `b:${r.building_id}` : '',
      }))
    : [{ role: 'student', scope: firstScope('student') }],
)
/** A new role may not fit the old scope (e.g. a manager only sits at a building). */
function setRole(r: { role: Role; scope: string }, role: Role) {
  r.role = role
  if (!scopeKnown(r)) r.scope = firstScope(role)
}
function addRole() {
  const role: Role = grantable.includes('teacher') ? 'teacher' : (grantable[grantable.length - 1] ?? 'student')
  roles.value.push({ role, scope: firstScope(role) })
}
// Class / group memberships. Editable with core.groups.write, otherwise shown read-only.
const canEditGroups = canAnywhere('core.groups.write')
const initialGroups = (props.user?.groups ?? []).map((g) => g.id)
const groupIds = ref(new Set(initialGroups))
function toggleGroup(id: string) {
  const s = new Set(groupIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  groupIds.value = s
}
// classes first, then staff/custom groups, each alphabetically (API already sorts by name)
const sortedGroups = computed(() =>
  [...props.groups].sort((a, b) => Number(a.kind !== 'class') - Number(b.kind !== 'class')),
)

const newPassword = ref('')
const error = ref('')
const info = ref('')
const busy = ref(false)

async function save() {
  busy.value = true
  error.value = ''
  const roleBody = roles.value.map((r) => ({
    role: r.role,
    room_id: r.scope.startsWith('r:') ? r.scope.slice(2) : null,
    building_id: r.scope.startsWith('b:') ? r.scope.slice(2) : null,
  }))
  try {
    if (isNew) {
      const created = await api.post<{ one_time_password: string | null; otp_expires_at: string | null }>('/users', {
        username: form.username,
        display_name: form.display_name,
        email: form.email || null,
        // omitted -> the server generates a one-time password
        ...(passwordMode.value === 'custom' ? { password: form.password } : {}),
        roles: roleBody,
        group_ids: canEditGroups ? [...groupIds.value] : [],
      })
      if (created.one_time_password) {
        // keep the dialog open to show it; the list refreshes behind it
        issued.value = { username: form.username, otp: created.one_time_password, until: created.otp_expires_at }
        emit('changed')
        return
      }
    } else {
      const id = props.user!.id
      // Losing a school-wide role that may create API tokens (admin, unit manager) also
      // deletes the tokens this user created (server-side).
      const managesTokens = (role: string, scoped: boolean) => !scoped && !!ROLE_TABLE[role as Role]?.managesTokens
      const demoted =
        props.user!.roles.some((r) => managesTokens(r.role, !!(r.room_id || r.building_id))) &&
        !roleBody.some((r) => managesTokens(r.role, !!(r.room_id || r.building_id)))
      if (demoted && !confirm(t('userdlg.confirm_demote', { username: props.user!.username }))) return
      await api.patch(`/users/${id}`, { display_name: form.display_name, email: form.email || null, disabled: form.disabled })
      await api.put(`/users/${id}/roles`, roleBody)
      const changed =
        groupIds.value.size !== initialGroups.length || initialGroups.some((g) => !groupIds.value.has(g))
      if (canEditGroups && changed) await api.put(`/users/${id}/groups`, [...groupIds.value])
    }
    emit('saved')
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function resetPassword(generate: boolean) {
  error.value = info.value = ''
  if (!generate && newPassword.value.length < 8) {
    error.value = t('userdlg.reset_too_short')
    return
  }
  try {
    const r = await api.post<{ one_time_password: string | null; otp_expires_at: string | null }>(
      `/users/${props.user!.id}/password`,
      generate ? {} : { password: newPassword.value },
    )
    if (r.one_time_password) {
      issued.value = { username: props.user!.username, otp: r.one_time_password, until: r.otp_expires_at }
    } else {
      info.value = t('userdlg.reset_done')
    }
    newPassword.value = ''
    emit('changed')
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <Modal :title="isNew ? t('userdlg.new_title') : t('userdlg.edit_title', { username: props.user!.username })" @close="issued ? emit('saved') : emit('close')">
    <div v-if="issued" class="stack">
      <OtpDisplay :username="issued.username" :otp="issued.otp" :until="issued.until" show-export-hint />
      <div class="modal-foot">
        <button type="button" @click="emit('saved')">{{ t('common.done') }}</button>
      </div>
    </div>
    <form v-else class="stack" @submit.prevent="save">
      <div v-if="error" class="alert err">{{ error }}</div>
      <div v-if="info" class="alert ok">{{ info }}</div>
      <div v-if="!mayEdit" class="alert err">{{ t('code.users.not_editable') }}</div>
      <div class="row">
        <div class="field">
          <label>{{ t('common.username') }}</label>
          <input v-model="form.username" :disabled="!isNew" required pattern="[A-Za-z0-9._@\-]+" />
        </div>
        <div class="field">
          <label>{{ t('common.display_name') }}</label>
          <input v-model="form.display_name" />
        </div>
      </div>
      <div class="field">
        <label>{{ t('common.email') }}</label>
        <input v-model="form.email" type="email" />
      </div>
      <div v-if="isNew" class="field">
        <label>{{ t('userdlg.password_label') }}</label>
        <div class="stack" style="gap: 6px">
          <label class="check">
            <input v-model="passwordMode" type="radio" value="otp" /> {{ t('userdlg.password_otp') }}
          </label>
          <label class="check"><input v-model="passwordMode" type="radio" value="custom" /> {{ t('userdlg.password_custom') }}</label>
          <template v-if="passwordMode === 'custom'">
            <input
              v-model="form.password"
              type="password"
              minlength="8"
              required
              autocomplete="new-password"
              :placeholder="t('userdlg.password_placeholder')"
            />
            <span class="muted small">{{ passwordRules() }}</span>
          </template>
        </div>
      </div>
      <label v-else class="check"><input v-model="form.disabled" type="checkbox" /> {{ t('userdlg.disabled') }}</label>

      <div class="field">
        <label>{{ t('userdlg.roles_label') }}</label>
        <div class="stack" style="gap: 6px">
          <div v-for="(r, i) in roles" :key="i" class="row" style="align-items: center">
            <select :value="r.role" style="flex: 1" @change="setRole(r, ($event.target as HTMLSelectElement).value as Role)">
              <option v-for="role in roleChoices(r.role)" :key="role" :value="role">{{ roleLabel(role) }}</option>
            </select>
            <select v-model="r.scope" :disabled="scopeLocked(r.role)" style="flex: 1">
              <option v-if="orgAllowed(r.role)" value="">{{ t('userdlg.scope_org') }}</option>
              <option v-if="!scopeKnown(r)" :value="r.scope" disabled>{{ t('userdlg.scope_other') }}</option>
              <optgroup v-if="buildingsFor(r.role).length" :label="t('userdlg.scope_buildings')">
                <option v-for="b in buildingsFor(r.role)" :key="b.id" :value="`b:${b.id}`">{{ b.name }}</option>
              </optgroup>
              <optgroup v-if="roomsFor(r.role).length" :label="t('userdlg.scope_rooms')">
                <option v-for="room in roomsFor(r.role)" :key="room.id" :value="`r:${room.id}`">{{ room.name }}</option>
              </optgroup>
            </select>
            <button type="button" class="ghost small" @click="roles.splice(i, 1)">✕</button>
          </div>
          <div>
            <button type="button" class="ghost small" @click="addRole">
              {{ t('userdlg.add_role') }}
            </button>
          </div>
        </div>
      </div>

      <div class="field">
        <label>{{ t('userdlg.groups_label') }}</label>
        <div v-if="sortedGroups.length" class="chips">
          <label
            v-for="g in sortedGroups"
            :key="g.id"
            class="check badge"
            :class="{ info: groupIds.has(g.id) }"
            :title="groupKindLabel(g.kind)"
            :style="{ cursor: canEditGroups ? 'pointer' : 'default' }"
          >
            <input
              type="checkbox"
              :checked="groupIds.has(g.id)"
              :disabled="!canEditGroups"
              style="display: none"
              @change="toggleGroup(g.id)"
            />{{ g.kind === 'class' ? '🎓 ' : '' }}{{ g.name }}
          </label>
        </div>
        <span v-else class="muted small">{{ t('userdlg.no_groups') }}</span>
      </div>

      <div class="modal-foot">
        <button type="button" class="ghost" @click="emit('close')">{{ t('common.cancel') }}</button>
        <button :disabled="busy || !mayEdit">{{ t('common.save') }}</button>
      </div>
    </form>

    <form
      v-if="!issued && !isNew && mayEdit && props.user!.auth_source === 'local'"
      class="row"
      style="margin-top: 16px"
      @submit.prevent="resetPassword(false)"
    >
      <div class="field">
        <label>{{ t('userdlg.reset_label') }}</label>
        <input v-model="newPassword" type="password" minlength="8" autocomplete="new-password" :placeholder="t('userdlg.reset_placeholder')" />
      </div>
      <button class="ghost">{{ t('userdlg.reset_set') }}</button>
      <button type="button" class="ghost" @click="resetPassword(true)">{{ t('users.new_otp') }}</button>
    </form>
  </Modal>
</template>
