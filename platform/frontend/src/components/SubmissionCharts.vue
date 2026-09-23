<template>
  <div v-if="runs.length === 0" class="text-sm text-gray-400">{{ $t('charts.noData') }}</div>
  <div v-else class="space-y-8">
    <!-- Score composition: how the overall score is made up (weighted shares) -->
    <div v-if="hasComposition">
      <h3 class="text-sm font-medium text-gray-700 mb-2">{{ $t('charts.scoreComposition') }}</h3>
      <p class="text-xs text-gray-400 mb-2">
        Each slice is a module's weighted contribution
        (score × weight) to the overall score. Hover for details.
      </p>
      <Doughnut :data="scoreData" :options="scoreOptions" :plugins="[centerTextPlugin]" class="max-h-72" />
    </div>
    <div v-else class="text-sm text-gray-400">
      No scored modules yet — nothing to compose.
    </div>

    <!-- Latency: one collapsible section per module; each metric family
         (TTFT / TPOT / ITL) gets its own chart so scales don't collide. -->
    <div v-if="latencyCharts.length">
      <h3 class="text-sm font-medium text-gray-700 mb-2">{{ $t('charts.latencyByModule') }}</h3>
      <div class="space-y-2">
        <div v-for="c in latencyCharts" :key="c.module" class="border border-gray-200 rounded-lg overflow-hidden">
          <button
            type="button"
            class="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-gray-50"
            @click="toggle(c.module)"
          >
            <span class="text-sm font-medium text-gray-700 shrink-0">{{ c.module }}</span>
            <span class="flex items-center gap-3 ml-auto text-xs text-gray-500">
              <span v-for="s in c.summary" :key="s.label" class="whitespace-nowrap">
                <span class="text-gray-400">{{ s.label }}</span> {{ s.text }}
              </span>
              <svg
                class="w-4 h-4 shrink-0 text-gray-400 transition-transform"
                :class="expanded[c.module] ? 'rotate-180' : ''"
                viewBox="0 0 20 20" fill="currentColor"
              >
                <path fill-rule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.06l3.71-3.83a.75.75 0 111.08 1.04l-4.25 4.39a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z" clip-rule="evenodd" />
              </svg>
            </span>
          </button>
          <div
            v-if="expanded[c.module]"
            class="px-3 pb-3 pt-1 grid grid-cols-1 gap-4"
            :class="gridClass(c.families.length)"
          >
            <div v-for="fam in c.families" :key="fam.key">
              <h4 class="text-xs font-medium text-gray-500 mb-1 text-center">
                {{ fam.label }} <span class="font-normal text-gray-400">(ms)</span>
              </h4>
              <Bar :data="fam.data" :options="famOptions" :plugins="[dataLabelPlugin]" class="max-h-48" />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Bar, Doughnut } from 'vue-chartjs'

ChartJS.register(CategoryScale, LinearScale, BarElement, ArcElement, Title, Tooltip, Legend)

interface RunData {
  module_name: string
  // Disambiguated display label (e.g. "functional_acceptance #1"); falls back
  // to module_name when not provided.
  label?: string
  score: number | null
  passed: boolean | null
  metrics_json: Record<string, any> | null
  weight?: number
}

const props = defineProps<{
  runs: RunData[]
  /** Overall submission score (Σ score×weight / Σ weight), drawn in the doughnut center. */
  overallScore?: number | null
}>()

/* ------------------------------------------------------------------ */
/*  Shared base options                                               */
/* ------------------------------------------------------------------ */
const baseOptions = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: {
    legend: { position: 'bottom' as const },
  },
}

/* Distinct categorical palette for module slices. */
const PALETTE = [
  '#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#06b6d4',
  '#a855f7', '#ec4899', '#14b8a6', '#f97316', '#64748b',
]

/* ------------------------------------------------------------------ */
/*  Score composition (doughnut of weighted contributions)            */
/* ------------------------------------------------------------------ */
const contributions = computed(() =>
  props.runs.map(r => Math.max(0, r.score ?? 0) * (r.weight ?? 1))
)
const totalContribution = computed(() =>
  contributions.value.reduce((s, v) => s + v, 0)
)
const hasComposition = computed(() => totalContribution.value > 0)

const scoreData = computed(() => ({
  labels: props.runs.map(r => r.label ?? r.module_name),
  datasets: [{
    data: contributions.value,
    backgroundColor: props.runs.map((_, i) => PALETTE[i % PALETTE.length]),
    borderColor: '#fff',
    borderWidth: 1,
  }],
}))

/* Inline plugin: overall score in the doughnut hole. */
const centerTextPlugin = {
  id: 'centerText',
  afterDraw(chart: any) {
    const txt = chart.options?.plugins?.centerText?.text
    if (!txt) return
    const { ctx, chartArea } = chart
    if (!chartArea) return
    const cx = (chartArea.left + chartArea.right) / 2
    const cy = (chartArea.top + chartArea.bottom) / 2
    ctx.save()
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillStyle = '#374151'
    ctx.font = 'bold 22px sans-serif'
    ctx.fillText(txt, cx, cy - 7)
    ctx.font = '11px sans-serif'
    ctx.fillStyle = '#9ca3af'
    ctx.fillText('overall', cx, cy + 15)
    ctx.restore()
  },
}

const scoreOptions = computed(() => ({
  ...baseOptions,
  cutout: '62%',
  plugins: {
    ...baseOptions.plugins,
    centerText: {
      text: props.overallScore != null ? props.overallScore.toFixed(3) : '',
    },
    tooltip: {
      callbacks: {
        label: (ctx: any) => {
          const r = props.runs[ctx.dataIndex]
          const share = totalContribution.value > 0
            ? (ctx.parsed / totalContribution.value) * 100 : 0
          const pass = r.passed === true ? ' ✓' : r.passed === false ? ' ✗' : ''
          const score = r.score != null ? r.score.toFixed(3) : 'n/a'
          const w = r.weight ?? 1
          return ` ${r.label ?? r.module_name}: ${share.toFixed(0)}% of total (score ${score} × w${w})${pass}`
        },
      },
    },
  },
}))

/* ------------------------------------------------------------------ */
/*  Latency — one chart per module, linear scale, only present series */
/* ------------------------------------------------------------------ */
const FAMILIES = [
  { key: 'ttft', label: 'TTFT' },
  { key: 'tpot', label: 'TPOT' },
  { key: 'itl', label: 'ITL' },
]
/* Aggregations a module may report. Colours stay consistent across modules. */
const AGGS = [
  { key: 'p50',  label: 'P50',  color: 'rgba(59,130,246,0.45)' },
  { key: 'p99',  label: 'P99',  color: 'rgba(59,130,246,0.9)' },
  { key: 'mean', label: 'Mean', color: 'rgba(245,158,11,0.85)' },
]
const metricKey = (fam: string, agg: string) => `${fam}_${agg}_ms`

/* Numeric labels on top of bars. */
const dataLabelPlugin = {
  id: 'dataLabels',
  afterDatasetsDraw(chart: any) {
    const { ctx } = chart
    ctx.save()
    ctx.font = 'bold 10px sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'bottom'
    chart.data.datasets.forEach((dataset: any, datasetIndex: number) => {
      const meta = chart.getDatasetMeta(datasetIndex)
      if (meta.hidden) return
      meta.data.forEach((bar: any, index: number) => {
        const value = dataset.data[index]
        if (value === null || value === undefined) return
        const color = dataset.backgroundColor
        ctx.fillStyle = typeof color === 'string' ? color : '#374151'
        const n = Number(value)
        const label = n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n))
        ctx.fillText(label, bar.x, bar.y - 2)
      })
    })
    ctx.restore()
  },
}

/* Collapsed/expanded state per module (collapsed by default). */
const expanded = reactive<Record<string, boolean>>({})
const toggle = (m: string) => { expanded[m] = !expanded[m] }

/* Compact latency formatter: seconds once we're past 1s, else ms. */
function fmtLatency(v: number): string {
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`
}

/* Static grid-cols classes (Tailwind JIT needs literal class names). */
const gridClass = (n: number) =>
  n >= 3 ? 'sm:grid-cols-3' : n === 2 ? 'sm:grid-cols-2' : 'sm:grid-cols-1'

const latencyCharts = computed(() => {
  const charts: {
    module: string
    summary: { label: string; text: string }[]
    families: { key: string; label: string; data: any }[]
  }[] = []

  for (const r of props.runs) {
    const m = r.metrics_json
    if (!m) continue
    // Families this module reports at least one aggregation for.
    const fams = FAMILIES.filter(f =>
      AGGS.some(a => typeof m[metricKey(f.key, a.key)] === 'number')
    )
    if (!fams.length) continue

    // One chart per family — separate charts keep each metric on its own
    // scale (TTFT in seconds vs TPOT/ITL in ms would otherwise flatten out).
    const families = fams.map(f => {
      const present = AGGS.filter(a => typeof m[metricKey(f.key, a.key)] === 'number')
      return {
        key: f.key,
        label: f.label,
        data: {
          labels: present.map(a => a.label),
          datasets: [{
            label: f.label,
            data: present.map(a => m[metricKey(f.key, a.key)] as number),
            backgroundColor: present.map(a => a.color),
            borderRadius: 3,
            borderSkipped: false,
          }],
        },
      }
    })

    // Folded summary: prefer the mean, else median, else p99 — labelled when
    // it isn't the mean so the glance stays honest.
    const summary = fams.map(f => {
      const pick = ['mean', 'p50', 'p99']
        .map(k => ({ k, v: m[metricKey(f.key, k)] }))
        .find(x => typeof x.v === 'number') as { k: string; v: number } | undefined
      return {
        label: f.label,
        text: pick ? fmtLatency(pick.v) + (pick.k !== 'mean' ? ` (${pick.k})` : '') : 'n/a',
      }
    })

    charts.push({ module: r.label ?? r.module_name, summary, families })
  }
  return charts
})

const famOptions = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: {
    legend: { display: false },
    tooltip: {
      callbacks: {
        label: (ctx: any) => {
          const v = ctx.parsed.y
          return ` ${ctx.label}: ${v !== null && v !== undefined ? v.toLocaleString() + ' ms' : 'N/A'}`
        },
      },
    },
  },
  scales: {
    y: { beginAtZero: true, title: { display: true, text: 'ms' } },
    x: { ticks: { maxRotation: 0 } },
  },
}
</script>
