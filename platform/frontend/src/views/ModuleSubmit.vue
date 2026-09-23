<template>
  <div class="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6">
      <router-link to="/benchmarks" class="text-sm text-gray-500 hover:text-gray-700">
        {{ $t('common.back') }}
      </router-link>
    </div>

    <div v-if="!moduleDesc" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>

    <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm p-6">
      <h1 class="text-xl font-bold text-gray-900 mb-1">
        {{ $t('submit.titleModule', { name: localizedModuleName(moduleDesc.name, moduleDesc.display_name) }) }}
        <span v-if="moduleDesc.deprecated"
          class="align-middle ml-2 inline-block rounded bg-amber-100 text-amber-800 text-xs font-semibold px-2 py-0.5">
          {{ $t('submit.deprecated') }}
        </span>
      </h1>
      <p class="text-sm text-gray-500 mb-4">{{ moduleDesc.description }}</p>

      <div v-if="moduleDesc.deprecated"
        class="mb-6 bg-amber-50 border border-amber-300 text-amber-800 px-4 py-3 rounded text-sm">
        <span class="font-semibold">{{ $t('submit.deprecatedBanner') }}</span>
        {{ moduleDesc.deprecation_note || $t('submit.deprecatedDefault') }}
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
          <!-- ===== Left column: module params ===== -->
          <div>
            <h3 class="text-sm font-semibold text-gray-900 border-b border-gray-100 pb-2 mb-4">{{ $t('submit.moduleParams') }}</h3>
            <!-- Module-specific params (editable for ad-hoc runs) -->
            <ModuleParamForm
              :schema="moduleDesc.params_schema"
              v-model="params"
            />
          </div>

          <!-- ===== Right column: endpoint + description + options ===== -->
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
                  rows="5"
                  :maxlength="DETAIL_MAX"
                  placeholder="## Setup&#10;- Serving stack, quantization, batching config…&#10;- What you optimized and why"
                  class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm font-mono"
                ></textarea>
              </div>
            </div>

            <!-- ===== Extra params (optional) ===== -->
            <div class="pt-2 border-t border-gray-100">
              <h3 class="text-sm font-semibold text-gray-900 mt-3">{{ $t('submit.extraParams') }}</h3>
              <div class="mt-3">
                <div class="flex items-center gap-1.5">
                  <label for="concurrency_override" class="block text-sm font-medium text-gray-700">
                    {{ $t('submit.concurrencyOverrideShort') }}
                  </label>
                  <HelpTip>{{ $t('submit.tips.concurrencySingle') }}</HelpTip>
                </div>
                <input
                  id="concurrency_override"
                  v-model.number="form.concurrency_override"
                  type="number"
                  step="1"
                  min="1"
                  :placeholder="$t('submit.concurrencyModulePlaceholder')"
                  class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
                />
                <p v-if="moduleDesc?.supports_concurrency_override" class="mt-1 text-xs text-indigo-600">
                  {{ $t('submit.concurrencyApplies', { name: localizedModuleName(moduleDesc.name, moduleDesc.display_name) }) }}
                </p>
                <p v-else class="mt-1 text-xs text-amber-700">
                  {{ $t('submit.concurrencyNoEffect', { name: moduleDesc ? localizedModuleName(moduleDesc.name, moduleDesc.display_name) : '' }) }}
                </p>
              </div>
            </div>

            <!-- Attribution (optional) -->
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
                <h3 class="text-sm font-semibold text-gray-900">{{ $t('submit.hardware') }} {{ $t('submit.optional') }}</h3>
                <HelpTip>{{ $t('submit.tips.hardwareRecorded') }}</HelpTip>
              </div>

              <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label for="cards_per_machine" class="block text-sm font-medium text-gray-700">
                    Cards per machine
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
                    Machine count
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
                  <label for="card_type" class="block text-sm font-medium text-gray-700">{{ $t('submit.cardType') }}</label>
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
            {{ $t('submit.reviewRun') }}
          </button>
        </div>
      </form>

      <!-- ===== Confirmation step ===== -->
      <div v-else class="space-y-4 max-w-2xl mx-auto">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
          {{ error }}
        </div>
        <h3 class="text-base font-semibold text-gray-900">{{ $t('submit.confirmRunTitle') }}</h3>
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
            {{ loading ? $t('submit.submitting') : $t('submit.confirmRun') }}
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
import { submissionsApi, cardTypesApi } from '@/api/client'
import { localizedModuleName } from '@/utils/modules'
import ModuleParamForm from '@/components/ModuleParamForm.vue'
import EndpointPreflight from '@/components/EndpointPreflight.vue'
import QuotaGuard from '@/components/QuotaGuard.vue'
import HelpTip from '@/components/HelpTip.vue'
import type { ModuleDescriptor, CardType } from '@/api/client'

// Keep in sync with the backend schema caps (app/schemas/benchmarks.py).
const SUMMARY_MAX = 100
const DETAIL_MAX = 5000

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const name = computed(() => route.params.name as string)
const moduleDesc = ref<ModuleDescriptor | null>(null)
const params = ref<Record<string, any>>({})
const form = ref<{
  endpoint_url: string
  model: string
  api_key: string
  contributor: string
  concurrency_override: number | null
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
  cards_per_machine: null,
  machine_count: null,
  card_type: '',
  description_summary: '',
  description_detail: '',
})
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

function mask(secret: string): string {
  // Whitespace-only is NOT a key: the backend trims it to "" and sends no
  // Authorization header, so showing •••• here would misreport what runs.
  if (!secret.trim()) return ''
  if (secret.length <= 4) return '••••'
  return '••••' + secret.slice(-4)
}

onMounted(async () => {
  try {
    const { data } = await import('@/api/client').then(m => m.modulesApi.get(name.value))
    moduleDesc.value = data
    params.value = { ...data.default_params }
  } catch {
    error.value = 'Module not found'
  }
  try {
    const { data } = await cardTypesApi.list()
    cardTypes.value = data.card_types
  } catch {
    cardTypes.value = []
  }
})

function onSubmitClick() {
  error.value = ''
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
      extra_params: form.value.concurrency_override
        ? { concurrency_override: form.value.concurrency_override }
        : null,
      cards_per_machine: form.value.cards_per_machine || null,
      machine_count: form.value.machine_count || null,
      card_type: form.value.card_type || null,
      description_summary: form.value.description_summary.trim() || null,
      description_detail: form.value.description_detail.trim() || null,
    }
    const { data } = await submissionsApi.submitModule(name.value, payload)
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
