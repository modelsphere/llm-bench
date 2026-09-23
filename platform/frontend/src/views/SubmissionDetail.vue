<template>
  <div class="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6">
      <router-link v-if="isOwner" to="/submissions" class="text-sm text-gray-500 hover:text-gray-700">← {{ $t('nav.mySubmissions') }}</router-link>
      <router-link v-else-if="submission?.benchmark_slug" :to="`/benchmarks/${submission.benchmark_slug}`" class="text-sm text-gray-500 hover:text-gray-700">← {{ submission.benchmark_name }}</router-link>
      <router-link v-else to="/benchmarks" class="text-sm text-gray-500 hover:text-gray-700">← {{ $t('nav.benchmarks') }}</router-link>
    </div>

    <div v-if="store.loading && !submission" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>

    <div v-else-if="submission">
      <!-- Header -->
      <div class="bg-white rounded-lg border border-gray-200 shadow-sm p-6 mb-6">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h1 class="text-xl font-bold text-gray-900">{{ $t('subDetail.title', { id: submission.id }) }}</h1>
            <p class="text-sm text-gray-500 mt-1">
              <span v-if="!isOwner" class="text-gray-700 font-medium">{{ submission.endpoint_model }}</span>
              <span v-else>{{ submission.endpoint_model }}</span>
              <span
                class="ml-2 inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold align-middle"
                :class="submission.card_type ? 'bg-indigo-50 text-indigo-700 ring-1 ring-indigo-200' : 'bg-gray-100 text-gray-400 ring-1 ring-gray-200'"
                :title="$t('submit.cardType')"
              >{{ submission.card_type || $t('subDetail.unknownCard') }}</span>
              <span v-if="submission.benchmark_slug" class="mx-1">·</span>
              <router-link
                v-if="submission.benchmark_slug"
                :to="`/benchmarks/${submission.benchmark_slug}`"
                class="text-indigo-600 hover:text-indigo-800"
              >
                {{ submission.benchmark_name }}
              </router-link>
            </p>
            <p v-if="submission.description_summary" class="text-sm text-gray-700 mt-1 italic">
              “{{ submission.description_summary }}”
            </p>
          </div>
          <div class="flex items-center space-x-3">
            <span class="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium" :class="statusClass(submission.status)">
              {{ $t(`common.status.${submission.status}`) }}
            </span>
            <template v-if="(isOwner || isAdmin) && (submission.status === 'queued' || submission.status === 'running')">
              <button @click="handleCancel" :disabled="canceling" class="text-sm text-red-600 hover:text-red-800 disabled:opacity-50">
                {{ canceling ? $t('subDetail.canceling') : $t('common.cancel') }}
              </button>
              <span v-if="cancelError" class="text-xs text-red-500">{{ cancelError }}</span>
            </template>
            <template v-if="isOwner || isAdmin">
              <button @click="handleDownloadLogs" :disabled="downloadingLogs" class="text-sm text-indigo-600 hover:text-indigo-800 disabled:opacity-50">
                {{ downloadingLogs ? $t('subDetail.preparing') : (submission.status === 'running' || submission.status === 'queued' ? $t('subDetail.downloadLogsLive') : $t('subDetail.downloadLogs')) }}
              </button>
              <span v-if="logsError" class="text-xs text-red-500">{{ logsError }}</span>
            </template>
          </div>
        </div>

        <div class="grid grid-cols-3 gap-4">
          <MetricCard :label="$t('subDetail.totalScore')" :value="submission.score_total" :passed="submission.status === 'done' || submission.status === 'failed' ? submission.passed : null" />
          <MetricCard :label="$t('subDetail.benchmark')" :value="submission.benchmark_id ? `#${submission.benchmark_id}` : submission.module_name || '—'" :passed="null" />
          <div class="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
            <div class="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
              <div>
                <p class="text-sm font-medium text-gray-500">{{ $t('subDetail.submitted') }}</p>
                <p class="mt-2 text-sm font-semibold text-gray-900 whitespace-nowrap">{{ formatDate(submission.created_at) }}</p>
              </div>
              <div v-if="submission.finished_at" class="text-right">
                <p class="text-sm font-medium text-gray-500">{{ $t(`subDetail.ended.${endedLabelKey}`) }}</p>
                <p class="mt-2 text-sm font-semibold text-gray-900 whitespace-nowrap" :title="formatDate(submission.finished_at)">{{ formatEndDate(submission.finished_at, submission.created_at) }}</p>
              </div>
            </div>
          </div>
        </div>

        <!-- Overall progress bar -->
        <div v-if="submission.status === 'running' || submission.status === 'queued'" class="mt-4">
          <div class="flex justify-between text-xs text-gray-600 mb-1">
            <span>{{ $t('subDetail.overallProgress') }}</span>
            <span>{{ Math.round(overallProgress * 100) }}%</span>
          </div>
          <div class="w-full bg-gray-200 rounded-full h-2.5">
            <div class="bg-blue-600 h-2.5 rounded-full transition-all duration-500" :style="{ width: `${overallProgress * 100}%` }"></div>
          </div>
        </div>

        <div v-if="liveEvents.length > 0 && submission.status !== 'done'" class="mt-3 flex items-center gap-2 text-xs text-blue-600">
          <span class="inline-block w-2 h-2 bg-blue-500 rounded-full animate-pulse"></span>
          {{ $t('subDetail.liveEvents', { n: liveEvents.length }) }}
        </div>

        <div v-if="submission.error" class="mt-4 bg-red-50 border border-red-200 rounded-md p-3">
          <p class="text-sm text-red-700">{{ submission.error }}</p>
        </div>

        <div v-if="hardwareSummary" class="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <span class="text-gray-500">{{ $t('submit.hardware') }}:</span>
          <span class="text-gray-900">{{ hardwareSummary }}</span>
        </div>

        <!-- Who submitted it and, when the submitting system supplied one, a link
             back to the entry that produced it (an autotune Baseline Hub page, a
             CI job, …). The URL is opaque to us — the backend has already refused
             anything that is not http(s), which is what makes binding it to an
             href safe; the label is ours, so a hostile URL has nothing to say. -->
        <div
          v-if="submission.contributor || submission.source_url"
          class="mt-4 flex flex-wrap items-center gap-2 text-sm"
        >
          <template v-if="submission.contributor">
            <span class="text-gray-500">{{ $t('submit.contributor') }}:</span>
            <span class="text-gray-900">{{ submission.contributor }}</span>
          </template>
          <template v-if="submission.source_url">
            <span class="text-gray-500">{{ $t('subDetail.source') }}:</span>
            <a
              :href="submission.source_url"
              target="_blank"
              rel="noopener noreferrer"
              class="text-indigo-600 hover:text-indigo-800 hover:underline break-all"
              :title="submission.source_url"
            >{{ submission.source_url }}</a>
          </template>
        </div>
      </div>

      <!-- Submitter-authored description (markdown) -->
      <div v-if="submission.description_detail" class="bg-white rounded-lg border border-gray-200 shadow-sm p-6 mb-6">
        <h2 class="text-lg font-medium text-gray-900 mb-3">{{ $t('subDetail.description') }}</h2>
        <MarkdownView :source="submission.description_detail" />
      </div>

      <!-- Charts -->
      <div v-if="showCharts" class="bg-white rounded-lg border border-gray-200 shadow-sm p-6 mb-6">
        <h2 class="text-lg font-medium text-gray-900 mb-4">{{ $t('subDetail.results') }}</h2>

        <!-- Per-module pass/fail glance (hover a mark for the module name) -->
        <div class="flex flex-wrap items-center gap-1.5 mb-6">
          <span class="text-xs font-medium text-gray-400 mr-1">{{ $t('subDetail.modules') }}</span>
          <span
            v-for="run in runsWithMetrics"
            :key="run.id"
            class="inline-flex items-center justify-center min-w-[1.5rem] h-6 px-1.5 rounded text-xs font-bold cursor-default select-none"
            :class="[passFailClass(run.passed), { 'opacity-40': run.weight === 0 }]"
            :title="passFailTitle(run)"
          >
            {{ run.status === 'skipped' ? '⊘' : run.passed === true ? '✓' : run.passed === false ? '✗' : '–' }}
          </span>
        </div>

        <SubmissionCharts :runs="runsWithMetrics" :overall-score="submission.score_total" />
      </div>

      <!-- Per-module runs -->
      <div v-if="submission.runs.length > 0" class="space-y-4">
        <div v-for="run in runsWithMetrics" :key="run.id" class="bg-white rounded-lg border border-gray-200 shadow-sm">
          <!-- Run header -->
          <div class="flex items-center justify-between px-4 py-3 border-b border-gray-100">
            <div class="flex items-center gap-3">
              <h3 class="text-sm font-semibold text-gray-900" :class="{ 'opacity-60': run.weight === 0 }">{{ run.label ?? run.module_name }}</h3>
              <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium" :class="statusClass(run.status)">
                {{ $t(`common.status.${run.status}`) }}
              </span>
            </div>
            <div class="flex items-center gap-4">
              <div class="flex-1 w-48">
                <div class="w-full bg-gray-100 rounded-full h-1.5">
                  <div
                    class="h-1.5 rounded-full transition-all duration-500"
                    :class="run.status === 'done' ? 'bg-green-500' : run.status === 'running' ? 'bg-blue-500' : 'bg-gray-300'"
                    :style="{ width: `${Math.round(runBarFraction(run) * 100)}%` }"
                  ></div>
                </div>
                <div class="text-[10px] text-gray-400 mt-0.5 truncate">{{ runBarMessage(run) }}</div>
              </div>
              <MetricCard :label="$t('subDetail.score')" :value="run.score" :passed="run.passed" class="min-w-[110px]" />
              <div
                v-if="run.started_at"
                @click="toggleDurationUnit(run.id)"
                class="cursor-pointer select-none"
                :title="$t('subDetail.durationToggleTip')"
              >
                <MetricCard
                  :label="$t('subDetail.duration')"
                  :value="durationValue(run)"
                  format="integer"
                  :sublabel="durationsAsSeconds[run.id] ? $t('subDetail.seconds') : $t('subDetail.elapsed')"
                  class="min-w-[110px] whitespace-nowrap"
                />
              </div>
            </div>
          </div>

          <!-- Concurrency override notice — shown only on modules the override affected -->
          <div v-if="run.concurrency_override" class="px-4 py-2 text-xs text-indigo-700 bg-indigo-50 border-b border-indigo-100 flex items-start gap-1.5">
            <span aria-hidden="true">⚡</span>
            <span>
              <span class="font-semibold">{{ $t('subDetail.ccOverridden') }}</span>{{ $t('subDetail.ccRanAt') }}<span class="font-mono font-semibold">{{ run.concurrency_override.effective }}</span>{{ $t('subDetail.ccConcurrent') }}<span
                v-if="run.concurrency_override.original != null && run.concurrency_override.original !== run.concurrency_override.effective"
              > ({{ $t('subDetail.ccConfigured') }}<span class="font-mono">{{ run.concurrency_override.original }}</span>)</span><span
                v-if="run.concurrency_override.effective !== run.concurrency_override.requested"
              > · {{ $t('subDetail.ccRequested') }}<span class="font-mono">{{ run.concurrency_override.requested }}</span>{{ $t('subDetail.ccClamped') }}</span>
            </span>
          </div>

          <div v-if="run.status === 'skipped'" class="px-4 py-2 text-sm text-amber-700 bg-amber-50 border-b border-amber-100">{{ run.error || $t('subDetail.skippedDefault') }}</div>
          <div v-else-if="run.error" class="px-4 py-2 text-sm text-red-600 bg-red-50 border-b border-red-100">{{ run.error }}</div>

          <!-- Score breakdown (when metric_configs available) -->
          <div v-if="run.metrics_json && run.metric_configs_json?.length" class="p-4 space-y-4">
            <!-- Score terms -->
            <div v-if="scoreTerms(run).length">
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">{{ $t('subDetail.scoreBreakdown') }}</p>
              <table class="w-full text-xs border-collapse">
                <thead>
                  <tr class="bg-blue-50 text-left text-gray-600">
                    <th class="px-3 py-2 border border-blue-100 font-medium">{{ $t('subDetail.metric') }}</th>
                    <th class="px-3 py-2 border border-blue-100 font-medium">{{ $t('subDetail.value') }}</th>
                    <th class="px-3 py-2 border border-blue-100 font-medium">{{ $t('subDetail.formula') }}</th>
                    <th class="px-3 py-2 border border-blue-100 font-medium text-right">{{ $t('subDetail.termScore') }}</th>
                    <th class="px-3 py-2 border border-blue-100 font-medium text-right">{{ $t('subDetail.weight') }}</th>
                    <th class="px-3 py-2 border border-blue-100 font-medium text-right">{{ $t('subDetail.contribution') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="t in scoreTerms(run)" :key="t.key" class="hover:bg-blue-50/40">
                    <td class="px-3 py-2 border border-gray-200 font-medium text-gray-700">
                      {{ t.displayName }}<HelpTip v-if="metricTip(t.key)" class="ml-1">{{ metricTip(t.key) }}</HelpTip>
                    </td>
                    <td class="px-3 py-2 border border-gray-200 text-gray-600 font-mono">
                      {{ t.value !== null ? formatVal(t.value, t.unit) : '—' }}
                    </td>
                    <td class="px-3 py-2 border border-gray-200 text-gray-500 font-mono text-[11px]">{{ t.formulaStr }}</td>
                    <td class="px-3 py-2 border border-gray-200 text-right font-mono"
                        :class="t.termScore !== null ? (t.termScore >= 0.9 ? 'text-green-700' : t.termScore >= 0.5 ? 'text-yellow-700' : 'text-red-600') : 'text-gray-400'">
                      {{ t.termScore !== null ? t.termScore.toFixed(3) : '—' }}
                    </td>
                    <td class="px-3 py-2 border border-gray-200 text-right text-gray-500">
                      {{ t.weightFraction !== null ? (t.weightFraction * 100).toFixed(0) + '%' : '—' }}
                    </td>
                    <td class="px-3 py-2 border border-gray-200 text-right font-semibold"
                        :class="t.contribution !== null ? 'text-blue-700' : 'text-gray-400'">
                      {{ t.contribution !== null ? t.contribution.toFixed(4) : '—' }}
                    </td>
                  </tr>
                  <tr class="bg-blue-50 font-semibold">
                    <td class="px-3 py-2 border border-blue-100 text-right text-gray-600" colspan="5">{{ $t('subDetail.moduleScore') }}</td>
                    <td class="px-3 py-2 border border-blue-100 text-right text-blue-800">
                      {{ run.score !== null ? Number(run.score).toFixed(4) : '—' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- Redlines -->
            <div v-if="redlineTerms(run).length">
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">{{ $t('subDetail.redlineChecks') }}</p>
              <table class="w-full text-xs border-collapse">
                <thead>
                  <tr class="bg-orange-50 text-left text-gray-600">
                    <th class="px-3 py-2 border border-orange-100 font-medium">{{ $t('subDetail.metric') }}</th>
                    <th class="px-3 py-2 border border-orange-100 font-medium">{{ $t('subDetail.value') }}</th>
                    <th class="px-3 py-2 border border-orange-100 font-medium">{{ $t('subDetail.threshold') }}</th>
                    <th class="px-3 py-2 border border-orange-100 font-medium text-center">{{ $t('subDetail.status') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in redlineTerms(run)" :key="r.key + r.side" class="hover:bg-orange-50/40">
                    <td class="px-3 py-2 border border-gray-200 font-medium text-gray-700">
                      {{ r.displayName }}<HelpTip v-if="metricTip(r.key)" class="ml-1">{{ metricTip(r.key) }}</HelpTip>
                    </td>
                    <td class="px-3 py-2 border border-gray-200 font-mono text-gray-600">
                      {{ r.value !== null ? formatVal(r.value, r.unit) : '—' }}
                    </td>
                    <td class="px-3 py-2 border border-gray-200 font-mono text-gray-500">{{ r.thresholdStr }}</td>
                    <td class="px-3 py-2 border border-gray-200 text-center">
                      <span v-if="r.value === null" class="text-gray-400">N/A</span>
                      <span v-else-if="r.ok" class="text-green-600 font-semibold">✓ {{ $t('common.passed') }}</span>
                      <span v-else class="text-red-600 font-semibold">✗ {{ $t('common.notPassed') }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- Display-only metrics -->
            <div v-if="displayTerms(run).length">
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">{{ $t('subDetail.informational') }}</p>

              <!-- Grouped sub-sections for info-heavy modules (e.g. replay) -->
              <div v-if="displayGroups(run)" class="space-y-3">
                <div v-for="g in displayGroups(run)" :key="g.label">
                  <p class="text-[11px] font-medium text-gray-400 uppercase tracking-wide mb-1">
                    {{ groupLabel(g.label) }} <span class="text-gray-300 font-normal">({{ g.terms.length }})</span>
                  </p>
                  <div class="flex flex-wrap gap-2">
                    <div
                      v-for="d in g.terms" :key="d.key"
                      class="bg-gray-50 rounded px-3 py-1.5 text-xs"
                    >
                      <span class="text-gray-500">{{ d.displayName }}<HelpTip v-if="metricTip(d.key)" class="mx-0.5">{{ metricTip(d.key) }}</HelpTip>:</span>
                      <span class="ml-1 font-mono font-medium text-gray-700">{{ d.value !== null ? formatVal(d.value, d.unit) : '—' }}</span>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Flat list for lean modules -->
              <div v-else class="flex flex-wrap gap-2">
                <div
                  v-for="d in displayTerms(run)" :key="d.key"
                  class="bg-gray-50 rounded px-3 py-1.5 text-xs"
                >
                  <span class="text-gray-500">{{ d.displayName }}<HelpTip v-if="metricTip(d.key)" class="mx-0.5">{{ metricTip(d.key) }}</HelpTip>:</span>
                  <span class="ml-1 font-mono font-medium text-gray-700">{{ d.value !== null ? formatVal(d.value, d.unit) : '—' }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- Fallback: flat metric display when no metric_configs -->
          <div v-else-if="run.metrics_json" class="px-4 py-3 flex flex-wrap gap-2">
            <div
              v-for="(val, key) in flattenMetrics(run.metrics_json)"
              :key="key"
              class="bg-gray-50 rounded px-3 py-1.5 text-xs truncate"
            >
              <span class="text-gray-500">{{ key }}<HelpTip v-if="metricTip(String(key))" class="mx-0.5">{{ metricTip(String(key)) }}</HelpTip>:</span>
              <span class="ml-1 font-medium text-gray-700">{{ formatMetric(String(key), val) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useSubmissionsStore } from '@/stores/submissions'
import { useAuthStore } from '@/stores/auth'
import HelpTip from '@/components/HelpTip.vue'
import MetricCard from '@/components/MetricCard.vue'
import SubmissionCharts from '@/components/SubmissionCharts.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import { benchmarksApi, submissionsApi } from '@/api/client'
import { disambiguateNames } from '@/utils/modules'
import { groupMetricTerms, GROUPING_MIN } from '@/utils/metricGroups'
import type { SubmissionRun, MetricConfig } from '@/api/client'

const { t, te } = useI18n()
const route = useRoute()
const store = useSubmissionsStore()
const auth = useAuthStore()
const canceling = ref(false)
const cancelError = ref('')
const downloadingLogs = ref(false)
const logsError = ref('')
const liveEvents = ref<Record<string, any>[]>([])
// Keyed by SubmissionRun.id, NOT module_name: a benchmark may contain the same
// module more than once, and keying by name would make every duplicate share
// (and display) the first run's progress bar.
const moduleProgress = ref<Record<number, { fraction: number; message: string }>>({})
// module_name -> weight, pulled from the benchmark definition so the score
// composition chart reflects the real Σ(score×weight)/Σweight makeup.
const moduleWeights = ref<Record<string, number>>({})
// Keyed by SubmissionRun.id: true while that run's duration is shown as a raw
// second count instead of the h/m/s breakdown. Clicking the card toggles it.
const durationsAsSeconds = ref<Record<number, boolean>>({})

const submission = computed(() => store.currentSubmission)
const id = computed(() => Number(route.params.id))
const isOwner = computed(() => submission.value?.user_id === auth.user?.id)
// includes super_admin (store.isAdmin treats super_admin as admin-or-above)
const isAdmin = computed(() => auth.isAdmin)

const overallProgress = computed(() => {
  if (!submission.value) return 0
  if (submission.value.status === 'done') return 1
  if (submission.value.status === 'failed' || submission.value.status === 'canceled') return 0
  const runs = submission.value.runs
  if (runs.length === 0) return submission.value.status === 'queued' ? 0.02 : 0.05
  let total = 0
  for (const run of runs) {
    const prog = moduleProgress.value[run.id]
    // done/failed/skipped are all terminal — count them as fully complete so a
    // failed-then-skipped tail doesn't peg the overall bar below 100%.
    if (run.status === 'done' || run.status === 'failed' || run.status === 'skipped') total += 1
    else if (prog) total += prog.fraction
    else if (run.status === 'running') total += 0.05
  }
  return total / runs.length
})

const sseUrl = computed(() => {
  const token = localStorage.getItem('token')
  return `${import.meta.env.VITE_API_URL || '/api'}/submissions/${id.value}/logs${token ? `?token=${token}` : ''}`
})

const runsWithMetrics = computed(() => {
  const runs = submission.value?.runs
  if (!runs) return []
  // Disambiguate duplicate module names (e.g. two functional_acceptance runs)
  // so the run list, pass/fail glance, and charts all label them distinctly.
  const labels = disambiguateNames(runs.map(r => r.module_name))
  return runs.map((run, i) => {
    const weight = moduleWeights.value[run.module_name] ?? 1
    const label = labels[i]
    const ev = liveEvents.value.find(e => e.run_id === run.id)
    if (ev && ev.metrics) {
      return {
        ...run,
        weight,
        label,
        score: ev.score ?? run.score,
        metrics_json: ev.metrics,
        metric_configs_json: ev.metric_configs ?? run.metric_configs_json,
      }
    }
    return { ...run, weight, label }
  })
})

const showCharts = computed(() => submission.value?.status === 'done' && submission.value.runs.length > 0)

// Human-readable one-liner of the hardware section, or '' when none was set.
const hardwareSummary = computed(() => {
  const s = submission.value
  if (!s) return ''
  const parts: string[] = []
  // card_type is shown as a badge next to the model id, so it is deliberately
  // left out here rather than repeated.
  if (s.cards_per_machine != null) parts.push(`${s.cards_per_machine} card(s)/machine`)
  if (s.machine_count != null) parts.push(`${s.machine_count} machine(s)`)
  return parts.join(' · ')
})

// Which label the finished-at timestamp carries: a submission stamps
// finished_at on every terminal outcome, not just a clean finish.
const endedLabelKey = computed(() => {
  const status = submission.value?.status
  if (status === 'failed') return 'failed'
  if (status === 'canceled') return 'canceled'
  return 'finished'
})

let eventSource: EventSource | null = null

function connectSSE() {
  disconnectSSE()
  try {
    eventSource = new EventSource(sseUrl.value)
    eventSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.event === 'connected') return
        if (data.event === 'run_complete') liveEvents.value.push(data)
        if (data.event === 'progress' && data.run_id != null) {
          moduleProgress.value[data.run_id] = { fraction: data.fraction, message: data.message }
        }
        if (data.event === 'done') store.refresh(id.value)
      } catch { /* ignore */ }
    }
    eventSource.onerror = () => disconnectSSE()
  } catch { /* EventSource not supported */ }
}

function disconnectSSE() {
  eventSource?.close()
  eventSource = null
}

watch(id, (newId) => {
  if (!newId) return
  liveEvents.value = []
  moduleProgress.value = {}
  moduleWeights.value = {}
  store.startPolling(newId, 3000)
  connectSSE()
}, { immediate: true })

// Pull module weights from the benchmark definition once it's known, so the
// score composition chart shows real weighted contributions (fallback: 1).
watch(() => submission.value?.benchmark_slug, async (slug) => {
  if (!slug) return
  try {
    const { data } = await benchmarksApi.get(slug)
    const map: Record<string, number> = {}
    for (const mod of data.modules ?? []) map[mod.module_name] = mod.weight ?? 1
    moduleWeights.value = map
  } catch { /* weights are optional — composition falls back to equal weights */ }
}, { immediate: true })

onUnmounted(() => {
  store.stopPolling()
  disconnectSSE()
})

async function handleCancel() {
  canceling.value = true
  cancelError.value = ''
  try {
    await store.cancel(id.value)
  } catch (e: any) {
    cancelError.value = e.response?.data?.detail || t('subDetail.cancelFailed')
  } finally {
    canceling.value = false
  }
}

async function handleDownloadLogs() {
  downloadingLogs.value = true
  logsError.value = ''
  try {
    const res = await submissionsApi.downloadLogs(id.value)
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `submission-${id.value}.log`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    // With responseType:'blob', an error body arrives as a Blob — read it for the detail.
    let detail = e?.response?.data?.detail
    if (e?.response?.data instanceof Blob) {
      try { detail = JSON.parse(await e.response.data.text())?.detail } catch { /* keep fallback */ }
    }
    logsError.value = detail || t('subDetail.logsFailed')
  } finally {
    downloadingLogs.value = false
  }
}

// ---------------------------------------------------------------------------
// Score breakdown helpers (mirror evaluator.py logic in TypeScript)
// ---------------------------------------------------------------------------

function applyFormula(val: number, mc: MetricConfig): number {
  const formula = mc.formula ?? 'ratio'
  const baseline = mc.baseline ?? 1
  if (formula === 'ratio') return val / baseline
  if (formula === 'ratio_capped') return Math.min(val / baseline, 1)
  if (formula === 'inverse_ratio_capped') return val !== 0 ? Math.min(baseline / val, 1) : 0
  if (formula === 'linear') {
    const zeroAt = mc.zero_at ?? 0
    const oneAt = mc.one_at ?? 1
    const span = oneAt - zeroAt
    if (span === 0) return 0
    return Math.max(0, Math.min(1, (val - zeroAt) / span))
  }
  if (formula === 'passthrough') return val
  if (formula === 'passthrough_scaled') return val  // contribution is weight*val; computed in scoreTerms
  return val
}

function formulaStr(mc: MetricConfig): string {
  const f = mc.formula ?? 'ratio'
  if (f === 'ratio') return `÷ ${mc.baseline ?? '?'}`
  if (f === 'ratio_capped') return `min(÷${mc.baseline ?? '?'}, 1)`
  if (f === 'inverse_ratio_capped') return `min(${mc.baseline ?? '?'}÷val, 1)`
  if (f === 'linear') return `linear [${mc.zero_at}→${mc.one_at}]`
  if (f === 'passthrough') return 'raw value'
  if (f === 'passthrough_scaled') return `${mc.weight ?? 1} × val (unnormalized)`
  return f
}

/**
 * Resolve a MetricConfig key against a metrics dict. Exact flat key wins;
 * otherwise a dotted key walks nested dicts — e.g. "c8.ttft_p99_ms" reads
 * metrics.c8.ttft_p99_ms (perf_guidellm_sweep's per-concurrency-level shape).
 * Mirrors bench/modules/evaluator.py:resolve_metric — keep the two in sync.
 */
function resolveMetric(metrics: Record<string, any>, key: string): any {
  if (key in metrics) return metrics[key]
  if (!key.includes('.')) return undefined
  let cur: any = metrics
  for (const part of key.split('.')) {
    if (cur === null || typeof cur !== 'object' || !(part in cur)) return undefined
    cur = cur[part]
  }
  return cur
}

function scoreTerms(run: SubmissionRun & { metric_configs_json?: MetricConfig[] | null }) {
  const configs = run.metric_configs_json ?? []
  const metrics = run.metrics_json ?? {}
  const terms = configs.filter(mc => mc.role === 'score')
  // Mirror compute_score: passthrough_scaled is not in the normalization denominator,
  // contributes weight*value directly, and skips clip.
  const normTerms = terms.filter(mc => mc.formula !== 'passthrough_scaled')
  const totalWeight = normTerms.reduce((s, mc) => s + (mc.weight ?? 1), 0) || 1

  return terms.map(mc => {
    const raw = resolveMetric(metrics, mc.key)
    const value = raw !== undefined && raw !== null && raw !== '' ? Number(raw) : null
    const isScaled = mc.formula === 'passthrough_scaled'
    let termScore = value !== null ? applyFormula(value, mc) : null
    if (termScore !== null && !isScaled) {
      if (mc.clip_low !== null && mc.clip_low !== undefined) termScore = Math.max(termScore, mc.clip_low)
      if (mc.clip_high !== null && mc.clip_high !== undefined) termScore = Math.min(termScore, mc.clip_high)
    }
    const weight = mc.weight ?? 1
    const weightFraction = isScaled ? null : weight / totalWeight
    const contribution = value === null
      ? null
      : isScaled
        ? weight * value
        : (termScore !== null ? termScore * (weight / totalWeight) : null)
    return {
      key: mc.key,
      displayName: mc.key,
      unit: '',
      value,
      formulaStr: formulaStr(mc),
      termScore,
      weight,
      weightFraction,
      contribution,
    }
  })
}

interface RedlineTerm {
  key: string
  displayName: string
  unit: string
  value: number | null
  thresholdStr: string
  side: string
  ok: boolean
}

function redlineTerms(run: SubmissionRun & { metric_configs_json?: MetricConfig[] | null }): RedlineTerm[] {
  const configs = run.metric_configs_json ?? []
  const metrics = run.metrics_json ?? {}
  const rows: RedlineTerm[] = []
  for (const mc of configs.filter(c => c.role === 'redline')) {
    const raw = resolveMetric(metrics, mc.key)
    const value = raw !== undefined && raw !== null ? Number(raw) : null
    if (mc.min_val !== null && mc.min_val !== undefined) {
      rows.push({
        key: mc.key,
        displayName: mc.key,
        unit: '',
        value,
        thresholdStr: `≥ ${mc.min_val}`,
        side: 'min',
        ok: value !== null ? value >= mc.min_val : false,
      })
    }
    if (mc.max_val !== null && mc.max_val !== undefined) {
      rows.push({
        key: mc.key,
        displayName: mc.key,
        unit: '',
        value,
        thresholdStr: `≤ ${mc.max_val}`,
        side: 'max',
        ok: value !== null ? value <= mc.max_val : false,
      })
    }
  }
  return rows
}


// Tooltip text explaining a metric, from the `metricTips.*` i18n namespace
// (both locale files). Empty string = no tooltip; the HelpTip is only rendered
// for keys that have an entry, so tips roll out metric-by-metric. Per-level
// keys (`*_c{N}`, perf_guidellm_sweep) share their base key's tip.
function metricTip(key: string): string {
  for (const k of [`metricTips.${key}`, `metricTips.${key.replace(/_c\d+$/, '')}`]) {
    if (te(k)) return t(k)
  }
  return ''
}

// Group labels are i18n keys from metricGroups.ts; dynamic per-concurrency
// groups arrive as "concurrency:<n>".
function groupLabel(label: string): string {
  const m = /^concurrency:(\d+)$/.exec(label)
  if (m) return t('metricGroups.concurrency', { n: m[1] })
  return t(`metricGroups.${label}`)
}

/**
 * Coerce a display metric for rendering, WITHOUT forcing it to a number.
 *
 * Not every display metric is numeric — replay reports which dataset a run
 * replayed (`dataset_id`, `dataset_sha256`) as strings. A blanket Number()
 * turns those into NaN, and because `typeof NaN === 'number'` they then sail
 * past any type guard downstream and render as the literal "NaN".
 *
 * Numeric strings still become numbers, so unit-aware formatting (ms, %, tok/s)
 * is unaffected for everything that really is a number.
 */
function asDisplayValue(raw: any): number | string | null {
  if (raw === undefined || raw === null || raw === '') return null
  if (typeof raw === 'number') return raw
  if (typeof raw === 'boolean') return String(raw)
  const text = String(raw)
  const n = Number(text)
  // Only take the numeric reading when it round-trips exactly. A sha prefix
  // that happens to be all digits parses as a number but can exceed 2^53, and
  // would then render with silently mangled digits — an identity value that is
  // subtly wrong is worse than one that is obviously text.
  return Number.isFinite(n) && String(n) === text.trim() ? n : text
}

function displayTerms(run: SubmissionRun & { metric_configs_json?: MetricConfig[] | null }) {
  const configs = run.metric_configs_json ?? []
  const metrics = run.metrics_json ?? {}
  return configs
    .filter(mc => mc.role === 'display')
    .map(mc => {
      const raw = resolveMetric(metrics, mc.key)
      return {
        key: mc.key,
        displayName: mc.key,
        unit: '',
        value: asDisplayValue(raw),
      }
    })
}

// Frontend-only sub-grouping of the informational chips. Returns labelled groups
// for info-heavy modules (≥ GROUPING_MIN metrics, e.g. replay) so they don't
// flood one flat list; returns null for lean modules, which render flat as
// before. Backend metrics stay flat — this is display sugar only.
function displayGroups(run: SubmissionRun & { metric_configs_json?: MetricConfig[] | null }) {
  const terms = displayTerms(run)
  if (terms.length < GROUPING_MIN) return null
  return groupMetricTerms(terms)
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

function formatVal(val: number | string | null, unit: string): string {
  // Not every metric is a number: identity metrics (e.g. the replay dataset's
  // build id / hash) are strings. `isFinite` alone is not enough of a guard —
  // it COERCES, so an all-digit string like "1234567" passes it and then blows
  // up on .toFixed(). Check the type first.
  if (typeof val !== 'number') return val === null || val === undefined ? '—' : String(val)
  if (!isFinite(val)) return String(val)
  if (unit === '%') return `${(val * 100).toFixed(1)}%`
  if (unit === 'ms') return `${val >= 1000 ? val.toFixed(0) : val.toFixed(1)} ms`
  if (unit === 'tok/s' || unit === 'chars/s') return val.toFixed(1)
  if (Number.isInteger(val) || Math.abs(val) >= 100) return val.toFixed(0)
  return val.toFixed(3)
}

function passFailClass(passed: boolean | null): string {
  if (passed === true) return 'bg-green-100 text-green-700'
  if (passed === false) return 'bg-red-100 text-red-700'
  return 'bg-gray-100 text-gray-400'
}

function passFailTitle(run: { label?: string; module_name: string; status?: string; passed: boolean | null; score: number | null; weight?: number }): string {
  const name = run.label ?? run.module_name
  const verdict = run.status === 'skipped'
    ? t('subDetail.vSkipped')
    : run.passed === true ? t('common.passed') : run.passed === false ? t('common.notPassed') : t('subDetail.vNone')
  const score = run.score != null ? ` · score ${Number(run.score).toFixed(3)}` : ''
  const w0 = run.weight === 0 ? ` · ${t('subDetail.vWeight0')}` : ''
  return `${name}: ${verdict}${score}${w0}`
}

function statusClass(status: string) {
  switch (status) {
    case 'done': return 'bg-green-100 text-green-800'
    case 'failed': return 'bg-red-100 text-red-800'
    case 'running': return 'bg-blue-100 text-blue-800'
    case 'queued': return 'bg-gray-100 text-gray-800'
    case 'canceled': return 'bg-gray-200 text-gray-600'
    case 'skipped': return 'bg-amber-50 text-amber-700'
    default: return 'bg-gray-100 text-gray-800'
  }
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}

// A submission almost always ends on the day it was submitted, and repeating
// the date twice in one card only crowds it — so drop it when it matches, and
// keep the full stamp (title tooltip aside) when the run crossed midnight.
function formatEndDate(iso: string, submittedIso: string) {
  const end = new Date(iso)
  if (end.toDateString() === new Date(submittedIso).toDateString()) {
    return end.toLocaleTimeString()
  }
  return end.toLocaleString()
}

function runDuration(run: SubmissionRun): number | null {
  if (!run.started_at || !run.finished_at) return null
  const d = Math.round((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000)
  return d >= 0 ? d : null
}

function toggleDurationUnit(runId: number) {
  durationsAsSeconds.value[runId] = !durationsAsSeconds.value[runId]
}

// Raw seconds while toggled, otherwise an h/m/s breakdown — module runs can go
// for hours, and a bare second count stops being readable well before that.
function durationValue(run: SubmissionRun): string | number | null {
  const secs = runDuration(run)
  if (secs === null) return null
  return durationsAsSeconds.value[run.id] ? secs : formatDuration(secs)
}

function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = totalSeconds % 60
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function runBarFraction(run: SubmissionRun): number {
  if (run.status === 'done' || run.status === 'skipped') return 1
  const prog = moduleProgress.value[run.id]
  if (prog) return prog.fraction
  if (run.status === 'running') return 0.05
  return 0
}

function runBarMessage(run: SubmissionRun): string {
  if (run.status === 'done') return t('common.status.done')
  if (run.status === 'failed') return run.error || t('common.status.failed')
  if (run.status === 'skipped') return t('common.status.skipped')
  const prog = moduleProgress.value[run.id]
  if (prog) return prog.message
  if (run.status === 'running') return t('common.status.running')
  return t('common.status.pending')
}

function flattenMetrics(metrics: Record<string, any>, prefix = ''): Record<string, any> {
  const result: Record<string, any> = {}
  for (const [k, v] of Object.entries(metrics)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      Object.assign(result, flattenMetrics(v, key))
    } else {
      result[key] = v
    }
  }
  return result
}

function formatMetric(key: string, val: any): string {
  if (typeof val === 'number') {
    if ((key.includes('latency') || key.includes('duration') || key.includes('time') || key.includes('ttft') || key.includes('tpot') || key.includes('itl')) && val > 10)
      return `${val.toFixed(0)} ms`
    if (key.includes('tps') || key.includes('cps') || key.includes('throughput')) return val.toFixed(1)
    if (val >= 0 && val <= 1 && (key.includes('rate') || key.includes('ratio') || key.includes('uptime') || key.includes('accuracy')))
      return `${(val * 100).toFixed(1)}%`
    return val.toFixed(2)
  }
  if (Array.isArray(val)) return `[${val.length} items]`
  if (typeof val === 'object' && val !== null) return '[object]'
  return String(val)
}
</script>
