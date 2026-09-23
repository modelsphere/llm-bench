<template>
  <div class="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6">
      <router-link :to="`/benchmarks/${slug}`" class="text-sm text-gray-500 hover:text-gray-700">
        {{ $t('submit.backToBenchmark') }}
      </router-link>
    </div>

    <div v-if="store.loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>

    <div v-else-if="benchmark" class="bg-white rounded-lg border border-gray-200 shadow-sm p-6">
      <h1 class="text-xl font-bold text-gray-900 mb-1">{{ $t('submit.titleBenchmark', { name: benchmark.name }) }}</h1>
      <p class="text-sm text-gray-500 mb-4">{{ $t('submit.paramsLocked') }}</p>

      <!-- Read-only params — compact chip row to keep the page short -->
      <div class="bg-gray-50 rounded-md px-4 py-3 mb-6 flex flex-wrap items-center gap-2">
        <span class="text-sm font-medium text-gray-700 mr-1">{{ $t('submit.modules') }}</span>
        <span
          v-for="mod in sortedModules"
          :key="`${mod.module_name}-${mod.order_index}`"
          class="inline-flex items-center gap-1 rounded-full bg-white border border-gray-200 px-2.5 py-0.5 text-xs text-gray-700"
        >
          {{ localizedModuleName(mod.module_name, mod.display_name) }}
          <span class="text-gray-400">w {{ mod.weight }}</span>
        </span>
      </div>

      <!-- ===== Form (visible when not in confirm step) ===== -->
      <form v-if="!showConfirm" @submit.prevent="onSubmitClick">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm mb-4">
          {{ error }}
        </div>
        <div v-if="success" class="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded text-sm mb-4">
          {{ $t('submit.queued') }} <router-link to="/submissions" class="underline">{{ $t('submit.viewSubmissions') }}</router-link>
        </div>

        <!-- Two columns on large screens so the form doesn't scroll forever -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-x-10 gap-y-6">
          <!-- ===== Left column: endpoint + description ===== -->
          <div class="space-y-4">
            <h3 class="text-sm font-semibold text-gray-900 border-b border-gray-100 pb-2">{{ $t('submit.endpoint') }}</h3>

            <div>
              <div class="flex items-center gap-1.5">
                <label for="endpoint_url" class="block text-sm font-medium text-gray-700">
                  {{ $t('submit.endpointUrl') }} <span class="text-red-500">*</span>
                </label>
                <HelpTip><span v-html="$t('submit.tips.endpointUrl')"></span></HelpTip>
              </div>
              <input
                id="endpoint_url"
                v-model="form.endpoint_url"
                type="url"
                required
                placeholder="https://api.example.com"
                class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
              />
            </div>

            <div>
              <label for="model" class="block text-sm font-medium text-gray-700">
                {{ $t('submit.model') }} <span class="text-red-500">*</span>
              </label>
              <input
                id="model"
                v-model="form.model"
                type="text"
                required
                placeholder="gpt-4o, claude-3-opus, ..."
                class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
              />
            </div>

            <div>
              <div class="flex items-center gap-1.5">
                <label for="api_key" class="block text-sm font-medium text-gray-700">
                  {{ $t('submit.apiKey') }} <span class="text-gray-400">{{ $t('submit.optional') }}</span>
                </label>
                <HelpTip>{{ $t('submit.tips.apiKey') }}</HelpTip>
              </div>
              <div class="relative mt-1">
                <input
                  id="api_key"
                  v-model.trim="form.api_key"
                  :type="showApiKey ? 'text' : 'password'"
                  :placeholder="$t('submit.apiKeyPlaceholder')"
                  class="block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm pr-16"
                />
                <button
                  type="button"
                  class="absolute inset-y-0 right-0 px-3 text-xs text-gray-500 hover:text-gray-700"
                  @click="showApiKey = !showApiKey"
                >
                  {{ showApiKey ? $t('submit.hide') : $t('submit.show') }}
                </button>
              </div>
            </div>

            <!-- ===== Description (optional) ===== -->
            <div class="pt-2 border-t border-gray-100">
              <h3 class="text-sm font-semibold text-gray-900 mt-3">{{ $t('submit.description') }}</h3>
              <p class="text-xs text-gray-500 mb-3">
                {{ $t('submit.descriptionHint') }}
              </p>

              <div>
                <div class="flex items-baseline justify-between">
                  <div class="flex items-center gap-1.5">
                    <label for="description_summary" class="block text-sm font-medium text-gray-700">{{ $t('submit.summary') }}</label>
                    <HelpTip>{{ $t('submit.tips.summary') }}</HelpTip>
                  </div>
                  <span class="text-xs" :class="form.description_summary.length >= SUMMARY_MAX ? 'text-amber-600' : 'text-gray-400'">
                    {{ form.description_summary.length }}/{{ SUMMARY_MAX }}
                  </span>
                </div>
                <input
                  id="description_summary"
                  v-model="form.description_summary"
                  type="text"
                  :maxlength="SUMMARY_MAX"
                  :placeholder="$t('submit.summaryPlaceholder')"
                  class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                />
              </div>

              <div class="mt-3">
                <div class="flex items-baseline justify-between">
                  <div class="flex items-center gap-1.5">
                    <label for="description_detail" class="block text-sm font-medium text-gray-700">{{ $t('submit.detail') }}</label>
                    <HelpTip>{{ $t('submit.tips.detail') }}</HelpTip>
                  </div>
                  <span class="text-xs" :class="form.description_detail.length >= DETAIL_MAX ? 'text-amber-600' : 'text-gray-400'">
                    {{ form.description_detail.length }}/{{ DETAIL_MAX }}
                  </span>
                </div>
                <textarea
                  id="description_detail"
                  v-model="form.description_detail"
                  rows="6"
                  :maxlength="DETAIL_MAX"
                  placeholder="## Setup&#10;- Serving stack, quantization, batching config…&#10;- What you optimized and why"
                  class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm font-mono"
                ></textarea>
              </div>
            </div>
          </div>

          <!-- ===== Right column: extras, attribution, hardware ===== -->
          <div class="space-y-4">
            <h3 class="text-sm font-semibold text-gray-900 border-b border-gray-100 pb-2">{{ $t('submit.options') }}</h3>

            <div>
              <div class="flex items-center gap-1.5">
                <span class="block text-sm font-medium text-gray-700">
                  {{ $t('submit.concurrencyOverrideShort') }}
                </span>
                <HelpTip>{{ $t('submit.tips.concurrencyModes') }}</HelpTip>
              </div>
              <!-- The two override mechanisms are mutually exclusive (backend-
                   enforced); a radio group makes that structural in the UI. -->
              <div class="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-700">
                <label class="inline-flex items-center gap-1.5">
                  <input v-model="ccMode" type="radio" value="none" class="text-indigo-600 focus:ring-indigo-500" />
                  {{ $t('submit.ccModeNone') }}
                </label>
                <label class="inline-flex items-center gap-1.5">
                  <input v-model="ccMode" type="radio" value="global" class="text-indigo-600 focus:ring-indigo-500" />
                  {{ $t('submit.ccModeGlobal') }}
                </label>
                <label class="inline-flex items-center gap-1.5">
                  <input v-model="ccMode" type="radio" value="per_module" class="text-indigo-600 focus:ring-indigo-500" />
                  {{ $t('submit.ccModePerModule') }}
                </label>
              </div>
              <p v-if="ccMode !== 'none'" class="mt-1 text-[11px] text-gray-400">{{ $t('submit.ccExclusive') }}</p>

              <template v-if="ccMode === 'global'">
                <input
                  id="concurrency_override"
                  v-model.number="form.concurrency_override"
                  type="number"
                  step="1"
                  min="1"
                  :placeholder="$t('submit.concurrencyPlaceholder')"
                  class="mt-2 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                />
                <p v-if="overridableModules.length" class="mt-1 text-xs text-indigo-600">
                  {{ $t('submit.appliesTo', { n: overridableModules.length, total: sortedModules.length, list: overridableModules.join(', ') }) }}
                </p>
                <p v-else class="mt-1 text-xs text-amber-700">
                  {{ $t('submit.noConcurrencyModules') }}
                </p>
              </template>

              <template v-else-if="ccMode === 'per_module'">
                <p v-if="overridableModuleRows.length" class="mt-2 text-xs text-gray-500">
                  {{ $t('submit.ccPerModuleHint') }}
                </p>
                <div
                  v-for="row in overridableModuleRows"
                  :key="row.name"
                  class="mt-2 flex items-center justify-between gap-3"
                >
                  <label
                    :for="`cc_module_${row.name}`"
                    class="text-sm text-gray-700"
                  >{{ row.label }}</label>
                  <input
                    :id="`cc_module_${row.name}`"
                    v-model.number="form.module_concurrency_overrides[row.name]"
                    type="number"
                    step="1"
                    min="1"
                    :placeholder="row.configured != null ? $t('submit.ccModuleDefault', { n: row.configured }) : ''"
                    class="w-28 shrink-0 rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                  />
                </div>
                <p v-if="!overridableModuleRows.length" class="mt-1 text-xs text-amber-700">
                  {{ $t('submit.noConcurrencyModules') }}
                </p>
              </template>
            </div>

            <!-- ===== Attribution (optional) ===== -->
            <div class="pt-2 border-t border-gray-100">
              <div class="flex items-center gap-1.5 mt-3 mb-3">
                <h3 class="text-sm font-semibold text-gray-900">{{ $t('submit.attribution') }}</h3>
                <HelpTip>{{ $t('submit.tips.contributor') }}</HelpTip>
              </div>

              <div>
                <label for="contributor" class="block text-sm font-medium text-gray-700">{{ $t('submit.contributor') }}</label>
                <input
                  id="contributor"
                  v-model="form.contributor"
                  type="text"
                  placeholder="your-name"
                  class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                />
              </div>
            </div>

            <!-- ===== Hardware (optional) ===== -->
            <div class="pt-2 border-t border-gray-100">
              <div class="flex items-center gap-1.5 mt-3 mb-3">
                <h3 class="text-sm font-semibold text-gray-900">
                  {{ $t('submit.hardware') }} <span v-if="!hardwareRequired">{{ $t('submit.optional') }}</span><span v-else class="text-red-500">*</span>
                </h3>
                <HelpTip>{{ $t('submit.tips.hardware') }}</HelpTip>
              </div>

              <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label for="cards_per_machine" class="block text-sm font-medium text-gray-700">
                    {{ $t('submit.cardsPerMachine') }} <span v-if="hardwareRequired" class="text-red-500">*</span>
                  </label>
                  <input
                    id="cards_per_machine"
                    v-model.number="form.cards_per_machine"
                    type="number"
                    step="1"
                    min="1"
                    placeholder="e.g. 8"
                    class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm placeholder:italic placeholder:text-gray-400"
                  />
                </div>
                <div>
                  <label for="machine_count" class="block text-sm font-medium text-gray-700">
                    {{ $t('submit.machineCount') }} <span v-if="hardwareRequired" class="text-red-500">*</span>
                  </label>
                  <input
                    id="machine_count"
                    v-model.number="form.machine_count"
                    type="number"
                    step="1"
                    min="1"
                    placeholder="e.g. 1"
                    class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm placeholder:italic placeholder:text-gray-400"
                  />
                </div>
                <div>
                  <label for="card_type" class="block text-sm font-medium text-gray-700">
                    {{ $t('submit.cardType') }} <span v-if="hardwareRequired" class="text-red-500">*</span>
                  </label>
                  <select
                    id="card_type"
                    v-model="form.card_type"
                    class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                  >
                    <option value="">—</option>
                    <option v-for="ct in cardTypes" :key="ct.id" :value="ct.name">{{ ct.name }}</option>
                  </select>
                </div>
              </div>
              <p v-if="hardwareRequired && !hardwareComplete" class="mt-2 text-xs text-amber-700">
                {{ $t('submit.hardwareRequiredNote') }}
              </p>
            </div>
          </div>
        </div>

        <!-- ===== Full-width footer: quota + preflight + submit ===== -->
        <div class="mt-6 space-y-4">
          <QuotaGuard ref="quotaGuard" @blocked="quotaBlocked = $event" />
          <EndpointPreflight
            :endpoint-url="form.endpoint_url"
            :model="form.model"
            :api-key="form.api_key"
            @status="preflightStatus = $event"
          />

          <!-- Hollow until the preflight passes — testing is recommended, not forced. -->
          <button
            type="submit"
            :disabled="loading || success || quotaBlocked"
            class="w-full flex justify-center py-2 px-4 border-2 rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
            :class="preflightStatus === 'pass'
              ? 'border-transparent bg-green-600 text-white shadow-sm hover:bg-green-700'
              : 'border-green-600 bg-white text-green-700 hover:bg-green-50'"
          >
            {{ $t('submit.reviewSubmit') }}
          </button>
        </div>
      </form>

      <!-- ===== Confirmation step ===== -->
      <div v-else class="space-y-4 max-w-2xl mx-auto">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
          {{ error }}
        </div>
        <h3 class="text-base font-semibold text-gray-900">{{ $t('submit.confirmTitle') }}</h3>
        <dl class="text-sm divide-y divide-gray-100 border border-gray-200 rounded-md">
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.endpointUrl') }}</dt>
            <dd class="text-gray-900 break-all text-right ml-3">{{ form.endpoint_url }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.model') }}</dt>
            <dd class="text-gray-900 text-right ml-3">{{ form.model }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.apiKey') }}</dt>
            <dd class="text-gray-900 text-right ml-3 font-mono">{{ maskedApiKey }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.descriptionSummary') }}</dt>
            <dd v-if="form.description_summary.trim()" class="text-gray-900 text-right ml-3">{{ form.description_summary.trim() }}</dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.notSet') }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.descriptionDetail') }}</dt>
            <dd v-if="form.description_detail.trim()" class="text-gray-900 text-right ml-3">
              {{ $t('submit.charsMarkdown', { n: form.description_detail.trim().length }) }}
            </dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.notSet') }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.concurrencyOverrideShort') }}</dt>
            <dd v-if="ccMode === 'global' && form.concurrency_override" class="text-gray-900 text-right ml-3">{{ form.concurrency_override }}</dd>
            <dd v-else-if="ccMode === 'per_module' && Object.keys(perModuleOverrides).length" class="text-gray-900 text-right ml-3">
              <div v-for="(v, name) in perModuleOverrides" :key="name">{{ moduleLabel(String(name)) }}: <span class="font-mono">{{ v }}</span></div>
            </dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.moduleDefaults') }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.contributor') }}</dt>
            <dd v-if="form.contributor" class="text-gray-900 text-right ml-3">{{ form.contributor }}</dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.notSet') }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.cardsPerMachine') }}</dt>
            <dd v-if="form.cards_per_machine" class="text-gray-900 text-right ml-3">{{ form.cards_per_machine }}</dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.notSet') }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.machineCount') }}</dt>
            <dd v-if="form.machine_count" class="text-gray-900 text-right ml-3">{{ form.machine_count }}</dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.notSet') }}</dd>
          </div>
          <div class="flex justify-between px-3 py-2">
            <dt class="text-gray-500">{{ $t('submit.cardType') }}</dt>
            <dd v-if="form.card_type" class="text-gray-900 text-right ml-3">{{ form.card_type }}</dd>
            <dd v-else class="text-gray-400 text-right ml-3 italic">{{ $t('submit.notSet') }}</dd>
          </div>
        </dl>

        <div
          v-if="hardwareEmpty"
          class="bg-amber-50 border border-amber-300 text-amber-800 px-4 py-3 rounded text-sm"
        >
          <p class="font-medium">{{ $t('submit.noHardwareTitle') }}</p>
          <p class="mt-1">
            {{ $t('submit.noHardwareBody') }}
          </p>
        </div>

        <div class="flex gap-3">
          <button
            type="button"
            :disabled="loading"
            class="flex-1 py-2 px-4 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
            @click="showConfirm = false"
          >
            {{ $t('submit.editBack') }}
          </button>
          <button
            type="button"
            :disabled="loading || success || quotaBlocked"
            class="flex-1 py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-green-600 hover:bg-green-700 disabled:opacity-50"
            @click="handleSubmit"
          >
            {{ loading ? $t('submit.submitting') : $t('submit.confirmSubmit') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useBenchmarksStore } from '@/stores/benchmarks'
import { submissionsApi, cardTypesApi, type CardType } from '@/api/client'
import { localizedModuleName } from '@/utils/modules'
import EndpointPreflight from '@/components/EndpointPreflight.vue'
import QuotaGuard from '@/components/QuotaGuard.vue'
import HelpTip from '@/components/HelpTip.vue'

// Keep in sync with the backend schema caps (app/schemas/benchmarks.py).
const SUMMARY_MAX = 100
const DETAIL_MAX = 5000

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const store = useBenchmarksStore()

const slug = computed(() => route.params.slug as string)
const benchmark = computed(() => store.currentBenchmark)
const sortedModules = computed(() =>
  [...(benchmark.value?.modules || [])].sort((a, b) => a.order_index - b.order_index)
)
// Modules in THIS benchmark that the concurrency override will affect. The
// backend derives `supports_concurrency_override` from each module's schema, so
// this list stays correct automatically as modules are added/changed.
const overridableModules = computed(() =>
  sortedModules.value
    .filter((m) => m.supports_concurrency_override)
    .map((m) => localizedModuleName(m.module_name, m.display_name)),
)

// One input row per overridable module for the per-module mode, deduped by
// module_name (a benchmark can contain the same module twice; the override map
// is keyed by name and applies to every instance). `configured` is the
// benchmark's own value, shown as the input placeholder.
const overridableModuleRows = computed(() => {
  const rows: { name: string; label: string; configured: number | null }[] = []
  const seen = new Set<string>()
  for (const m of sortedModules.value) {
    if (!m.supports_concurrency_override || seen.has(m.module_name)) continue
    seen.add(m.module_name)
    const configured = m.params_json?.concurrency ?? m.params_json?.max_workers ?? null
    rows.push({ name: m.module_name, label: localizedModuleName(m.module_name, m.display_name), configured })
  }
  return rows
})

// Which override mechanism the submitter picked. The backend rejects payloads
// setting both, so only the active mode's values are ever sent.
const ccMode = ref<'none' | 'global' | 'per_module'>('none')

const form = ref<{
  endpoint_url: string
  model: string
  api_key: string
  contributor: string
  concurrency_override: number | null
  module_concurrency_overrides: Record<string, number | null>
  cards_per_machine: number | null
  machine_count: number | null
  card_type: string
  description_summary: string
  description_detail: string
}>({
  endpoint_url: '',
  model: '',
  api_key: '',
  contributor: '',
  concurrency_override: null,
  module_concurrency_overrides: {},
  cards_per_machine: null,
  machine_count: null,
  card_type: '',
  description_summary: '',
  description_detail: '',
})

// Per-module map as it will be submitted: filled, positive entries only.
// (v-model.number leaves '' when an input is cleared — treat as unset.)
const perModuleOverrides = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const [name, v] of Object.entries(form.value.module_concurrency_overrides)) {
    if (typeof v === 'number' && Number.isFinite(v) && v >= 1) out[name] = Math.floor(v)
  }
  return out
})

const effectiveExtraParams = computed(() => {
  if (ccMode.value === 'global' && form.value.concurrency_override)
    return { concurrency_override: form.value.concurrency_override }
  if (ccMode.value === 'per_module' && Object.keys(perModuleOverrides.value).length)
    return { module_concurrency_overrides: perModuleOverrides.value }
  return null
})

function moduleLabel(name: string): string {
  return overridableModuleRows.value.find((r) => r.name === name)?.label ?? name
}
const cardTypes = ref<CardType[]>([])
const loading = ref(false)
const error = ref('')
const success = ref(false)
const showConfirm = ref(false)
const showApiKey = ref(false)
const preflightStatus = ref<'idle' | 'pass' | 'warn' | 'fail'>('idle')
const quotaGuard = ref<InstanceType<typeof QuotaGuard> | null>(null)
const quotaBlocked = ref(false)

const maskedApiKey = computed(() => mask(form.value.api_key))

// Hardware is never mandatory, but without it the card-normalized throughput
// metrics assume the endpoint is already at the baseline card count — see the
// warning below, and app/core/card_normalize.py on the server.
const hardwareRequired = computed(() => false)
const hardwareComplete = computed(() =>
  Boolean(form.value.cards_per_machine) &&
  Boolean(form.value.machine_count) &&
  Boolean(form.value.card_type),
)
const hardwareEmpty = computed(() =>
  !form.value.cards_per_machine &&
  !form.value.machine_count &&
  !form.value.card_type,
)

function mask(secret: string): string {
  // Whitespace-only is NOT a key: the backend trims it to "" and sends no
  // Authorization header, so showing •••• here would misreport what runs.
  if (!secret.trim()) return ''
  if (secret.length <= 4) return '••••'
  return '••••' + secret.slice(-4)
}

onMounted(async () => {
  store.fetchBenchmark(slug.value)
  try {
    const { data } = await cardTypesApi.list()
    cardTypes.value = data.card_types
  } catch {
    // Non-fatal — the dropdown just stays empty; hardware is optional.
    cardTypes.value = []
  }
})

function onSubmitClick() {
  error.value = ''
  if (hardwareRequired.value && !hardwareComplete.value) {
    error.value = t('submit.hardwareRequiredError')
    return
  }
  showConfirm.value = true
}

async function handleSubmit() {
  loading.value = true
  error.value = ''
  try {
    const payload = {
      endpoint_url: form.value.endpoint_url,
      model: form.value.model,
      api_key: form.value.api_key,
      contributor: form.value.contributor || null,
      extra_params: effectiveExtraParams.value,
      cards_per_machine: form.value.cards_per_machine || null,
      machine_count: form.value.machine_count || null,
      card_type: form.value.card_type || null,
      description_summary: form.value.description_summary.trim() || null,
      description_detail: form.value.description_detail.trim() || null,
    }
    const { data } = await submissionsApi.submitBenchmark(slug.value, payload)
    success.value = true
    setTimeout(() => router.push(`/submissions/${data.id}`), 1500)
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('submit.submitFailed')
    // A 429 means the quota filled since the page loaded — re-fetch so the
    // banner appears and the buttons lock.
    if (e.response?.status === 429) {
      showConfirm.value = false
      quotaGuard.value?.refresh()
    }
  } finally {
    loading.value = false
  }
}
</script>
