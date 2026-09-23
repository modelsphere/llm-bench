<template>
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6 flex flex-wrap items-center justify-between gap-3">
      <h1 class="text-2xl font-bold text-gray-900">{{ $t('nav.benchmarks') }}</h1>
      <div class="flex items-center gap-4">
        <!-- View toggle: grouped cards / grouped list / legacy flat grid -->
        <div
          class="inline-flex rounded-md border border-gray-300 bg-white shadow-sm text-xs font-medium overflow-hidden"
          role="group"
          :aria-label="$t('benchList.viewLabel')"
        >
          <button
            v-for="opt in VIEW_OPTIONS"
            :key="opt.value"
            type="button"
            class="px-3 py-1.5 border-r border-gray-300 last:border-r-0 transition-colors"
            :class="view === opt.value
              ? 'bg-indigo-600 text-white'
              : 'text-gray-600 hover:bg-gray-50'"
            :title="$t(opt.tip)"
            :aria-pressed="view === opt.value"
            @click="setView(opt.value)"
          >{{ $t(opt.label) }}</button>
        </div>
        <button
          v-if="view !== 'flat' && tree.roots.length"
          type="button"
          class="text-xs text-gray-500 hover:text-gray-700"
          @click="toggleAll"
        >{{ allCollapsed ? $t('editor.expandAll') : $t('editor.collapseAll') }}</button>
        <router-link
          v-if="auth.isAdmin"
          to="/admin/benchmarks"
          class="text-sm text-indigo-600 hover:text-indigo-500"
        >
          {{ $t('benchList.manage') }}
        </router-link>
      </div>
    </div>

    <div v-if="store.loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div v-else-if="store.error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
      {{ store.error }}
    </div>
    <div v-else-if="store.activeBenchmarks.length === 0" class="text-center py-12 text-gray-500">
      {{ $t('benchList.empty') }}
    </div>

    <!-- Legacy flat grid: every active benchmark once, in API order -->
    <div v-else-if="view === 'flat'" class="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
      <BenchmarkCard
        v-for="benchmark in store.activeBenchmarks"
        :key="benchmark.slug"
        :benchmark="benchmark"
        show-groups
      />
    </div>

    <!-- Grouped: one section per top-level tag, nested up to 3 deep, then "Not grouped" -->
    <div v-else>
      <BenchmarkGroup
        v-for="node in tree.roots"
        :key="node.path"
        :node="node"
        :depth="1"
        :mode="view"
        :collapsed-paths="collapsedPaths"
        @toggle="toggleGroup"
      />
      <BenchmarkGroup
        v-if="ungroupedNode"
        :node="ungroupedNode"
        :depth="1"
        :mode="view"
        :collapsed-paths="collapsedPaths"
        @toggle="toggleGroup"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useBenchmarksStore } from '@/stores/benchmarks'
import { useAuthStore } from '@/stores/auth'
import BenchmarkCard from '@/components/BenchmarkCard.vue'
import BenchmarkGroup from '@/components/BenchmarkGroup.vue'
import { buildGroupTree, type GroupNode } from '@/utils/groupTags'

const store = useBenchmarksStore()
const auth = useAuthStore()
const { t } = useI18n()

// 'cards' and 'list' are both grouped by tag; 'flat' is the pre-grouping grid.
type ViewMode = 'cards' | 'list' | 'flat'
const VIEW_OPTIONS: { value: ViewMode; label: string; tip: string }[] = [
  { value: 'cards', label: 'benchList.viewCards', tip: 'benchList.viewCardsTip' },
  { value: 'list', label: 'benchList.viewList', tip: 'benchList.viewListTip' },
  { value: 'flat', label: 'benchList.viewFlat', tip: 'benchList.viewFlatTip' },
]
const VIEW_KEY = 'benchList.view'
const COLLAPSED_KEY = 'benchList.collapsed'

// Reserved key for the untagged bucket: "/" can never be a real path (the
// backend rejects empty segments), so it cannot collide with a tag.
const UNGROUPED_PATH = '/'

function loadView(): ViewMode {
  try {
    const v = localStorage.getItem(VIEW_KEY)
    if (v === 'cards' || v === 'list' || v === 'flat') return v
  } catch { /* private mode etc. — fall through to the default */ }
  return 'cards'
}

const view = ref<ViewMode>(loadView())
function setView(v: ViewMode) {
  view.value = v
  try { localStorage.setItem(VIEW_KEY, v) } catch { /* ignore */ }
}

const tree = computed(() => buildGroupTree(store.activeBenchmarks))

const ungroupedNode = computed<GroupNode | null>(() => {
  if (!tree.value.ungrouped.length) return null
  return {
    name: t('benchList.notGrouped'),
    path: UNGROUPED_PATH,
    depth: 1,
    benchmarks: tree.value.ungrouped,
    children: [],
  }
})

// Collapse state is keyed by full path so it survives re-fetches and is
// shared between the cards and list views.
const collapsedPaths = ref<Set<string>>(loadCollapsed())

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : [])
  } catch {
    return new Set()
  }
}

function saveCollapsed() {
  try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsedPaths.value])) } catch { /* ignore */ }
}

function toggleGroup(path: string) {
  const next = new Set(collapsedPaths.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  collapsedPaths.value = next
  saveCollapsed()
}

const topLevelPaths = computed(() => {
  const paths = tree.value.roots.map((n) => n.path)
  if (ungroupedNode.value) paths.push(UNGROUPED_PATH)
  return paths
})

const allCollapsed = computed(
  () => topLevelPaths.value.length > 0 && topLevelPaths.value.every((p) => collapsedPaths.value.has(p)),
)

// "Collapse all" folds only the top level; expanding restores every level so
// nothing stays hidden inside an open section.
function toggleAll() {
  collapsedPaths.value = allCollapsed.value ? new Set() : new Set(topLevelPaths.value)
  saveCollapsed()
}

onMounted(() => store.fetchBenchmarks())
</script>
