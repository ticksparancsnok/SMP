// Friendly names for the types generated from the server's OpenAPI schema.
// Regenerate after backend changes:
//   (cd ../server && smp openapi -o ../WEBUI/openapi.json) && npm run gen:api
import type { components } from './schema'

type S = components['schemas']

export type Me = S['MeOut']
export type User = S['UserOut']
export type UserCreate = S['UserCreate']
export type RoleIn = S['RoleIn']
export type Role = S['Role']
export type Group = S['GroupOut']
export type Member = S['MemberOut']
export type Room = S['RoomOut']
export type Device = S['DeviceOut']
export type DeviceStatus = S['DeviceStatus']
export type Policy = S['PolicyOut']
export type Schedule = S['ScheduleOut']
export type Assignment = S['AssignmentOut']
export type TargetType = S['TargetType']
export type AuditEntry = S['AuditOut']
export type Token = S['TokenOut']
export type TokenCreated = S['TokenCreated']
export type Widget = S['Widget']
export type ShortLink = S['LinkOut']
export type ImportResult = S['ImportResult']
export type DnsEntry = S['EntryOut']
export type DnsEntryIn = S['EntryIn']
export type DnsHostsPreview = S['HostsPreviewOut']
export type DnsStatus = S['SyncStatusOut']
export type DnsAllowEntry = S['AllowEntryOut']
export type DnsAllowIn = S['AllowIn']
export type DnsAllowlist = S['AllowlistOut']

export interface Page<T> {
  items: T[]
  total: number
}

// /modules returns plain dicts (see Module.manifest() in server/smp/core/module.py).
export interface UiRoute {
  path: string
  title: string
  icon: string
  permission: string | null
  section: string
}

export interface JsonSchema {
  type?: string
  title?: string
  description?: string
  default?: unknown
  minimum?: number
  maximum?: number
  items?: JsonSchema
  anyOf?: JsonSchema[]
  properties?: Record<string, JsonSchema>
}

export interface Manifest {
  name: string
  title: string
  version: string
  description: string
  permissions: { code: string; description: string; default_roles: string[]; room_scoped: boolean }[]
  ui_routes: UiRoute[]
  events: { subject: string; description: string }[]
  settings_schema: JsonSchema | null
  policy_schema: JsonSchema | null
}
