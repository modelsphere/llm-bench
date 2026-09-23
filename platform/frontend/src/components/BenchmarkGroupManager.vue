<template>
  <div>
    <p class="text-sm text-gray-500 mb-4">{{ $t('adminGroups.hint') }}</p>

    <div v-if="loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div v-else-if="loadError" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
      {{ loadError }}
    </div>

    <template v-else>
      <div
        v-if="message"
        class="mb-4 border px-4 py-2 rounded text-sm flex items-start gap-2"
        :class="messageTone === 'error'
          ? 'bg-red-50 border-red-200 text-red-700'
          : 'bg-amber-50 border-amber-200 text-amber-800'"
      >
        <span class="flex-1">{{ message }}</span>
        <button type="button" class="opacity-60 hover:opacity-100" @click="message = ''">✕</button>
      </div>

      <div class="flex flex-col lg:flex-row gap-6 items-start">
        <!-- Groups -->
        <div class="w-full lg:w-80 shrink-0 bg-white rounded-lg border border-gray-200 shadow-sm">
          <div class="flex items-center justify-between px-3 py-2 border-b border-gray-200">
            <h2 class="text-sm font-semibold text-gray-700">{{ $t('adminGroups.groupsPane') }}</h2>
            <button
              type="button"
              class="text-sm text-indigo-600 hover:text-indigo-500"
              @click="editing = { path: '', mode: 'add' }"
            >{{ $t('adminGroups.newGroup') }}</button>
          </div>

          <ul class="py-2 max-h-[32rem] overflow-y-auto">
            <li>
              <div
                class="flex items-center gap-2 rounded px-2 py-1 mx-1 cursor-pointer text-sm"
                :class="selection.mode === 'all' ? 'bg-indigo-50 text-indigo-800 font-medium' : 'text-gray-700 hover:bg-gray-50'"
                @click="selection = { mode: 'all', path: '' }"
              >
                <span class="flex-1">{{ $t('adminGroups.allBenchmarks') }}</span>
                <span class="text-xs text-gray-400">{{ benchmarks.length }}</span>
              </div>
            </li>

            <!-- Top-level group being typed. -->
            <li v-if="editing?.mode === 'add' && editing.path === ''" class="px-2 py-1">
              <input
                ref="rootInput"
                v-model="rootBuffer"
                type="text"
                :maxlength="GROUP_TAG_SEGMENT_MAX"
                class="block w-full rounded border-gray-300 shadow-sm text-xs"
                :placeholder="$t('adminGroups.newTopGroup')"
                @keydown.enter.prevent="commitEdit(rootBuffer)"
                @keydown.esc.prevent="cancelEdit"
                @blur="commitEdit(rootBuffer)"
              />
            </li>

            <GroupTreeNode
              v-for="node in roots"
              :key="node.path"
              :node="node"
              :counts="totalsByPath"
              :selected-path="selection.mode === 'group' ? selection.path : null"
              :editing="editing"
              @select="selection = { mode: 'group', path: $event }"
              @rename-start="editing = { path: $event, mode: 'rename' }"
              @add-start="editing = { path: $event, mode: 'add' }"
              @delete="deleteGroup"
              @commit="commitEdit"
              @cancel="cancelEdit"
            />

            <li>
              <div
                class="flex items-center gap-2 rounded px-2 py-1 mx-1 mt-1 cursor-pointer text-sm border-t border-gray-100 pt-2"
                :class="selection.mode === 'ungrouped' ? 'bg-indigo-50 text-indigo-800 font-medium' : 'text-gray-500 hover:bg-gray-50'"
                @click="selection = { mode: 'ungrouped', path: '' }"
              >
                <span class="flex-1">{{ $t('benchList.notGrouped') }}</span>
                <span class="text-xs text-gray-400">{{ ungroupedCount }}</span>
              </div>
            </li>
          </ul>
        </div>

        <!-- Benchmarks -->
        <div class="flex-1 min-w-0 bg-white rounded-lg border border-gray-200 shadow-sm">
          <div class="px-4 py-3 border-b border-gray-200 flex flex-wrap items-center gap-3">
            <div class="flex-1 min-w-0">
              <h2 class="text-sm font-semibold text-gray-900 truncate">{{ paneTitle }}</h2>
              <p class="text-xs text-gray-500">
                <template v-if="selection.mode === 'group'">
                  {{ $t('adminGroups.membersCount', { n: memberIds.size }) }}
                </template>
                <template v-else>{{ $t('adminGroups.pickGroupHint') }}</template>
              </p>
            </div>
            <input
              v-model="search"
              type="search"
              :placeholder="$t('adminGroups.searchPlaceholder')"
              class="rounded-md border-gray-300 shadow-sm text-sm w-56"
            />
          </div>

          <div v-if="selection.mode === 'group'" class="px-4 py-2 border-b border-gray-100 flex items-center gap-3 text-xs">
            <button type="button" class="text-indigo-600 hover:text-indigo-500" @click="setAllVisible(true)">
              {{ $t('adminGroups.selectAllShown') }}
            </button>
            <button type="button" class="text-gray-500 hover:text-gray-700" @click="setAllVisible(false)">
              {{ $t('adminGroups.clearAllShown') }}
            </button>
          </div>

          <div v-if="!visibleBenchmarks.length" class="px-4 py-10 text-center text-sm text-gray-400">
            {{ $t('adminGroups.noBenchmarks') }}
          </div>
          <ul v-else class="divide-y divide-gray-100 max-h-[32rem] overflow-y-auto">
            <li
              v-for="b in visibleBenchmarks"
              :key="b.id"
              class="px-4 py-2 flex items-start gap-3 hover:bg-gray-50"
            >
              <input
                v-if="selection.mode === 'group'"
                type="checkbox"
                class="mt-1 rounded border-gray-300 text-indigo-600"
                :checked="memberIds.has(b.id)"
                @change="toggleMembership(b, ($event.target as HTMLInputElement).checked)"
              />
              <div class="min-w-0 flex-1">
                <div class="text-sm text-gray-900 truncate">{{ b.name }}</div>
                <div class="text-xs text-gray-400 font-mono truncate">{{ b.slug }}</div>
              </div>
              <div class="flex flex-wrap justify-end gap-1 max-w-[55%]">
                <span
                  v-for="tag in draft[b.id] || []"
                  :key="tag"
                  class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px]"
                  :class="selection.mode === 'group' && tag === selection.path
                    ? 'bg-indigo-100 text-indigo-800'
                    : 'bg-gray-100 text-gray-600'"
                >
                  {{ tag }}
                  <button
                    type="button"
                    class="text-gray-400 hover:text-red-600"
                    :title="$t('adminGroups.removeTag')"
                    @click="removeTag(b, tag)"
                  >✕</button>
                </span>
                <span v-if="!(draft[b.id] || []).length" class="text-[11px] text-gray-300">
                  {{ $t('benchList.notGrouped') }}
                </span>
              </div>
            </li>
          </ul>
        </div>
      </div>

      <!-- Nothing above touched the server: this bar is the only writer. -->
      <div
        v-if="dirty"
        class="sticky bottom-4 mt-4 bg-white border border-indigo-200 shadow-lg rounded-lg px-4 py-3 flex flex-wrap items-center gap-3"
      >
        <div class="flex-1 min-w-0">
          <p v-if="changed.length" class="text-sm font-medium text-gray-800">
            {{ $t('adminGroups.pendingChanges', { n: changed.length }) }}
          </p>
          <p v-if="emptyStaged.length" class="text-xs text-amber-600">
            {{ $t('adminGroups.emptyGroupsWarning', { groups: emptyStaged.join(', ') }) }}
          </p>
        </div>
        <button
          type="button"
          class="px-3 py-2 text-sm text-gray-600 hover:text-gray-800"
          :disabled="saving"
          @click="reset"
        >{{ $t('adminGroups.discard') }}</button>
        <button
          type="button"
          class="px-4 py-2 rounded-md text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
          :disabled="saving || !changed.length"
          @click="save"
        >{{ saving ? $t('common.loading') : $t('adminGroups.save') }}</button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useI18n } from 'vue-i18n'
import GroupTreeNode from '@/components/GroupTreeNode.vue'
import { benchmarksApi } from '@/api/client'
import type { Benchmark } from '@/api/client'
import {
  GROUP_TAG_MAX_COUNT,
  GROUP_TAG_MAX_DEPTH,
  GROUP_TAG_SEGMENT_MAX,
  GROUP_TAG_SEPARATOR,
  buildGroupPathTree,
  isUnderPath,
  normalizeGroupPath,
  pathDepth,
  relativeSubtreeDepth,
  removePathIn,
  renamePathIn,
  validateGroupPath,
  type GroupPathError,
} from '@/utils/groupTags'

// Bulk editor for the display grouping. There is no group entity server-side —
// a group exists exactly as long as some benchmark carries its path — so this
// works on a draft of every benchmark's tag list and saves the rows that
// actually changed in one atomic call. Renaming a group is a prefix rewrite
// across all of them; deleting one drops the prefix. Nothing is written until
// Save, which is what makes the destructive-looking actions safe to offer.

const emit = defineEmits<{ saved: [] }>()

const { t } = useI18n()

const benchmarks = ref<Benchmark[]>([])
const draft = ref<Record<number, string[]>>({})
// Groups created here but not yet given a benchmark. They cannot be persisted
// (nothing stores them), so they live only until Save — see emptyStaged.
const stagedPaths = ref<string[]>([])
const selection = ref<{ mode: 'all' | 'ungrouped' | 'group'; path: string }>({ mode: 'all', path: '' })
const editing = ref<{ path: string; mode: 'rename' | 'add' } | null>(null)
const rootBuffer = ref('')
const rootInput = ref<HTMLInputElement | null>(null)
const search = ref('')
const loading = ref(false)
const loadError = ref('')
const message = ref('')
const messageTone = ref<'info' | 'error'>('info')
const saving = ref(false)

function notify(text: string, tone: 'info' | 'error' = 'info') {
  message.value = text
  messageTone.value = tone
}

onMounted(load)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await benchmarksApi.adminList()
    benchmarks.value = data.benchmarks
    reset()
  } catch (e: any) {
    loadError.value = e.response?.data?.detail || t('adminBench.loadFailed')
  } finally {
    loading.value = false
  }
}

function reset() {
  draft.value = Object.fromEntries(benchmarks.value.map((b) => [b.id, [...(b.group_tags ?? [])]]))
  stagedPaths.value = []
  editing.value = null
  message.value = ''
}

// --- derived state ---------------------------------------------------------

const allPaths = computed(() => {
  const paths = new Set<string>(stagedPaths.value)
  for (const tags of Object.values(draft.value)) for (const tag of tags) paths.add(tag)
  return paths
})

const roots = computed(() => buildGroupPathTree(allPaths.value))

/** Benchmarks under each path, counting a benchmark once per tag — the same
 *  count the public benchmarks page shows for that heading. */
const totalsByPath = computed(() => {
  const out: Record<string, number> = {}
  for (const tags of Object.values(draft.value)) {
    for (const tag of tags) {
      let prefix = ''
      for (const seg of tag.split(GROUP_TAG_SEPARATOR)) {
        prefix = prefix ? `${prefix}${GROUP_TAG_SEPARATOR}${seg}` : seg
        out[prefix] = (out[prefix] || 0) + 1
      }
    }
  }
  return out
})

const ungroupedCount = computed(
  () => benchmarks.value.filter((b) => !(draft.value[b.id] ?? []).length).length,
)

/** Ids tagged with exactly the selected path (not merely under it). */
const memberIds = computed(() => {
  const ids = new Set<number>()
  if (selection.value.mode !== 'group') return ids
  for (const b of benchmarks.value) {
    if ((draft.value[b.id] ?? []).includes(selection.value.path)) ids.add(b.id)
  }
  return ids
})

const paneTitle = computed(() => {
  if (selection.value.mode === 'group') return selection.value.path
  if (selection.value.mode === 'ungrouped') return t('benchList.notGrouped')
  return t('adminGroups.allBenchmarks')
})

const visibleBenchmarks = computed(() => {
  const q = search.value.trim().toLowerCase()
  let list = benchmarks.value
  if (selection.value.mode === 'ungrouped') {
    list = list.filter((b) => !(draft.value[b.id] ?? []).length)
  }
  if (q) {
    list = list.filter(
      (b) => b.name.toLowerCase().includes(q) || b.slug.toLowerCase().includes(q),
    )
  }
  if (selection.value.mode === 'group') {
    // Members first: the point of the pane is reviewing and adjusting them,
    // and the rest of the list is only there so more can be ticked in.
    const members = list.filter((b) => memberIds.value.has(b.id))
    return [...members, ...list.filter((b) => !memberIds.value.has(b.id))]
  }
  return list
})

const changed = computed(() =>
  benchmarks.value.filter((b) => !sameTags(draft.value[b.id] ?? [], b.group_tags ?? [])),
)

const emptyStaged = computed(() => stagedPaths.value.filter((p) => !totalsByPath.value[p]))

// Staged groups count as unsaved state even though they save nothing: losing
// them silently on a tab switch or navigation would be just as annoying.
const dirty = computed(() => changed.value.length > 0 || stagedPaths.value.length > 0)

function sameTags(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((tag, i) => tag === b[i])
}

// --- group edits (draft only) ----------------------------------------------

function pathErrorMessage(err: GroupPathError): string {
  switch (err) {
    case 'tooDeep':
      return t('adminGroups.errTooDeep', { n: GROUP_TAG_MAX_DEPTH })
    case 'segmentTooLong':
      return t('adminGroups.errSegmentTooLong', { n: GROUP_TAG_SEGMENT_MAX })
    default:
      return t('adminGroups.errEmptySegment')
  }
}

function cancelEdit() {
  editing.value = null
  rootBuffer.value = ''
}

function commitEdit(value: string) {
  const current = editing.value
  if (!current) return
  editing.value = null
  rootBuffer.value = ''
  const raw = value.trim()
  if (!raw) return // blank input is a cancel, not an error
  if (current.mode === 'add') {
    addGroup(current.path ? `${current.path}${GROUP_TAG_SEPARATOR}${raw}` : raw)
  } else {
    renameGroup(current.path, raw)
  }
}

function addGroup(path: string) {
  const err = validateGroupPath(path)
  if (err) {
    notify(pathErrorMessage(err), 'error')
    return
  }
  const normalized = normalizeGroupPath(path)
  if (!allPaths.value.has(normalized)) stagedPaths.value = [...stagedPaths.value, normalized]
  selection.value = { mode: 'group', path: normalized }
}

function renameGroup(from: string, to: string) {
  const err = validateGroupPath(to)
  if (err) {
    notify(pathErrorMessage(err), 'error')
    return
  }
  const target = normalizeGroupPath(to)
  if (target === from) return
  // Moving a group carries its sub-groups with it, and they must still fit
  // within the three levels the tag format allows.
  if (pathDepth(target) + relativeSubtreeDepth(allPaths.value, from) - 1 > GROUP_TAG_MAX_DEPTH) {
    notify(t('adminGroups.errMoveTooDeep', { n: GROUP_TAG_MAX_DEPTH }), 'error')
    return
  }
  const merging = allPaths.value.has(target)
  for (const b of benchmarks.value) {
    draft.value[b.id] = renamePathIn(draft.value[b.id] ?? [], from, target)
  }
  const staged: string[] = []
  for (const p of stagedPaths.value) {
    const next = isUnderPath(p, from) ? target + p.slice(from.length) : p
    if (!staged.includes(next)) staged.push(next)
  }
  stagedPaths.value = staged
  if (selection.value.mode === 'group' && isUnderPath(selection.value.path, from)) {
    selection.value = { mode: 'group', path: target + selection.value.path.slice(from.length) }
  }
  if (merging) notify(t('adminGroups.mergedInto', { from, to: target }))
}

function deleteGroup(path: string) {
  // Discard is the only undo, and it would also throw away every other pending
  // edit — so ask before dropping a group that still has benchmarks in it.
  const affected = totalsByPath.value[path] || 0
  if (affected && !window.confirm(t('adminGroups.confirmDelete', { path, n: affected }))) return
  for (const b of benchmarks.value) {
    draft.value[b.id] = removePathIn(draft.value[b.id] ?? [], path)
  }
  stagedPaths.value = stagedPaths.value.filter((p) => !isUnderPath(p, path))
  if (selection.value.mode === 'group' && isUnderPath(selection.value.path, path)) {
    selection.value = { mode: 'all', path: '' }
  }
}

// --- membership edits (draft only) -----------------------------------------

function toggleMembership(b: Benchmark, member: boolean): boolean {
  const path = selection.value.path
  const tags = draft.value[b.id] ?? []
  if (member) {
    if (tags.includes(path)) return true
    if (tags.length >= GROUP_TAG_MAX_COUNT) {
      notify(t('adminGroups.errTooManyTags', { n: GROUP_TAG_MAX_COUNT, name: b.name }), 'error')
      return false
    }
    draft.value[b.id] = [...tags, path]
  } else {
    draft.value[b.id] = tags.filter((tag) => tag !== path)
  }
  return true
}

function removeTag(b: Benchmark, tag: string) {
  draft.value[b.id] = (draft.value[b.id] ?? []).filter((t2) => t2 !== tag)
}

function setAllVisible(member: boolean) {
  for (const b of visibleBenchmarks.value) {
    // Stop at the first benchmark that cannot take another tag: its message
    // explains why, and carrying on would half-apply the action silently.
    if (!toggleMembership(b, member)) break
  }
}

// --- persistence -----------------------------------------------------------

async function save() {
  if (!changed.value.length) return
  saving.value = true
  message.value = ''
  try {
    await benchmarksApi.bulkGroupTags(
      changed.value.map((b) => ({ benchmark_id: b.id, group_tags: draft.value[b.id] ?? [] })),
    )
    await load()
    emit('saved')
  } catch (e: any) {
    notify(e.response?.data?.detail || t('adminGroups.saveFailed'), 'error')
  } finally {
    saving.value = false
  }
}

// --- unsaved-work guards ---------------------------------------------------

watch(
  () => editing.value?.mode === 'add' && editing.value.path === '',
  async (addingRoot) => {
    if (!addingRoot) return
    await nextTick()
    rootInput.value?.focus()
  },
)

function warnOnUnload(e: BeforeUnloadEvent) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}

onMounted(() => window.addEventListener('beforeunload', warnOnUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', warnOnUnload))

onBeforeRouteLeave(() => (dirty.value ? window.confirm(t('adminGroups.leaveConfirm')) : true))

defineExpose({ reload: load })
</script>
