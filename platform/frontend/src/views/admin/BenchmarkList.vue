<template>
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-4 flex items-center justify-between">
      <h1 class="text-2xl font-bold text-gray-900">{{ $t('adminBench.title') }}</h1>
      <div v-show="tab === 'benchmarks'" class="flex items-center gap-3">
        <label
          class="inline-flex items-center px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 cursor-pointer"
          :title="$t('adminBench.importTip')"
        >
          {{ $t('adminBench.importYaml') }}
          <input type="file" accept=".yaml,.yml" class="hidden" @change="handleImport" />
        </label>
        <router-link
          to="/admin/benchmarks/new"
          class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
        >
          {{ $t('adminBench.newBenchmark') }}
        </router-link>
      </div>
    </div>

    <!-- Tabs: the table edits benchmarks one at a time; the group manager
         edits the grouping across all of them. -->
    <div class="mb-6 border-b border-gray-200">
      <nav class="-mb-px flex gap-6">
        <button
          v-for="key in (['benchmarks', 'groups'] as const)"
          :key="key"
          type="button"
          class="py-2 px-1 border-b-2 text-sm font-medium"
          :class="tab === key
            ? 'border-indigo-500 text-indigo-600'
            : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'"
          @click="tab = key"
        >{{ key === 'groups' ? $t('adminBench.tabGroups') : $t('adminBench.tabBenchmarks') }}</button>
      </nav>
    </div>

    <div v-if="importSuccess" class="mb-4 bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded text-sm">
      {{ importSuccess }}
    </div>
    <div v-if="tab === 'benchmarks' && loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div v-else-if="tab === 'benchmarks' && error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">{{ error }}</div>

    <div v-else-if="tab === 'benchmarks'" class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
      <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">ID</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{{ $t('adminBench.name') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Slug</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{{ $t('subDetail.status') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{{ $t('subDetail.modules') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{{ $t('adminBench.groups') }}</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{{ $t('adminBench.actions') }}</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-200">
          <tr v-for="b in benchmarks" :key="b.id" class="hover:bg-gray-50">
            <td class="px-4 py-3 text-sm text-gray-400 font-mono">{{ b.id }}</td>
            <td class="px-4 py-3 text-sm font-medium text-gray-900">{{ b.name }}</td>
            <td class="px-4 py-3 text-sm text-gray-500 font-mono">{{ b.slug }}</td>
            <td class="px-4 py-3">
              <select
                :value="b.status"
                @change="updateStatus(b.id, ($event.target as HTMLSelectElement).value)"
                class="text-xs rounded border-gray-300"
              >
                <option value="draft">{{ $t('adminBench.draft') }}</option>
                <option value="active">{{ $t('benchList.active') }}</option>
                <option value="archived">{{ $t('adminBench.archived') }}</option>
              </select>
            </td>
            <td class="px-4 py-3 text-sm text-gray-500">{{ b.modules?.length || 0 }}</td>
            <td class="px-4 py-3">
              <div v-if="b.group_tags?.length" class="flex flex-wrap gap-1 max-w-xs">
                <span
                  v-for="tag in b.group_tags"
                  :key="tag"
                  class="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] bg-indigo-50 text-indigo-700"
                >{{ tag }}</span>
              </div>
              <span v-else class="text-xs text-gray-400">{{ $t('benchList.notGrouped') }}</span>
            </td>
            <td class="px-4 py-3 text-sm flex items-center gap-3">
              <router-link :to="`/admin/benchmarks/${b.slug}`" class="text-indigo-600 hover:text-indigo-500">
                {{ $t('adminBench.edit') }}
              </router-link>
              <router-link
                :to="`/admin/benchmarks/new?from=${b.slug}`"
                class="text-gray-500 hover:text-gray-700"
                :title="$t('adminBench.cloneTip')"
              >
                {{ $t('adminBench.clone') }}
              </router-link>
              <button @click="exportBenchmark(b.id, b.slug)" class="text-gray-500 hover:text-gray-700">
                {{ $t('adminBench.exportYaml') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Mounted on first visit and then kept alive, so switching back to the
         table never throws away an unsaved grouping draft. -->
    <BenchmarkGroupManager
      v-if="groupsMounted"
      v-show="tab === 'groups'"
      @saved="fetchBenchmarks"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import BenchmarkGroupManager from '@/components/BenchmarkGroupManager.vue'
import { benchmarksApi } from '@/api/client'
import type { Benchmark } from '@/api/client'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()

// ?tab=groups keeps the choice across a reload and makes it linkable. The
// query-only replace stays on the same route record, so the group manager's
// unsaved-changes guard doesn't fire when the tabs are switched.
type Tab = 'benchmarks' | 'groups'
const tab = ref<Tab>(route.query.tab === 'groups' ? 'groups' : 'benchmarks')
const groupsMounted = ref(tab.value === 'groups')

watch(tab, (value) => {
  if (value === 'groups') groupsMounted.value = true
  router.replace({ query: { ...route.query, tab: value === 'groups' ? 'groups' : undefined } })
})

const benchmarks = ref<Benchmark[]>([])
const loading = ref(false)
const error = ref('')
const importSuccess = ref('')

onMounted(fetchBenchmarks)

async function fetchBenchmarks() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await benchmarksApi.adminList()
    benchmarks.value = data.benchmarks
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('adminBench.loadFailed')
  } finally {
    loading.value = false
  }
}

async function updateStatus(id: number, newStatus: string) {
  try {
    await benchmarksApi.update(id, { status: newStatus } as any)
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('adminBench.updateFailed')
  }
}

async function exportBenchmark(id: number, slug: string) {
  try {
    const { data } = await benchmarksApi.exportYaml(id)
    const url = URL.createObjectURL(new Blob([data], { type: 'text/yaml' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${slug}.yaml`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    error.value = t('adminBench.exportFailed')
  }
}

async function handleImport(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  error.value = ''
  importSuccess.value = ''
  try {
    const { data } = await benchmarksApi.importYaml(file)
    importSuccess.value = t('adminBench.importOk', { name: data.name, slug: data.slug })
    await fetchBenchmarks()
    router.push(`/admin/benchmarks/${data.slug}`)
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('adminBench.importFailed')
  } finally {
    // reset so the same file can be re-uploaded after an error
    ;(event.target as HTMLInputElement).value = ''
  }
}
</script>
