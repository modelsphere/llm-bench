<template>
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6 flex items-start justify-between">
      <div>
        <h1 class="text-2xl font-bold text-gray-900">{{ $t('nav.allSubmissions') }}</h1>
        <p class="text-sm text-gray-500 mt-1">
          {{ $t('adminSubs.subtitle') }}
        </p>
      </div>
      <button
        @click="reload"
        :disabled="loading"
        class="text-sm px-3 py-1.5 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
      >
        {{ loading ? $t('common.loading') : $t('adminSubs.refresh') }}
      </button>
    </div>

    <div v-if="loading && rows.length === 0" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div v-else-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">{{ error }}</div>
    <div v-else-if="rows.length === 0" class="text-center py-12 text-gray-500">{{ $t('mySubs.empty') }}</div>

    <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
      <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">#</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('lb.user') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('adminSubs.benchOrModule') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('mySubs.model') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.status') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.score') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">{{ $t('subDetail.submitted') }}</th>
            <th class="px-4 py-3"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-200">
          <tr
            v-for="s in rows"
            :key="s.id"
            class="hover:bg-gray-50 cursor-pointer"
            @click="open(s.id)"
          >
            <td class="px-4 py-3 text-sm text-gray-500">#{{ s.id }}</td>
            <td class="px-4 py-3 text-sm text-gray-700">{{ s.username || `user ${s.user_id}` }}</td>
            <td class="px-4 py-3 text-sm text-gray-700 font-medium">
              <span v-if="s.benchmark_slug">{{ s.benchmark_slug }}</span>
              <span v-else-if="s.module_name">{{ s.module_name }}</span>
              <span v-else class="text-gray-400">—</span>
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
            <td class="px-4 py-3 text-sm text-gray-500 whitespace-nowrap">{{ formatDate(s.created_at) }}</td>
            <td class="px-4 py-3 text-sm text-right whitespace-nowrap">
              <router-link :to="`/submissions/${s.id}`" class="text-indigo-600 hover:text-indigo-500" @click.stop>
                {{ $t('lb.view') }}
              </router-link>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- Pagination -->
      <div class="flex items-center justify-between px-4 py-3 border-t border-gray-200 bg-gray-50 text-sm text-gray-600">
        <div>{{ $t('adminSubs.range', { start: rangeStart, end: rangeEnd, total }) }}</div>
        <div class="flex items-center gap-3">
          <button
            @click="prevPage"
            :disabled="offset === 0 || loading"
            class="px-3 py-1 rounded-md border border-gray-300 bg-white hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
          >{{ $t('adminSubs.prev') }}</button>
          <span>{{ $t('adminSubs.page', { page, totalPages }) }}</span>
          <button
            @click="nextPage"
            :disabled="rangeEnd >= total || loading"
            class="px-3 py-1 rounded-md border border-gray-300 bg-white hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
          >{{ $t('adminSubs.next') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { submissionsApi, type Submission } from '@/api/client'

// Deliberately NO live polling / SSE here — this is a high-level admin list that
// could show many rows. It fetches once per page; the per-submission detail page
// is where live status is streamed. Showing only summary status avoids opening a
// stream per row.
const { t } = useI18n()
const router = useRouter()
const PAGE_SIZE = 50

const rows = ref<Submission[]>([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)

const page = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const rangeStart = computed(() => (total.value === 0 ? 0 : offset.value + 1))
const rangeEnd = computed(() => Math.min(offset.value + PAGE_SIZE, total.value))

async function load() {
  loading.value = true
  error.value = null
  try {
    const { data } = await submissionsApi.listAll({ limit: PAGE_SIZE, offset: offset.value })
    rows.value = data.items
    total.value = data.total
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('adminSubs.loadFailed')
  } finally {
    loading.value = false
  }
}

function reload() {
  offset.value = 0
  load()
}
function nextPage() {
  if (rangeEnd.value < total.value) {
    offset.value += PAGE_SIZE
    load()
  }
}
function prevPage() {
  if (offset.value > 0) {
    offset.value = Math.max(0, offset.value - PAGE_SIZE)
    load()
  }
}
function open(id: number) {
  router.push(`/submissions/${id}`)
}

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

onMounted(load)
</script>
