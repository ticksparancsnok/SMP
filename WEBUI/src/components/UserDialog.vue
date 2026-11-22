<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { api, errorText } from '../api/client'
import type { Group, Role, Room, User } from '../api/types'
import { groupKindLabel, passwordRules, roleLabel, ROLES } from '../format'
import { t } from '../i18n'
import { can } from '../session'
import Modal from './Modal.vue'
import OtpDisplay from './OtpDisplay.vue'

const props = defineProps<{ user: User | null; rooms: Room[]; groups: Group[] }>()
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
// room_id '' = the role applies to every room (org-wide)
const roles = ref<{ role: Role; room_id: string }[]>(
  props.user
    ? props.user.roles.map((r) => ({ role: r.role as Role, room_id: r.room_id ?? '' }))
    : [{ role: 'student', room_id: '' }],
)
// Class / group memberships. Editable with core.groups.write, otherwise shown read-only.
const canEditGroups = can('core.groups.write')
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
  const roleBody = roles.value.map((r) => ({ role: r.role, room_id: r.role === 'admin' ? null : r.room_id || null }))
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
      // Removing the admin role also deletes the API tokens this user created (server-side).
      const demoted = props.user!.roles.some((r) => r.role === 'admin') && !roleBody.some((r) => r.role === 'admin')
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
            <select v-model="r.role" style="flex: 1">
              <option v-for="role in ROLES" :key="role" :value="role">{{ roleLabel(role) }}</option>
            </select>
            <select v-model="r.room_id" :disabled="r.role === 'admin'" style="flex: 1">
              <option value="">{{ t('common.all_rooms') }}</option>
              <option v-for="room in props.rooms" :key="room.id" :value="room.id">{{ room.name }}</option>
            </select>
            <button type="button" class="ghost small" @click="roles.splice(i, 1)">✕</button>
          </div>
          <div>
            <button type="button" class="ghost small" @click="roles.push({ role: 'teacher', room_id: '' })">
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
        <button :disabled="busy">{{ t('common.save') }}</button>
      </div>
    </form>

    <form
      v-if="!issued && !isNew && props.user!.auth_source === 'local'"
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
