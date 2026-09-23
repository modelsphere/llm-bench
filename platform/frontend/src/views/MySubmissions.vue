<template>
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6">
      <h1 class="text-2xl font-bold text-gray-900">{{ $t('nav.mySubmissions') }}</h1>
    </div>

    <div v-if="store.loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div v-else-if="store.error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
      {{ store.error }}
    </div>
    <div v-else-if="store.mySubmissions.length === 0" class="text-center py-12 text-gray-500">
      {{ $t('mySubs.empty') }}
      <router-link to="/benchmarks" class="text-indigo-600 hover:text-indigo-500 ml-1">{{ $t('mySubs.browse') }}</router-link>
    </div>

    <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
      <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">#</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.benchmark') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('mySubs.benchmarkId') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('mySubs.model') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.status') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.score') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.submitted') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-200">
          <tr v-for="s in store.mySubmissions" :key="s.id" class="hover:bg-gray-50">
            <td class="px-4 py-3 text-sm text-gray-500">#{{ s.id }}</td>
            <td class="px-4 py-3 text-sm text-gray-700 font-medium">
              <router-link
                v-if="s.benchmark_slug"
                :to="`/benchmarks/${s.benchmark_slug}`"
                class="text-indigo-600 hover:text-indigo-500"
              >{{ s.benchmark_slug }}</router-link>
              <span v-else-if="s.module_name">{{ s.module_name }}</span>
              <span v-else class="text-gray-400">—</span>
            </td>
            <td class="px-4 py-3 text-sm text-gray-500">
              {{ s.benchmark_id || '—' }}
            </td>
            <td class="px-4 py-3 text-sm text-gray-700 font-mono text-xs">{{ s.endpoint_model }}</td>
            <td class="px-4 py-3 text-sm">
              <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium" :class="statusClass(s.status)">
                {{ $t(`common.status.${s.status}`) }}
              </span>
            </td>
            <td class="px-4 py-3 text-sm font-medium">
              <span :class="s.score_total !== null ? 'text-indigo-600' : 'text-gray-400'">
                {{ s.score_total !== null ? s.score_total.toFixed(4) : '—' }}
              </span>
            </td>
            <td class="px-4 py-3 text-sm text-gray-500">{{ formatDate(s.created_at) }}</td>
            <td class="px-4 py-3 text-sm">
              <router-link :to="`/submissions/${s.id}`" class="text-indigo-600 hover:text-indigo-500">
                View →
              </router-link>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useSubmissionsStore } from '@/stores/submissions'

const store = useSubmissionsStore()

onMounted(() => store.fetchMine())

function statusClass(status: string) {
  switch (status) {
    case 'done': return 'bg-green-100 text-green-800'
    case 'failed': return 'bg-red-100 text-red-800'
    case 'running': return 'bg-blue-100 text-blue-800'
    case 'queued': return 'bg-gray-100 text-gray-800'
    case 'canceled': return 'bg-gray-200 text-gray-600'
    default: return 'bg-gray-100 text-gray-800'
  }
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}
</script>
