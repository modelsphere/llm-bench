<template>
  <div class="space-y-4">
    <div v-for="(rawSchema, key) in schemaProperties" :key="key" class="form-field">
      <label :for="String(key)" class="block text-sm font-medium text-gray-700">
        {{ rawSchema.title || key }}
        <span v-if="!required.includes(key)" class="text-gray-400 font-normal">(optional)</span>
      </label>

      <!-- string (including Optional[str] resolved from anyOf) -->
      <input
        v-if="resolvedType(rawSchema) === 'string' && !rawSchema.enum"
        :id="String(key)"
        v-model="values[key]"
        :type="isSecretField(String(key)) ? 'password' : 'text'"
        :autocomplete="isSecretField(String(key)) ? 'new-password' : undefined"
        :list="suggestionsFor(String(key)).length ? `${String(key)}-suggestions` : undefined"
        :placeholder="isOptional(rawSchema) ? 'leave blank to omit' : ''"
        class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
      />
      <!-- Known-good values for a free-text param, offered as a datalist rather
           than a <select>: the caller's list can go stale (a profile is renamed
           or deleted) and a select would silently blank a value it doesn't
           recognise. A datalist suggests without constraining. -->
      <datalist
        v-if="resolvedType(rawSchema) === 'string' && !rawSchema.enum && suggestionsFor(String(key)).length"
        :id="`${String(key)}-suggestions`"
      >
        <option v-for="opt in suggestionsFor(String(key))" :key="opt" :value="opt" />
      </datalist>
      <p
        v-if="resolvedType(rawSchema) === 'string' && !rawSchema.enum && suggestionsFor(String(key)).length"
        class="mt-1 text-xs text-gray-500"
      >
        {{ suggestionsFor(String(key)).length }} available:
        <button
          v-for="opt in suggestionsFor(String(key)).slice(0, 6)"
          :key="opt"
          type="button"
          class="mr-2 font-mono text-indigo-600 hover:text-indigo-800"
          @click="values[key] = opt"
        >{{ opt }}</button>
      </p>

      <!-- number / integer -->
      <input
        v-else-if="resolvedType(rawSchema) === 'number' || resolvedType(rawSchema) === 'integer'"
        :id="String(key)"
        v-model.number="values[key]"
        type="number"
        :step="resolvedType(rawSchema) === 'integer' ? 1 : 'any'"
        class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
      />

      <!-- boolean -->
      <input
        v-else-if="resolvedType(rawSchema) === 'boolean'"
        :id="String(key)"
        v-model="values[key]"
        type="checkbox"
        class="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
      />

      <!-- string enum / select -->
      <select
        v-else-if="rawSchema.enum"
        :id="String(key)"
        v-model="values[key]"
        class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
      >
        <option v-if="!required.includes(key)" value="">—</option>
        <option v-for="opt in rawSchema.enum" :key="opt" :value="opt">{{ opt }}</option>
      </select>

      <!-- array with enum items — checkboxes (labelled via items.x-enum-labels when present) -->
      <div v-else-if="resolvedType(rawSchema) === 'array' && rawSchema.items?.enum">
        <div
          class="mt-1 space-y-1"
          :class="{ 'max-h-64 overflow-y-auto rounded-md border border-gray-200 p-2': rawSchema.items.enum.length > 12 }"
        >
          <label
            v-for="opt in rawSchema.items.enum"
            :key="opt"
            class="flex items-center space-x-2 text-sm"
          >
            <input
              type="checkbox"
              :value="opt"
              :checked="Array.isArray(values[key]) && values[key].includes(opt)"
              @change="toggleArrayValue(String(key), opt, $event)"
              class="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
            />
            <span>{{ enumLabel(rawSchema, opt) }}</span>
            <span v-if="enumLabel(rawSchema, opt) !== opt" class="text-gray-400 font-mono text-xs">{{ opt }}</span>
          </label>
        </div>
      </div>

      <!-- array of free-form strings — newline-separated textarea -->
      <div v-else-if="resolvedType(rawSchema) === 'array'">
        <textarea
          :id="String(key)"
          v-model="textBuffers[key]"
          @blur="syncTextToArray(String(key))"
          rows="3"
          placeholder="one value per line"
          class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm font-mono text-xs"
        />
        <p class="mt-1 text-xs text-gray-400">{{ $t('paramForm.onePerLine') }}</p>
      </div>

      <p v-if="rawSchema.description || defaultHint(rawSchema)" class="mt-1 text-xs text-gray-400">
        {{ rawSchema.description }}
        <span v-if="defaultHint(rawSchema)" class="text-gray-400">{{ defaultHint(rawSchema) }}</span>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  schema: Record<string, any>
  modelValue: Record<string, any>
  // Optional known-good values per param key, rendered as a datalist +
  // click-to-fill hints. Dynamic data (e.g. the rolling dataset profiles that
  // exist right now) can't live in a static JSON schema, so the caller supplies
  // it. Purely advisory — the field stays free text.
  suggestions?: Record<string, string[]>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, any>]
}>()

const values = ref({ ...props.modelValue })

watch(values, (v) => emit('update:modelValue', { ...v }), { deep: true })
watch(() => props.modelValue, (v) => {
  // Guard against the circular echo: parent mirrors back what we just emitted,
  // which would re-trigger our values watcher and cause an infinite update loop.
  if (JSON.stringify(v) === JSON.stringify(values.value)) return
  values.value = { ...v }
  initTextBuffers()
}, { deep: true })

const allProperties = computed<Record<string, any>>(() => props.schema.properties || {})

/**
 * Fields the module marks irrelevant to the current settings are not rendered.
 * Two schema extensions drive this, both emitted by the module (so the module
 * stays the single source of truth for what its params mean):
 *
 *   "x-hidden": true            — never render (deprecated/unused params kept
 *                                 only so older saved configs still load)
 *   "x-visible-when": {p: [v]}  — render only while every listed param holds
 *                                 one of the listed values
 *
 * A hidden field keeps whatever value it already has: it is dropped from the
 * form, not from the config, so switching modes back and forth is lossless and
 * a param the run does not read cannot be silently rewritten.
 */
const schemaProperties = computed<Record<string, any>>(() => {
  const visible: Record<string, any> = {}
  for (const [key, schema] of Object.entries(allProperties.value)) {
    if (schema['x-hidden']) continue
    // Deprecated params: shown only while they still carry a value, so they
    // stay clearable but don't clutter a config that has moved on.
    if (schema['x-visible-when-set'] && (values.value[key] === undefined
        || values.value[key] === null || values.value[key] === '')) continue
    const conditions = schema['x-visible-when']
    if (conditions && !Object.entries(conditions).every(
      ([dep, allowed]) => (allowed as any[]).includes(values.value[dep])
    )) continue
    visible[key] = schema
  }
  return visible
})

const required = computed(() => props.schema.required || [])

function suggestionsFor(key: string): string[] {
  return props.suggestions?.[key] ?? []
}

/**
 * Show the schema default for any param the saved config doesn't carry, so a
 * benchmark saved before a param existed displays what it will actually run
 * with instead of an empty box. Applied to the values (not just as a
 * placeholder) so what is shown is what gets saved.
 */
function applySchemaDefaults() {
  let changed = false
  for (const [key, schema] of Object.entries(allProperties.value)) {
    if (values.value[key] === undefined && schema.default !== undefined) {
      values.value[key] = schema.default
      changed = true
    }
  }
  if (changed) emit('update:modelValue', { ...values.value })
}

watch(() => props.schema, applySchemaDefaults, { immediate: true, deep: true })

// Local text buffers for array fields so newlines are preserved while typing
const textBuffers = ref<Record<string, string>>({})

function initTextBuffers() {
  for (const [key, rawSchema] of Object.entries(schemaProperties.value)) {
    if (resolvedType(rawSchema) === 'array' && !rawSchema.items?.enum) {
      textBuffers.value[key] = arrayToText(values.value[key])
    }
  }
}
initTextBuffers()

/**
 * Credential-carrying params (judge_api_key etc.) render as password inputs so
 * the value isn't shoulder-surfable / screenshot-leakable. Same name-suffix
 * convention as the backend's redaction (app/core/param_secrets.py).
 */
const SECRET_FIELD_RE = /(api_key|apikey|api_token|access_token|secret|password)$/i

function isSecretField(key: string): boolean {
  return SECRET_FIELD_RE.test(key)
}

/**
 * Pydantic v2 emits Optional[X] as { anyOf: [{type: X}, {type: "null"}] }.
 * Resolve the effective type for rendering.
 */
function resolvedType(schema: Record<string, any>): string | undefined {
  if (schema.type) return schema.type
  if (schema.anyOf) {
    const nonNull = schema.anyOf.find((s: any) => s.type !== 'null')
    return nonNull?.type
  }
  return undefined
}

function isOptional(schema: Record<string, any>): boolean {
  return !!schema.anyOf?.some((s: any) => s.type === 'null')
}

/**
 * Trailing "(default: X)" on the help text, so the shipped value stays visible
 * after an admin overrides the field and can be restored by hand.
 */
function defaultHint(schema: Record<string, any>): string {
  const def = schema.default
  if (def === undefined || def === null || def === '') return ''
  return ` (default: ${Array.isArray(def) ? def.join(', ') : def})`
}

/**
 * Friendly label for an array-enum option. A field may carry an
 * `items["x-enum-labels"]` map (key → display name); fall back to the raw value.
 */
function enumLabel(schema: Record<string, any>, opt: string): string {
  return schema.items?.['x-enum-labels']?.[opt] ?? opt
}

function arrayToText(val: unknown): string {
  if (!val) return ''
  if (Array.isArray(val)) return val.join('\n')
  return String(val)
}

function textToArray(text: string): string[] {
  return text.split('\n').map(s => s.trim()).filter(Boolean)
}

function syncTextToArray(key: string) {
  values.value[key] = textToArray(textBuffers.value[key] || '')
}

/**
 * Add/remove an option in an array-enum field. Tolerates a missing/undefined
 * value (treats it as an empty array) so configs saved before the field existed
 * still toggle correctly — Vue's array v-model would otherwise mis-handle them.
 */
function toggleArrayValue(key: string, opt: string, ev: Event) {
  const checked = (ev.target as HTMLInputElement).checked
  const cur = Array.isArray(values.value[key]) ? [...values.value[key]] : []
  const idx = cur.indexOf(opt)
  if (checked && idx === -1) cur.push(opt)
  else if (!checked && idx !== -1) cur.splice(idx, 1)
  values.value[key] = cur
}
</script>
