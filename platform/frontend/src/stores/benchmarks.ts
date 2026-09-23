import { defineStore } from 'pinia'
import { i18n } from '@/i18n'
import { ref } from 'vue'
import { benchmarksApi, modulesApi, type Benchmark, type ModuleDescriptor } from '@/api/client'

export const useBenchmarksStore = defineStore('benchmarks', () => {
  const benchmarks = ref<Benchmark[]>([])
  const activeBenchmarks = ref<Benchmark[]>([])
  const currentBenchmark = ref<Benchmark | null>(null)
  const modules = ref<ModuleDescriptor[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchBenchmarks() {
    loading.value = true
    error.value = null
    try {
      const { data } = await benchmarksApi.list()
      benchmarks.value = data.benchmarks
      activeBenchmarks.value = data.benchmarks.filter((b: Benchmark) => b.status === 'active')
    } catch (e: any) {
      error.value = e.response?.data?.detail || i18n.global.t('storeErrors.benchmarks')
    } finally {
      loading.value = false
    }
  }

  async function fetchBenchmark(slug: string) {
    loading.value = true
    error.value = null
    try {
      const { data } = await benchmarksApi.get(slug)
      currentBenchmark.value = data
      return data
    } catch (e: any) {
      error.value = e.response?.data?.detail || i18n.global.t('storeErrors.benchmark')
      return null
    } finally {
      loading.value = false
    }
  }

  async function fetchModules() {
    try {
      const { data } = await modulesApi.list()
      modules.value = data.modules
    } catch (e) {
      console.error('Failed to load modules', e)
    }
  }

  return { benchmarks, activeBenchmarks, currentBenchmark, modules, loading, error, fetchBenchmarks, fetchBenchmark, fetchModules }
})
