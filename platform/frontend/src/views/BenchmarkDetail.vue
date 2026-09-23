<template>
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div v-if="store.loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div v-else-if="!benchmark" class="text-center py-12 text-gray-500">{{ $t('benchDetail.notFound') }}</div>

    <div v-else>
      <!-- Header -->
      <div class="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-gray-900">
            {{ benchmark.name }}
            <span class="ml-1 text-lg font-normal text-gray-400" :title="$t('benchList.idTip')">#{{ benchmark.id }}</span>
          </h1>
          <p class="mt-1 text-sm text-gray-500">{{ benchmark.description }}</p>
          <div class="mt-2 flex items-center gap-3 flex-wrap">
            <div class="flex items-center gap-1.5">
              <span class="text-xs text-gray-400">{{ $t('benchDetail.config') }}</span>
              <code
                class="font-mono text-xs px-2 py-0.5 bg-gray-100 text-gray-700 rounded border border-gray-200"
                :title="$t('benchDetail.configTip')"
              >{{ benchmark.config_hash ?? 'n/a' }}</code>
            </div>
            <span class="text-gray-200 select-none">|</span>
            <span class="text-xs text-gray-400">v{{ benchmark.version }}</span>
            <span
              v-if="benchmark.is_locked"
              class="text-xs px-1.5 py-0.5 bg-blue-50 text-blue-600 border border-blue-200 rounded"
              :title="$t('benchDetail.lockedTip')"
            >{{ $t('benchDetail.locked') }}</span>
          </div>
        </div>
        <router-link
          :to="`/benchmarks/${benchmark.slug}/submit`"
          class="shrink-0 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
        >
          {{ $t('benchDetail.submitYourModel') }}
        </router-link>
      </div>

      <!-- Scoring & Metrics panel -->
      <div class="mb-6 border border-gray-200 rounded-lg overflow-hidden">
        <!-- Top bar -->
        <button
          class="w-full flex items-center justify-between px-4 py-3 bg-gray-50 hover:bg-gray-100 text-left"
          @click="scoringOpen = !scoringOpen"
        >
          <span class="text-sm font-semibold text-gray-700">{{ $t('benchDetail.scoringMetrics') }}</span>
          <span class="text-gray-400 text-xs">{{ scoringOpen ? '▲' : '▼' }}</span>
        </button>

        <div v-if="scoringOpen">
          <!-- Overall formula -->
          <div class="px-4 py-3 bg-white border-b border-gray-100 flex items-baseline gap-3">
            <span class="text-xs font-medium text-gray-500 shrink-0">{{ $t('benchDetail.overall') }}</span>
            <code class="text-xs font-mono text-indigo-700">
              score_total = Σ (module_score × weight) / Σ weight
            </code>
          </div>

          <!-- Per-module collapsible rows -->
          <div
            v-for="{ mod, label, key } in moduleRows"
            :key="key"
            class="border-t border-gray-100"
          >
            <!-- Module header (clickable) -->
            <button
              class="w-full flex items-center gap-3 px-4 py-2 bg-white hover:bg-gray-50 text-left"
              @click="toggleModule(key)"
            >
              <span class="text-xs font-mono font-medium text-gray-800">{{ label }}</span>
              <span class="text-xs text-gray-400">{{ localizedModuleName(mod.module_name, mod.display_name) }}</span>
              <span class="ml-auto flex items-center gap-3">
                <span
                  class="text-xs font-medium"
                  :class="mod.weight === 0 ? 'text-gray-400' : 'text-indigo-600'"
                  :title="mod.weight === 0 ? $t('benchDetail.weight0Tip') : ''"
                >{{ $t('subDetail.weight') }} {{ (mod.weight * 100).toFixed(0) }}%</span>
                <span class="text-gray-300 text-xs">{{ openModules[key] ? '▲' : '▼' }}</span>
              </span>
            </button>

            <!-- Module body -->
            <div v-if="openModules[key]" class="px-4 pb-4 bg-white">
              <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-2">

                <!-- Left: Score formula + redlines + metric table -->
                <div class="space-y-3">
                  <!-- Score terms -->
                  <div v-if="scoreTerms(mod).length">
                    <p class="text-xs font-medium text-gray-500 mb-1">{{ $t('benchDetail.scoreFormula') }}</p>
                    <div class="text-xs font-mono text-indigo-800 bg-indigo-50 rounded px-3 py-2 space-y-0.5">
                      <div v-for="t in scoreTerms(mod)" :key="t.key">
                        {{ (t.weightFraction * 100).toFixed(0) }}% × {{ t.displayName }} [{{ t.formulaStr }}]
                      </div>
                    </div>
                  </div>

                  <!-- Redline thresholds -->
                  <div v-if="redlineTerms(mod).length">
                    <p class="text-xs font-medium text-gray-500 mb-1">{{ $t('benchDetail.redlines') }}</p>
                    <div class="text-xs font-mono text-emerald-800 bg-emerald-50 rounded px-3 py-2 space-y-0.5">
                      <div v-for="r in redlineTerms(mod)" :key="r.key + r.side">
                        {{ r.displayName }} {{ r.side === 'min' ? '≥' : '≤' }} {{ formatThreshold(r.threshold, r.unit) }}
                      </div>
                    </div>
                  </div>

                  <!-- Metrics table (display-only rows) -->
                  <div v-if="displayTerms(mod).length">
                    <p class="text-xs font-medium text-gray-500 mb-1">{{ $t('benchDetail.infoMetrics') }}</p>

                    <!-- Grouped sub-sections for info-heavy modules (e.g. replay) -->
                    <div v-if="displayGroups(mod)" class="space-y-2">
                      <div v-for="g in displayGroups(mod)" :key="g.label">
                        <p class="text-[10px] font-medium text-gray-400 uppercase tracking-wide mb-0.5">
                          {{ groupLabel(g.label) }} <span class="text-gray-300 font-normal">({{ g.terms.length }})</span>
                        </p>
                        <div class="flex flex-wrap gap-1">
                          <span v-for="d in g.terms" :key="d.key"
                            class="text-xs text-gray-500 bg-gray-100 rounded px-2 py-0.5"
                            :title="d.description">
                            {{ d.displayName }}<span v-if="d.unit" class="text-gray-400"> ({{ d.unit }})</span>
                          </span>
                        </div>
                      </div>
                    </div>

                    <!-- Flat list for lean modules -->
                    <div v-else class="flex flex-wrap gap-1">
                      <span v-for="d in displayTerms(mod)" :key="d.key"
                        class="text-xs text-gray-500 bg-gray-100 rounded px-2 py-0.5"
                        :title="d.description">
                        {{ d.displayName }}<span v-if="d.unit" class="text-gray-400"> ({{ d.unit }})</span>
                      </span>
                    </div>
                  </div>
                </div>

                <!-- Right: Params (read-only) -->
                <div v-if="mod.params_json && Object.keys(mod.params_json).length">
                  <p class="text-xs font-medium text-gray-500 mb-1">{{ $t('benchDetail.params') }}</p>
                  <div class="grid grid-cols-2 gap-x-4 gap-y-0.5">
                    <div v-for="(val, key) in mod.params_json" :key="key" class="contents">
                      <span class="text-xs font-mono text-gray-500 truncate" :title="paramDescription(mod, key)">{{ key }}</span>
                      <span class="text-xs font-mono text-gray-900 text-right truncate" :title="String(val)">{{ formatParamValue(val) }}</span>
                    </div>
                  </div>
                </div>

              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Leaderboard -->
      <div class="bg-white rounded-lg border border-gray-200 shadow-sm">
        <div class="px-4 py-3 border-b border-gray-200 flex items-center justify-between gap-4 flex-wrap">
          <div>
            <h2 class="text-lg font-medium text-gray-900">{{ $t('benchDetail.leaderboard') }}</h2>
            <p class="text-sm text-gray-500">
              {{ $t('benchDetail.rowCount', { shown: filteredRows.length, total: leaderboard?.total_submissions || 0 }) }}
            </p>
          </div>
          <div v-if="leaderboard" class="flex items-center gap-4">
            <label class="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer select-none">
              <input type="checkbox" v-model="showFailed" class="rounded border-gray-300 text-indigo-600" />
              {{ $t('benchDetail.showFailed') }}
            </label>
            <label class="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer select-none">
              <input type="checkbox" v-model="showOutdated" class="rounded border-gray-300 text-indigo-600" />
              {{ $t('benchDetail.showOutdated') }}
            </label>
          </div>
        </div>
        <div v-if="lbLoading" class="p-8 text-center text-gray-500">{{ $t('common.loading') }}</div>
        <div v-else-if="leaderboardError" class="p-4 text-sm text-red-600">{{ leaderboardError }}</div>
        <LeaderboardTable
          v-else-if="leaderboard"
          :rows="filteredRows"
          :modules="leaderboard.modules"
          :module-names="leaderboard.module_names"
          :current-config-hash="leaderboard.current_config_hash"
        />
        <div v-else class="p-8 text-center text-sm text-gray-500">
          {{ $t('benchDetail.noSubmissions') }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useBenchmarksStore } from '@/stores/benchmarks'
import { leaderboardApi } from '@/api/client'
import { disambiguateNames, localizedModuleName } from '@/utils/modules'
import { groupMetricTerms, GROUPING_MIN } from '@/utils/metricGroups'
import LeaderboardTable from '@/components/LeaderboardTable.vue'
import type { Leaderboard, BenchmarkModule, MetricConfig } from '@/api/client'

const { t } = useI18n()
const route = useRoute()
const store = useBenchmarksStore()

const slug = computed(() => route.params.slug as string)
const benchmark = computed(() => store.currentBenchmark)

// Modules paired with a unique key (index) and a disambiguated label, so
// duplicate module names get a #1/#2 suffix and each row's open/closed state
// is tracked independently (keying on module_name alone collides on dupes).
const moduleRows = computed(() => {
  const mods = benchmark.value?.modules ?? []
  const labels = disambiguateNames(mods.map((m) => m.module_name))
  return mods.map((mod, i) => ({ mod, label: labels[i], key: i }))
})

const leaderboard = ref<Leaderboard | null>(null)
const lbLoading = ref(false)
const leaderboardError = ref('')
const scoringOpen = ref(true)
const openModules = reactive<Record<string, boolean>>({})
// Both filters ON by default — hiding rows surprised users more than showing them.
const showFailed = ref(true)
const showOutdated = ref(true)

const filteredRows = computed(() => {
  if (!leaderboard.value) return []
  return leaderboard.value.rows.filter(row => {
    if (!showFailed.value && row.passed === false) return false
    if (!showOutdated.value && leaderboard.value!.current_config_hash && row.config_hash !== leaderboard.value!.current_config_hash) return false
    return true
  })
})

onMounted(async () => {
  await store.fetchBenchmark(slug.value)
  // Default: first module open, rest closed (keyed by row index, not name,
  // so duplicate module names don't share toggle state).
  benchmark.value?.modules?.forEach((_mod: any, i: number) => {
    openModules[i] = i === 0
  })
  await loadLeaderboard()
})

function toggleModule(key: number) {
  openModules[key] = !openModules[key]
}

async function loadLeaderboard() {
  lbLoading.value = true
  leaderboardError.value = ''
  try {
    const { data } = await leaderboardApi.get(slug.value)
    leaderboard.value = data
  } catch (e: any) {
    leaderboardError.value = e.response?.data?.detail || t('benchDetail.lbFailed')
  } finally {
    lbLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// Score breakdown helpers for benchmark definition display
// ---------------------------------------------------------------------------

function _descriptor(mod: BenchmarkModule, key: string) {
  return mod.metrics_schema?.metrics_descriptors?.find(d => d.name === key)
}

function _displayName(mod: BenchmarkModule, key: string): string {
  return _descriptor(mod, key)?.display_name || key
}

function _unit(mod: BenchmarkModule, key: string): string {
  return _descriptor(mod, key)?.unit || ''
}

function _formulaStr(mc: MetricConfig): string {
  const f = mc.formula ?? 'ratio'
  if (f === 'ratio') return `÷ ${mc.baseline ?? '?'}`
  if (f === 'ratio_capped') return `min(÷${mc.baseline ?? '?'}, 1)`
  if (f === 'inverse_ratio_capped') return `min(${mc.baseline ?? '?'}÷val, 1)`
  if (f === 'linear') return `linear [${mc.zero_at}→${mc.one_at}]`
  if (f === 'passthrough') return 'raw value'
  if (f === 'passthrough_scaled') return `${mc.weight ?? 1} × val (unnormalized)`
  return f
}

function _configs(mod: BenchmarkModule): MetricConfig[] {
  const c = mod.metric_configs
  if (c && c.length > 0) return c
  return mod.metrics_schema?.default_metric_configs ?? []
}

function scoreTerms(mod: BenchmarkModule) {
  const configs = _configs(mod)
  const terms = configs.filter(mc => mc.role === 'score')
  const totalWeight = terms.reduce((s, mc) => s + (mc.weight ?? 1), 0) || 1
  return terms.map(mc => ({
    key: mc.key,
    displayName: _displayName(mod, mc.key),
    formulaStr: _formulaStr(mc),
    weightFraction: (mc.weight ?? 1) / totalWeight,
  }))
}

function redlineTerms(mod: BenchmarkModule) {
  const configs = _configs(mod)
  const rows: { key: string; side: 'min' | 'max'; displayName: string; threshold: number; unit: string }[] = []
  for (const mc of configs.filter(c => c.role === 'redline')) {
    const unit = _unit(mod, mc.key)
    const name = _displayName(mod, mc.key)
    if (mc.min_val !== null && mc.min_val !== undefined)
      rows.push({ key: mc.key, side: 'min', displayName: name, threshold: mc.min_val, unit })
    if (mc.max_val !== null && mc.max_val !== undefined)
      rows.push({ key: mc.key, side: 'max', displayName: name, threshold: mc.max_val, unit })
  }
  return rows
}


// Group labels are i18n keys from metricGroups.ts; dynamic per-concurrency
// groups arrive as "concurrency:<n>".
function groupLabel(label: string): string {
  const m = /^concurrency:(\d+)$/.exec(label)
  if (m) return t('metricGroups.concurrency', { n: m[1] })
  return t(`metricGroups.${label}`)
}

function displayTerms(mod: BenchmarkModule) {
  const configs = _configs(mod)
  return configs
    .filter(mc => mc.role === 'display')
    .map(mc => ({
      key: mc.key,
      displayName: _displayName(mod, mc.key),
      unit: _unit(mod, mc.key),
      description: _descriptor(mod, mc.key)?.description || '',
    }))
}

// Frontend-only sub-grouping of the informational metric chips, shared with the
// submission view. Groups when a module is info-heavy (≥ GROUPING_MIN display
// metrics, e.g. replay); returns null for lean modules, which render flat.
function displayGroups(mod: BenchmarkModule) {
  const terms = displayTerms(mod)
  if (terms.length < GROUPING_MIN) return null
  return groupMetricTerms(terms)
}

function formatThreshold(value: unknown, unit: string): string {
  if (value === undefined || value === null) return '?'
  const num = Number(value)
  if (unit === '%') return `${(num * 100).toFixed(0)}%`
  if (unit === 'ms' && num >= 1000) return `${(num / 1000).toFixed(0)} s`
  return `${num}${unit ? ' ' + unit : ''}`
}

function formatParamValue(val: unknown): string {
  if (Array.isArray(val)) return val.join(', ')
  if (val === null || val === undefined) return '—'
  if (typeof val === 'number') return String(val)
  return String(val)
}

function paramDescription(mod: any, key: string): string {
  return mod.params_schema?.properties?.[key]?.description ?? key
}
</script>
