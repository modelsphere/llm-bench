<template>
  <div class="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
    <div class="flex items-center justify-between">
      <p class="text-sm font-medium text-gray-500 truncate">{{ label }}</p>
      <span
        v-if="passed != null"
        class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
        :class="passed ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'"
      >
        {{ passed ? $t('common.passed') : $t('common.notPassed') }}
      </span>
    </div>
    <p class="mt-2 text-2xl font-semibold" :class="valueColor">
      {{ formattedValue }}
    </p>
    <p v-if="sublabel" class="mt-1 text-xs text-gray-400">{{ sublabel }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  label: string
  value: string | number | null
  sublabel?: string
  passed?: boolean | null
  format?: 'score' | 'latency' | 'integer' | 'percent'
}>(), {
  passed: undefined,
})

const formattedValue = computed(() => {
  if (props.value === null || props.value === undefined) return '—'
  if (typeof props.value === 'string') return props.value
  switch (props.format) {
    case 'latency': return `${props.value.toFixed(0)} ms`
    case 'integer': return `${Math.round(props.value)}`
    case 'percent': return `${(props.value * 100).toFixed(1)}%`
    default: return props.value.toFixed(4)
  }
})

const valueColor = computed(() => {
  if (props.passed === false) return 'text-red-600'
  if (props.passed === true) return 'text-green-600'
  return 'text-gray-900'
})
</script>
