<template>
  <div class="rounded-md border border-gray-200 bg-white p-3 text-xs text-gray-600 space-y-3">
    <!-- Verdict -->
    <div class="flex flex-wrap items-start gap-2">
      <span class="text-gray-400">{{ $t('loadReading.title', { hours: reading.window_hours ?? 168 }) }}</span>
      <span :class="badgeClass" class="rounded px-1.5 py-0.5 font-semibold uppercase tracking-wide text-[10px]">
        {{ $t(`loadReading.cat.${reading.category}`) }}
      </span>
      <HelpTip>{{ $t('loadReading.help') }}</HelpTip>
    </div>
    <p class="text-sm text-gray-800 leading-snug">{{ sentence }}</p>

    <!-- Where this backend sits on the idle–moderate–busy scale -->
    <div v-if="reading.w_log !== undefined && reading.status !== 'failed'" class="max-w-md">
      <div class="relative h-3 flex rounded overflow-hidden">
        <div class="bg-sky-100" :style="{ width: pct(idleBelow) }"></div>
        <div class="bg-sky-300" :style="{ width: pct(busyFrom - idleBelow) }"></div>
        <div class="bg-sky-600 flex-1"></div>
        <div
          class="absolute -top-1 h-5 w-0.5 bg-gray-900"
          :class="{ 'opacity-40': reading.category === 'unclear' }"
          :style="{ left: `calc(${pct(Math.min(1, reading.w_log))} - 1px)` }"
          :title="`w_log ${(reading.w_log * 100).toFixed(0)}%`"
        ></div>
      </div>
      <div class="relative h-4 text-[10px] text-gray-500">
        <span class="absolute left-0">{{ $t('loadReading.cat.idle') }}</span>
        <span class="absolute" :style="{ left: pct((idleBelow + busyFrom) / 2), transform: 'translateX(-50%)' }">{{ $t('loadReading.cat.moderate') }}</span>
        <span class="absolute right-0">{{ $t('loadReading.cat.busy') }}</span>
      </div>
    </div>

    <!-- The curve it was read from: the same request, slower as more run at once -->
    <div v-if="groups.length >= 2" class="overflow-x-auto">
      <svg :viewBox="`0 0 ${W} ${H}`" class="w-full max-w-xl" :class="{ 'opacity-50': reading.category === 'unclear' }" role="img" :aria-label="$t('loadReading.chartAria')">
        <!-- top panel: slowdown -->
        <text :x="L" y="11" class="fill-gray-500" font-size="10">{{ $t('loadReading.slowdownAxis') }}</text>
        <line v-for="t in sTicks" :key="`st${t}`" :x1="L" :x2="W - R" :y1="sy(t)" :y2="sy(t)" stroke="#e1e0d9" stroke-width="1" />
        <text v-for="t in sTicks" :key="`sl${t}`" :x="L - 4" :y="sy(t) + 3" text-anchor="end" font-size="9" class="fill-gray-400">{{ t }}×</text>
        <polyline :points="slowdownPoints" fill="none" stroke="#2a78d6" stroke-width="2" stroke-linejoin="round" />
        <g v-for="(g, i) in groups" :key="`p${g.group}`">
          <circle :cx="x(i)" :cy="sy(g.slowdown)" r="4" fill="#2a78d6" stroke="#fff" stroke-width="2">
            <title>{{ tip(g) }}</title>
          </circle>
          <rect :x="x(i) - band / 2" :y="TOP" :width="band" :height="TOP_H" fill="transparent"><title>{{ tip(g) }}</title></rect>
        </g>

        <!-- knee marker across both panels -->
        <g v-if="kneeIndex !== null">
          <line :x1="kneeX" :x2="kneeX" :y1="TOP - 4" :y2="BOT + BOT_H" stroke="#0b0b0b" stroke-width="1" stroke-dasharray="3 3" />
          <text :x="kneeX + 4" :y="TOP + 6" font-size="10" class="fill-gray-700">{{ $t('loadReading.knee') }}</text>
        </g>

        <!-- bottom panel: requests per level -->
        <text :x="L" :y="BOT - 6" class="fill-gray-500" font-size="10">{{ $t('loadReading.requestsAxis') }}</text>
        <g v-for="(g, i) in groups" :key="`b${g.group}`">
          <rect
            :x="x(i) - barW / 2" :y="by(g.requests)" :width="barW" :height="Math.max(1, BOT + BOT_H - by(g.requests))"
            :fill="pastKnee(g) ? '#2a78d6' : '#c3c2b7'" rx="2"
          ><title>{{ tip(g) }}</title></rect>
          <text :x="x(i)" :y="BOT + BOT_H + 12" text-anchor="middle" font-size="9" class="fill-gray-500">{{ i === groups.length - 1 && isTopFolded ? `${g.group}+` : g.group }}</text>
        </g>
        <text v-if="kneeIndex !== null" :x="kneeX + 4" :y="BOT + 10" font-size="10" class="fill-gray-700">
          {{ $t('loadReading.pastKnee', { share: `${Math.round((reading.w_log ?? 0) * 100)}%` }) }}
        </text>
        <line :x1="L" :x2="W - R" :y1="BOT + BOT_H" :y2="BOT + BOT_H" stroke="#c3c2b7" stroke-width="1" />
        <text :x="(L + W - R) / 2" :y="H - 2" text-anchor="middle" font-size="10" class="fill-gray-500">{{ $t('loadReading.xAxis') }}</text>
      </svg>
    </div>

    <!-- Plain facts -->
    <dl v-if="reading.c_typ !== undefined" class="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1">
      <div>
        <dt class="text-gray-400">{{ $t('loadReading.typical') }}</dt>
        <dd class="text-gray-700">{{ reading.c_typ!.toFixed(1) }}</dd>
      </div>
      <div>
        <dt class="text-gray-400">{{ $t('loadReading.busiest') }}</dt>
        <dd class="text-gray-700">{{ reading.c_peak?.toFixed(1) ?? '–' }}</dd>
      </div>
      <div>
        <dt class="text-gray-400">{{ $t('loadReading.requests') }}</dt>
        <dd class="text-gray-700">{{ (reading.requests ?? 0).toLocaleString() }}</dd>
      </div>
      <div v-if="reading.window_start">
        <dt class="text-gray-400">{{ $t('loadReading.window') }}</dt>
        <dd class="text-gray-700">{{ day(reading.window_start) }} → {{ day(reading.window_end) }}</dd>
      </div>
    </dl>

    <!-- How far to trust it -->
    <details v-if="reading.fit" class="text-gray-500">
      <summary class="cursor-pointer select-none">{{ $t('loadReading.fitTitle') }}</summary>
      <ul class="mt-1 space-y-0.5">
        <li v-for="(ok, name) in reading.fit.checks" :key="name">
          <span :class="ok === false ? (hardChecks.includes(name) ? 'text-red-700' : 'text-amber-600') : ok ? 'text-gray-700' : 'text-gray-400'">
            {{ ok === false ? (hardChecks.includes(name) ? '✗' : '!') : ok ? '✓' : '–' }}
          </span>
          {{ $t(`loadReading.check.${name}`, checkArgs) }}
          <span v-if="ok === false && !hardChecks.includes(name)" class="text-gray-400">{{ $t('loadReading.noteOnly') }}</span>
        </li>
      </ul>
      <p class="mt-1">
        {{ $t('loadReading.sizeLine', sizeArgs) }}
      </p>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { LoadReading, LoadReadingGroup } from '@/api/client'
import HelpTip from '@/components/HelpTip.vue'

const props = defineProps<{ reading: LoadReading }>()
const { t } = useI18n()

const idleBelow = computed(() => props.reading.thresholds?.idle_below ?? 0.1)
const busyFrom = computed(() => props.reading.thresholds?.busy_from ?? 0.5)
const groups = computed<LoadReadingGroup[]>(() => props.reading.groups ?? [])
const isTopFolded = computed(() => {
  const last = groups.value[groups.value.length - 1]
  return !!last && (props.reading.c_req_max ?? 0) > last.group * 1.15
})

function pct(v: number): string {
  return `${(Math.max(0, Math.min(1, v)) * 100).toFixed(1)}%`
}
function day(iso?: string): string {
  return iso ? new Date(iso).toLocaleDateString() : ''
}

const hardChecks = computed(() => props.reading.fit?.hard_checks ?? Object.keys(props.reading.fit?.checks ?? {}))
const failedChecks = computed(() =>
  Object.entries(props.reading.fit?.checks ?? {})
    .filter(([name, ok]) => ok === false && hardChecks.value.includes(name))
    .map(([name]) => t(`loadReading.checkShort.${name}`))
    .join(', '),
)

const sentence = computed(() => {
  const r = props.reading
  const share = `${Math.round((r.w_log ?? 0) * 100)}%`
  if (r.status === 'failed') return t('loadReading.failed', { error: r.error ?? '' })
  if (r.status === 'no_traffic' || r.status === 'too_little_traffic') return t('loadReading.tooLittle', { note: r.note ?? '' })
  if (r.category === 'unclear') {
    return t('loadReading.unclear', {
      checks: failedChecks.value,
      fitted: r.fitted_category ? t(`loadReading.cat.${r.fitted_category}`) : '–',
    })
  }
  if (!groups.value.length) return t('loadReading.idleNoOverlap', { share: `${Math.round((r.overlapped_share ?? 0) * 100)}%` })
  if (r.knee_group == null) return t('loadReading.noKnee', { max: groups.value[groups.value.length - 1].group })
  return t(`loadReading.say.${r.category}`, { share, knee: r.knee_group })
})

const badgeClass = computed(() => ({
  idle: 'bg-sky-100 text-sky-900',
  moderate: 'bg-sky-300 text-sky-950',
  busy: 'bg-sky-700 text-white',
  unclear: 'bg-white text-gray-600 ring-1 ring-gray-300',
}[props.reading.category]))

// --- chart geometry ----------------------------------------------------------
const W = 560
const L = 34
const R = 10
const TOP = 18
const TOP_H = 92
const BOT = 140
const BOT_H = 56
const H = BOT + BOT_H + 30
const band = computed(() => (W - L - R) / Math.max(1, groups.value.length))
const barW = computed(() => Math.max(4, Math.min(28, band.value - 4)))
function x(i: number): number {
  return L + band.value * (i + 0.5)
}
const sMax = computed(() => Math.max(1.2, ...groups.value.map((g) => g.slowdown)))
function sy(v: number): number {
  return TOP + TOP_H - ((v - 1) / (sMax.value - 1)) * TOP_H
}
const sTicks = computed(() => {
  const step = sMax.value > 4 ? 1 : 0.5
  const out: number[] = []
  for (let v = 1; v <= sMax.value + 1e-9; v += step) out.push(+v.toFixed(1))
  return out
})
const slowdownPoints = computed(() => groups.value.map((g, i) => `${x(i)},${sy(g.slowdown)}`).join(' '))
const nMax = computed(() => Math.max(1, ...groups.value.map((g) => g.requests)))
function by(n: number): number {
  return BOT + BOT_H - (n / nMax.value) * BOT_H
}
const kneeIndex = computed(() => {
  const k = props.reading.knee_group
  if (k == null) return null
  const i = groups.value.findIndex((g) => g.group === k)
  return i < 0 ? null : i
})
const kneeX = computed(() => (kneeIndex.value === null ? 0 : x(kneeIndex.value) - band.value / 2))
function pastKnee(g: LoadReadingGroup): boolean {
  const k = props.reading.knee_group
  return k != null && g.group >= k
}
function tip(g: LoadReadingGroup): string {
  return t('loadReading.tip', {
    group: g.group,
    requests: g.requests.toLocaleString(),
    share: `${Math.round(g.share_from * 100)}%`,
    slowdown: g.slowdown.toFixed(2),
    raw: g.raw_slowdown.toFixed(2),
    e: g.e_into == null ? '–' : g.e_into.toFixed(2),
    out: Math.round(g.output_mean),
    prompt: Math.round(g.prompt_mean).toLocaleString(),
  })
}

function fmtW(w?: number): string {
  return w == null ? '–' : `${Math.round(w * 100)}%`
}
const checkArgs = computed(() => ({
  r2: props.reading.fit?.r2_log?.toFixed(2) ?? '–',
  even: fmtW(props.reading.fit?.halves?.even_days?.w_log),
  odd: fmtW(props.reading.fit?.halves?.odd_days?.w_log),
  crossings: props.reading.crossings ?? 0,
}))
const sizeArgs = computed(() => {
  const c = props.reading.fit?.size_coefficients_s ?? {}
  const f = (v?: number) => (v ?? 0).toFixed(3)
  return { fixed: f(c.fixed), uncached: f(c.per_1k_uncached), cached: f(c.per_1k_cached), output: f(c.per_1k_output) }
})
</script>
