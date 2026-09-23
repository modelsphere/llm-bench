<template>
  <div class="flex flex-col bg-white rounded-lg border border-gray-200 shadow-sm hover:shadow-md transition-shadow">
    <div class="p-6 flex flex-col flex-1">
      <div class="flex items-start justify-between mb-2 gap-2">
        <div>
          <h2 class="text-lg font-semibold text-gray-900">
            {{ benchmark.name }}
            <span class="ml-1 text-sm font-normal text-gray-400" :title="$t('benchList.idTip')">#{{ benchmark.id }}</span>
          </h2>
          <p class="text-xs text-gray-400 font-mono">{{ benchmark.slug }}</p>
        </div>
        <div class="flex flex-col items-end gap-1 shrink-0">
          <div class="flex items-center gap-1.5">
            <router-link
              v-if="auth.isAdmin"
              :to="`/admin/benchmarks/${benchmark.slug}`"
              class="text-gray-400 hover:text-indigo-600 transition-colors"
              :title="$t('benchList.editTip')"
              :aria-label="$t('benchList.editTip')"
            >
              <CogIcon />
            </router-link>
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
              {{ $t('benchList.active') }}
            </span>
          </div>
        </div>
      </div>
      <p class="text-sm text-gray-500 mb-4 line-clamp-2">{{ benchmark.description }}</p>
      <div class="text-xs text-gray-400 mb-4">
        {{ $t('benchList.moduleCount', { n: benchmark.modules.length }) }} · v{{ benchmark.version }}
      </div>
      <div v-if="showGroups && benchmark.group_tags?.length" class="flex flex-wrap gap-1 mb-3">
        <span
          v-for="tag in benchmark.group_tags"
          :key="tag"
          class="inline-flex items-center px-2 py-0.5 rounded text-xs bg-indigo-50 text-indigo-700"
          :title="$t('benchList.groupTagTip')"
        >{{ tag }}</span>
      </div>
      <div class="flex flex-wrap gap-1 mb-4">
        <span
          v-for="(tag, i) in moduleTags.slice(0, MODULE_TAG_CAP)"
          :key="i"
          class="inline-flex items-center px-2 py-0.5 rounded text-xs bg-gray-100 text-gray-600"
          :class="{ 'opacity-40': tag.weight === 0 }"
          :title="tag.weight === 0 ? $t('benchList.weight0Tip', { label: tag.label }) : tag.label"
        >
          {{ tag.label }}
        </span>
        <span v-if="benchmark.modules.length > MODULE_TAG_CAP" class="self-center text-xs text-gray-400">
          {{ $t('benchList.more', { n: benchmark.modules.length - MODULE_TAG_CAP }) }}
        </span>
      </div>
      <router-link
        :to="`/benchmarks/${benchmark.slug}`"
        class="mt-auto block w-full text-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
      >
        {{ $t('benchList.viewSubmit') }}
      </router-link>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import CogIcon from '@/components/CogIcon.vue'
import { useAuthStore } from '@/stores/auth'
import { disambiguateNames } from '@/utils/modules'
import type { Benchmark } from '@/api/client'

const auth = useAuthStore()

const props = withDefaults(defineProps<{
  benchmark: Benchmark
  // Show the benchmark's group tags on the card. On in the flat view (where
  // grouping is otherwise invisible), off inside a grouped section where the
  // heading already says it.
  showGroups?: boolean
}>(), { showGroups: false })

// How many module tags to show per card before collapsing into "+N more".
const MODULE_TAG_CAP = 8

// Module tags for a card: duplicate names get a #1/#2 suffix, and weight-0
// modules carry their weight so the template can dim them.
const moduleTags = computed(() => {
  const labels = disambiguateNames(props.benchmark.modules.map((m) => m.module_name))
  return props.benchmark.modules.map((m, i) => ({ label: labels[i], weight: m.weight }))
})
</script>
