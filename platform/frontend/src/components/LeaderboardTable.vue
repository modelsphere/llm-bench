<template>
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">{{ $t('lb.rank') }}</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">
            <button type="button" class="inline-flex items-center gap-0.5 uppercase hover:text-gray-700" :title="$t('lb.sortTime')" @click="toggleSort('time')">
              ID <span :class="sortKey === 'time' ? 'text-indigo-600' : 'text-gray-300'">{{ sortKey === 'time' && sortDir === 'asc' ? '▲' : '▼' }}</span>
            </button>
          </th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">{{ $t('lb.user') }}</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">{{ $t('mySubs.model') }}</th>
          <th
            class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap"
            :title="$t('lb.summaryTip')"
          >{{ $t('submit.summary') }}</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">
            <button type="button" class="inline-flex items-center gap-0.5 uppercase hover:text-gray-700" :title="$t('lb.sortScore')" @click="toggleSort('score')">
              {{ $t('subDetail.score') }} <span :class="sortKey === 'score' ? 'text-indigo-600' : 'text-gray-300'">{{ sortKey === 'score' && sortDir === 'asc' ? '▲' : '▼' }}</span>
            </button>
          </th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">{{ $t('common.passed') }}</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">{{ $t('benchDetail.config') }}</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap">{{ $t('subDetail.submitted') }}</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap"></th>
          <th
            v-for="mod in modules"
            :key="mod.order_index"
            class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap"
          >
            {{ mod.name }}<span v-if="hasDuplicateName(mod.name)" class="text-gray-400"> #{{ mod.order_index }}</span>
          </th>
        </tr>
      </thead>
      <tbody class="bg-white divide-y divide-gray-200">
        <tr v-if="!rows || rows.length === 0">
          <td :colspan="modules.length + 11" class="px-4 py-8 text-center text-sm text-gray-500">
            {{ $t('lb.empty') }}
          </td>
        </tr>
        <tr v-for="row in displayRows" :key="row.rank" class="hover:bg-gray-50">
          <td class="px-4 py-3 text-sm font-medium text-gray-900">
            <span
              class="inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold"
              :class="rankClass(row.rank)"
            >
              {{ row.rank }}
            </span>
          </td>
          <td class="px-4 py-3 text-sm text-gray-500">
            <span v-if="row.submission_id != null">#{{ row.submission_id }}</span>
            <span v-else class="text-gray-400">—</span>
          </td>
          <td class="px-4 py-3 text-sm text-gray-700">{{ row.username }}</td>
          <td class="px-4 py-3 text-sm text-gray-700 font-mono text-xs">{{ row.endpoint_model }}</td>
          <td class="px-4 py-3 text-sm text-gray-600">
            <span
              v-if="row.description_summary"
              class="block max-w-[16rem] truncate"
              :title="row.description_summary"
            >{{ row.description_summary }}</span>
            <span v-else class="text-gray-400">—</span>
          </td>
          <td class="px-4 py-3 text-sm font-semibold text-indigo-600">
            {{ row.score_total != null ? Number(row.score_total).toFixed(4) : '—' }}
          </td>
          <td class="px-4 py-3 text-sm">
            <span
              v-if="row.passed != null"
              class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
              :class="row.passed ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'"
            >
              {{ row.passed ? $t('common.passed') : $t('common.notPassed') }}
            </span>
            <span v-else class="text-gray-400">—</span>
          </td>
          <td class="px-4 py-3 text-sm">
            <span
              v-if="row.config_hash"
              class="inline-flex items-center gap-1 font-mono text-xs px-1.5 py-0.5 rounded"
              :class="isStale(row.config_hash)
                ? 'bg-amber-50 text-amber-700 border border-amber-200'
                : 'bg-gray-100 text-gray-600'"
              :title="hashTitle(row.config_hash)"
            >
              {{ row.config_hash }}
              <span v-if="isStale(row.config_hash)" :title="$t('lb.staleTip')">⚠</span>
            </span>
            <span v-else class="text-gray-400 text-xs">—</span>
          </td>
          <td class="px-4 py-3 text-sm text-gray-500">
            {{ row.created_at ? new Date(row.created_at).toLocaleString() : '—' }}
          </td>
          <td class="px-4 py-3 text-sm">
            <router-link
              v-if="row.submission_id != null"
              :to="`/submissions/${row.submission_id}`"
              class="text-indigo-600 hover:text-indigo-800 font-medium"
            >
              {{ $t('lb.view') }}
            </router-link>
            <span v-else class="text-gray-400">—</span>
          </td>
          <td
            v-for="mod in modules"
            :key="mod.order_index"
            class="px-4 py-3 text-sm text-gray-600"
          >
            <template v-for="run in [getRun(row, mod.order_index)]" :key="mod.order_index">
              <span v-if="run" :class="run.passed ? 'text-green-600' : 'text-red-600'">
                {{ run.score != null ? Number(run.score).toFixed(3) : '—' }}
              </span>
              <span v-else class="text-gray-400">—</span>
            </template>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { LeaderboardRow, LeaderboardModule } from '@/api/client'

const { t } = useI18n()

const props = defineProps<{
  rows: LeaderboardRow[]
  modules: LeaderboardModule[]
  moduleNames?: string[]
  currentConfigHash?: string | null
}>()

// Client-side sort: 'score' keeps the server's rank order (score desc);
// 'time' orders by submission time. Click the active column to flip direction.
// The rank cell always shows the server-side score rank, whatever the sort.
const sortKey = ref<'score' | 'time'>('score')
const sortDir = ref<'asc' | 'desc'>('desc')
function toggleSort(key: 'score' | 'time') {
  if (sortKey.value === key) sortDir.value = sortDir.value === 'desc' ? 'asc' : 'desc'
  else { sortKey.value = key; sortDir.value = 'desc' }
}
const displayRows = computed(() => {
  const rows = [...props.rows]
  if (sortKey.value === 'time') {
    rows.sort((a, b) => new Date(a.created_at ?? 0).getTime() - new Date(b.created_at ?? 0).getTime())
    if (sortDir.value === 'desc') rows.reverse()
  } else if (sortDir.value === 'asc') {
    rows.reverse()
  }
  return rows
})

function getRun(row: LeaderboardRow, orderIndex: number) {
  return row.runs?.find(r => r.order_index === orderIndex)
}

function hasDuplicateName(name: string): boolean {
  return props.modules.filter(m => m.name === name).length > 1
}

function rankClass(rank: number) {
  if (rank === 1) return 'bg-yellow-100 text-yellow-800'
  if (rank === 2) return 'bg-gray-100 text-gray-800'
  if (rank === 3) return 'bg-orange-100 text-orange-800'
  return 'bg-gray-50 text-gray-600'
}

function isStale(hash: string | null): boolean {
  if (!hash || !props.currentConfigHash) return false
  return hash !== props.currentConfigHash
}

function hashTitle(hash: string | null): string {
  if (!hash) return ''
  if (isStale(hash)) return t('lb.hashStale', { hash, current: props.currentConfigHash })
  return t('lb.hashCurrent', { hash })
}
</script>
