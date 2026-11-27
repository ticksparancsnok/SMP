<!--
  Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
  License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
  Attribution required in modified versions: see NOTICE
-->
<script setup lang="ts">
// Renders a form for a module's settings from its JSON schema (pydantic model).
// Supports the field types settings typically use: string, integer, number,
// boolean, list of strings, and Optional[...] of those.
// Labels: `<i18nPrefix>.<field>` and `<i18nPrefix>.<field>.help` when translated,
// otherwise the schema's (English) title and description.
import type { JsonSchema } from '../api/types'
import { t, tOr } from '../i18n'

const props = defineProps<{ schema: JsonSchema; disabled?: boolean; i18nPrefix?: string }>()
const model = defineModel<Record<string, unknown>>({ required: true })

interface Field {
  key: string
  title: string
  description: string
  type: string
  nullable: boolean
}

function fields(): Field[] {
  return Object.entries(props.schema.properties ?? {}).map(([key, s]) => {
    const variants = s.anyOf ?? [s]
    const main = variants.find((v) => v.type !== 'null') ?? s
    const type = main.type === 'array' ? `array:${main.items?.type ?? 'string'}` : (main.type ?? 'string')
    const title = s.title ?? key
    const description = s.description ?? ''
    return {
      key,
      title: props.i18nPrefix ? tOr(`${props.i18nPrefix}.${key}`, title) : title,
      description: props.i18nPrefix ? tOr(`${props.i18nPrefix}.${key}.help`, description) : description,
      type,
      nullable: variants.some((v) => v.type === 'null'),
    }
  })
}

function setNumber(f: Field, raw: string) {
  model.value[f.key] = raw === '' ? (f.nullable ? null : 0) : Number(raw)
}
function setList(f: Field, raw: string) {
  model.value[f.key] = raw.split('\n').map((s) => s.trim()).filter(Boolean)
}
function setText(f: Field, raw: string) {
  model.value[f.key] = raw === '' && f.nullable ? null : raw
}
</script>

<template>
  <div class="stack">
    <div v-for="f in fields()" :key="f.key" class="field">
      <label v-if="f.type === 'boolean'" class="check">
        <input
          type="checkbox"
          :checked="!!model[f.key]"
          :disabled="props.disabled"
          @change="model[f.key] = ($event.target as HTMLInputElement).checked"
        />
        {{ f.title }}
      </label>
      <template v-else>
        <label>{{ f.title }}<span v-if="f.nullable" class="muted"> {{ t('common.optional') }}</span></label>
        <input
          v-if="f.type === 'integer' || f.type === 'number'"
          type="number"
          :step="f.type === 'integer' ? 1 : 'any'"
          :value="model[f.key] ?? ''"
          :disabled="props.disabled"
          @input="setNumber(f, ($event.target as HTMLInputElement).value)"
        />
        <textarea
          v-else-if="f.type.startsWith('array')"
          class="mono"
          style="min-height: 70px"
          :value="((model[f.key] as string[] | null) ?? []).join('\n')"
          :disabled="props.disabled"
          @input="setList(f, ($event.target as HTMLTextAreaElement).value)"
        ></textarea>
        <input
          v-else
          :value="model[f.key] ?? ''"
          :disabled="props.disabled"
          @input="setText(f, ($event.target as HTMLInputElement).value)"
        />
      </template>
      <span v-if="f.description" class="muted small">{{ f.description }}</span>
    </div>
  </div>
</template>
