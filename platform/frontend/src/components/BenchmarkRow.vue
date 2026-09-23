<template>
  <router-link
    :to="`/benchmarks/${benchmark.slug}`"
    class="flex items-center gap-3 px-4 py-2.5 bg-white hover:bg-indigo-50/60 transition-colors"
  >
    <div class="min-w-0 flex-1">
      <div class="flex items-baseline gap-2 min-w-0">
        <span class="text-sm font-medium text-gray-900 truncate">{{ benchmark.name }}</span>
        <span class="text-xs text-gray-400 shrink-0" :title="$t('benchList.idTip')">#{{ benchmark.id }}</span>
        <span class="text-xs text-gray-400 font-mono truncate hidden sm:inline">{{ benchmark.slug }}</span>
      </div>
      <p v-if="benchmark.description" class="text-xs text-gray-500 truncate">{{ benchmark.description }}</p>
    </div>
    <div class="hidden md:flex items-center gap-1 shrink-0 max-w-[40%] overflow-hidden">
      <span
        v-for="(tag, i) in moduleTags.slice(0, ROW_TAG_CAP)"
        :key="i"
        class="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] bg-gray-100 text-gray-600 whitespace-nowrap"
        :class="{ 'opacity-40': tag.weight === 0 }"
        :title="tag.weight === 0 ? $t('benchList.weight0Tip', { label: tag.label }) : tag.label"
      >{{ tag.label }}</span>
      <span v-if="benchmark.modules.length > ROW_TAG_CAP" class="text-[11px] text-gray-400 whitespace-nowrap">
        {{ $t('benchList.more', { n: benchmark.modules.length - ROW_TAG_CAP }) }}
      </span>
    </div>
    <span class="text-xs text-gray-400 shrink-0 whitespace-nowrap">
      {{ $t('benchList.moduleCount', { n: benchmark.modules.length }) }} · v{{ benchmark.version }}
    </span>
    <button
      v-if="auth.isAdmin"
      type="button"
      class="text-gray-400 hover:text-indigo-600 transition-colors shrink-0"
      :title="$t('benchList.editTip')"
      :aria-label="$t('benchList.editTip')"
      @click.prevent.stop="router.push(`/admin/benchmarks/${benchmark.slug}`)"
    >
      <CogIcon />
    </button>
    <span class="text-indigo-600 text-sm shrink-0">→</span>
  </router-link>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import CogIcon from '@/components/CogIcon.vue'
import { useAuthStore } from '@/stores/auth'
import { disambiguateNames } from '@/utils/modules'
import type { Benchmark } from '@/api/client'

const props = defineProps<{ benchmark: Benchmark }>()
const auth = useAuthStore()
const router = useRouter()

// Rows are one line, so fewer module tags fit than on a card.
const ROW_TAG_CAP = 4

const moduleTags = computed(() => {
  const labels = disambiguateNames(props.benchmark.modules.map((m) => m.module_name))
  return props.benchmark.modules.map((m, i) => ({ label: labels[i], weight: m.weight }))
})
</script>
