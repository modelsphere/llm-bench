<template>
  <div class="rounded-md border border-gray-200 bg-gray-50 p-3">
    <div class="flex items-center justify-between gap-3">
      <div>
        <p class="text-sm font-medium text-gray-700">{{ $t('preflight.title') }}</p>
        <p class="text-xs text-gray-500">
          {{ $t('preflight.subtitle') }}
        </p>
      </div>

      <!-- Button + hover popover. Results float beside the button instead of
           expanding the box, so the submit button below never moves. -->
      <span class="group relative inline-flex shrink-0">
        <button
          type="button"
          :disabled="!canTest || loading"
          class="py-1.5 px-4 border-2 rounded-full text-sm font-semibold transition-all disabled:opacity-40"
          :class="buttonClass"
          @click="runTest"
        >
          {{ buttonLabel }}
        </button>

        <div
          class="invisible absolute right-full top-1/2 z-20 mr-3 w-96 max-w-[80vw] -translate-y-1/2 rounded-md border border-gray-200 bg-white p-3 text-left shadow-lg opacity-0 transition-opacity duration-100 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
        >
          <p v-if="!canTest" class="text-xs text-gray-500">
            {{ $t('preflight.needInputs') }}
          </p>

          <p v-else-if="testError" class="text-xs text-red-700">{{ testError }}</p>

          <div v-else-if="result" class="space-y-2">
            <div
              class="flex items-center gap-2 text-sm font-semibold"
              :class="result.ok ? 'text-green-700' : 'text-red-700'"
            >
              <span>{{ result.ok ? $t('preflight.ready') : $t('preflight.problems') }}</span>
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
              {{ $t('preflight.canStillSubmit') }}
            </p>
          </div>

          <p v-else class="text-xs text-gray-500">
            {{ $t('preflight.clickHint') }}
          </p>

          <span class="absolute left-full top-1/2 -translate-y-1/2 border-4 border-transparent border-l-gray-200"></span>
        </div>
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { submissionsApi, type PreflightResult, type PreflightStatus } from '@/api/client'

const { t } = useI18n()
const props = defineProps<{ endpointUrl: string; model: string; apiKey: string }>()
const emit = defineEmits<{ (e: 'status', value: 'idle' | 'pass' | 'warn' | 'fail'): void }>()

const loading = ref(false)
const result = ref<PreflightResult | null>(null)
const testError = ref('')

const canTest = computed(() => props.endpointUrl.trim() !== '' && props.model.trim() !== '')

// Button state: idle (never run) → pass / warn (ok but e.g. model id could not
// be verified) / fail (a check failed or the probe itself errored).
const status = computed<'idle' | 'pass' | 'warn' | 'fail'>(() => {
  if (testError.value) return 'fail'
  if (!result.value) return 'idle'
  if (!result.value.ok) return 'fail'
  return result.value.checks.some((c) => c.status === 'warn') ? 'warn' : 'pass'
})

// Let the parent react (e.g. the submit button only turns solid green once the
// preflight passes). Resets to 'idle' automatically when the inputs change.
watch(status, (s) => emit('status', s), { immediate: true })

const buttonLabel = computed(() => {
  if (loading.value) return t('preflight.testing')
  switch (status.value) {
    case 'pass': return t('preflight.ok')
    case 'warn': return t('preflight.warn')
    case 'fail': return t('preflight.fail')
    default: return t('preflight.test')
  }
})

const buttonClass = computed(() => {
  switch (status.value) {
    case 'pass': return 'border-transparent bg-green-600 text-white shadow-sm hover:bg-green-700'
    case 'warn': return 'border-transparent bg-yellow-400 text-yellow-950 shadow-sm hover:bg-yellow-500'
    case 'fail': return 'border-transparent bg-red-600 text-white shadow-sm hover:bg-red-700'
    default: return 'border-amber-400 bg-white text-orange-600 hover:bg-amber-50'
  }
})

// Stale results would be misleading — clear them whenever the inputs change.
watch(
  () => [props.endpointUrl, props.model, props.apiKey],
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
    const { data } = await submissionsApi.preflight({
      endpoint_url: props.endpointUrl,
      model: props.model,
      api_key: props.apiKey,
    })
    result.value = data
  } catch (e: any) {
    testError.value = e.response?.data?.detail || t('preflight.runFailed')
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
