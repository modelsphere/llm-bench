<template>
  <div class="rounded-md border border-gray-200 bg-gray-50 p-3">
    <div class="flex items-start justify-between gap-3">
      <div>
        <p class="text-sm font-medium text-gray-700">{{ $t('judge.title') }}</p>
        <p class="text-xs text-gray-500">
          Send one real grading call to the judge with the configured token budget.
          Judge problems at run time are swallowed as NOT_ATTEMPTED grades — test here instead.
        </p>
      </div>
      <button
        type="button"
        :disabled="!canTest || loading"
        class="shrink-0 py-1.5 px-3 border border-gray-300 rounded-md text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
        @click="runTest"
      >
        {{ loading ? $t('preflight.testing') : $t('judge.test') }}
      </button>
    </div>

    <p v-if="!canTest && !result" class="mt-2 text-xs text-gray-400">
      Enter a judge endpoint URL and judge model to test (both blank = self-judge, nothing to test).
    </p>

    <div v-if="result" class="mt-3 space-y-2">
      <div
        class="flex items-center gap-2 text-sm font-semibold"
        :class="result.ok ? 'text-green-700' : 'text-red-700'"
      >
        <span>{{ result.ok ? $t('judge.ready') : $t('judge.problems') }}</span>
        <span v-if="result.latency_ms != null" class="text-xs font-normal text-gray-500">
          ({{ Math.round(result.latency_ms) }} ms)
        </span>
      </div>
      <ul class="space-y-1">
        <li v-for="(c, i) in result.checks" :key="i" class="flex gap-2 text-xs leading-snug">
          <span class="shrink-0 font-bold w-4 text-center" :class="iconClass(c.status)">{{ icon(c.status) }}</span>
          <span class="text-gray-700"><span class="font-medium">{{ c.name }}:</span> {{ c.detail }}</span>
        </li>
      </ul>
      <p v-if="!result.ok" class="text-xs text-gray-400">
        A failing judge doesn't error the benchmark — it silently grades every sample
        NOT_ATTEMPTED and the score reads as 0.
      </p>
    </div>

    <p v-if="testError" class="mt-3 text-xs text-red-700">{{ testError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { submissionsApi, type PreflightResult, type PreflightStatus } from '@/api/client'

const { t } = useI18n()
const props = defineProps<{
  endpointUrl: string
  model: string
  apiKey: string
  maxTokens: number
}>()

const loading = ref(false)
const result = ref<PreflightResult | null>(null)
const testError = ref('')

const canTest = computed(() => props.endpointUrl.trim() !== '' && props.model.trim() !== '')

// Stale results would be misleading — clear them whenever the inputs change.
watch(
  () => [props.endpointUrl, props.model, props.apiKey, props.maxTokens],
  () => {
    result.value = null
    testError.value = ''
  },
)

async function runTest() {
  loading.value = true
  testError.value = ''
  result.value = null
  try {
    const { data } = await submissionsApi.judgePreflight({
      endpoint_url: props.endpointUrl,
      model: props.model,
      api_key: props.apiKey,
      max_tokens: props.maxTokens,
    })
    result.value = data
  } catch (e: any) {
    testError.value = e.response?.data?.detail || t('judge.runFailed')
  } finally {
    loading.value = false
  }
}

function icon(status: PreflightStatus): string {
  return status === 'pass' ? '✓' : status === 'fail' ? '✗' : status === 'warn' ? '!' : '–'
}

function iconClass(status: PreflightStatus): string {
  return status === 'pass'
    ? 'text-green-600'
    : status === 'fail'
      ? 'text-red-600'
      : status === 'warn'
        ? 'text-amber-600'
        : 'text-gray-400'
}
</script>
