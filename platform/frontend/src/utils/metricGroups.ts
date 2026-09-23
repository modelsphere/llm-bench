// Display-only grouping of a module's informational ("display" role) metrics
// into labelled sub-sections, so info-heavy modules (e.g. replay) don't flood
// the page with one flat list of dozens of chips.
//
// This is purely a frontend concern — the backend keeps emitting a flat metrics
// dict, and score/redline handling is untouched. Grouping is by metric-key
// regex: the FIRST matching rule in RULES wins, anything unmatched falls into
// "misc". RULES order = match priority; DISPLAY_ORDER = the order groups render.
// Labels are i18n KEYS (metricGroups.<label> in the locale files); level groups
// use the machine label "concurrency:<n>", translated by the views' groupLabel().
// To add/retune a category: edit one RULES entry, add its label to
// DISPLAY_ORDER, and add metricGroups.<label> to BOTH locale files.

export interface MetricGroup<T> {
  label: string
  terms: T[]
}

// Match priority (first hit wins). Keep specific signals (qos/debug) above the
// broad ones (tokens/requests) so e.g. `repetitive_token_count` lands in Debug,
// not Tokens, and `finish_reason_stop_count` lands in QoS, not Debug.
const RULES: { label: string; test: RegExp }[] = [
  { label: 'throughput',         test: /(^|_)(tps|tpm)(_|$)/ },                                             // output_tps_p50, input_tpm, service_tps
  { label: 'latency',            test: /ttft|tpot|wall_time|total_time|_ms$/ },                             // ttft_*_ms buckets, tpot, wall_time_s
  { label: 'qos', test: /uptime|_rate$|error|finish_reason|http_|_4xx|_5xx|unfinished|not_started|capped/ },
  { label: 'debug',              test: /_count$|repetitive|normalized|unknown|after_finish/ },              // internal counters/diagnostics
  { label: 'tokens',             test: /token/ },                                                           // total_*_tokens, avg_*_tokens
  { label: 'requests',           test: /request/ },                                                         // total_requests, tool_requests_total
]

// Render order: useful groups first, diagnostics/leftovers last.
const DISPLAY_ORDER = ['latency', 'throughput', 'tokens', 'qos', 'requests', 'debug', 'misc']

// Below this many informational metrics, grouping isn't worth the extra headers
// — the caller renders a flat list instead (see SubmissionDetail).
export const GROUPING_MIN = 12

// Per-concurrency-level metrics (perf_guidellm_sweep) use dotted keys like
// "c8.ttft_p99_ms". They get one dynamic group per level ("Concurrency 8"),
// checked BEFORE the static rules so e.g. "c8.output_tps_mean" doesn't land
// in Throughput. Level groups render after the static ones, sorted numerically.
// Inside a level group the redundant "c8." prefix is stripped from the chip
// label (the group header already says which level), and chips are ordered by
// metric family (DISPLAY_ORDER) so every level group reads the same way.
const LEVEL_KEY_RE = /^c(\d+)\./

function familyRank(key: string): number {
  const rule = RULES.find(r => r.test.test(key))
  const idx = DISPLAY_ORDER.indexOf(rule ? rule.label : 'misc')
  return idx === -1 ? DISPLAY_ORDER.length : idx
}

export function groupMetricTerms<T extends { key: string; displayName?: string }>(
  terms: T[],
): MetricGroup<T>[] {
  const buckets = new Map<string, T[]>()
  const levelOf = new Map<string, number>()
  for (let t of terms) {
    const levelMatch = LEVEL_KEY_RE.exec(t.key)
    let label: string
    if (levelMatch) {
      label = `concurrency:${levelMatch[1]}`
      levelOf.set(label, Number(levelMatch[1]))
      // Strip the level prefix from auto-derived labels (displayName === key);
      // a curated pretty name is left untouched.
      if (t.displayName === t.key) {
        t = { ...t, displayName: t.key.slice(levelMatch[0].length) }
      }
    } else {
      const rule = RULES.find(r => r.test.test(t.key))
      label = rule ? rule.label : 'misc'
    }
    if (!buckets.has(label)) buckets.set(label, [])
    buckets.get(label)!.push(t)
  }
  const levelOrder = [...levelOf.entries()]
    .sort((a, b) => a[1] - b[1])
    .map(([label]) => label)
  for (const label of levelOrder) {
    buckets.get(label)!.sort((a, b) => {
      const strippedA = a.key.replace(LEVEL_KEY_RE, '')
      const strippedB = b.key.replace(LEVEL_KEY_RE, '')
      return familyRank(strippedA) - familyRank(strippedB)
        || strippedA.localeCompare(strippedB)
    })
  }
  return [...DISPLAY_ORDER, ...levelOrder]
    .filter(label => buckets.has(label))
    .map(label => ({ label, terms: buckets.get(label)! }))
}
