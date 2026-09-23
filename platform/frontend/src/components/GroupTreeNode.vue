<template>
  <li>
    <!-- The group itself: click to select, hover for the actions. -->
    <div
      v-if="!renaming"
      class="group flex items-center gap-1 rounded px-2 py-1 cursor-pointer text-sm"
      :class="selectedPath === node.path ? 'bg-indigo-50 text-indigo-800 font-medium' : 'text-gray-700 hover:bg-gray-50'"
      :style="{ paddingLeft: `${(node.depth - 1) * 14 + 8}px` }"
      @click="$emit('select', node.path)"
    >
      <span class="truncate">{{ node.name }}</span>
      <span class="text-xs text-gray-400">{{ counts[node.path] || 0 }}</span>
      <span class="flex-1"></span>
      <span class="hidden group-hover:flex items-center gap-1 shrink-0">
        <button
          type="button"
          class="px-1 text-xs text-gray-500 hover:text-indigo-600"
          :title="$t('adminGroups.rename')"
          @click.stop="$emit('rename-start', node.path)"
        >✎</button>
        <button
          v-if="node.depth < GROUP_TAG_MAX_DEPTH"
          type="button"
          class="px-1 text-xs text-gray-500 hover:text-indigo-600"
          :title="$t('adminGroups.addChild')"
          @click.stop="$emit('add-start', node.path)"
        >＋</button>
        <button
          type="button"
          class="px-1 text-xs text-gray-500 hover:text-red-600"
          :title="$t('adminGroups.deleteGroup')"
          @click.stop="$emit('delete', node.path)"
        >✕</button>
      </span>
    </div>

    <!-- Rename edits the whole path, so retyping it under another parent is
         also how a subtree is moved. -->
    <div v-else class="px-2 py-1" :style="{ paddingLeft: `${(node.depth - 1) * 14 + 8}px` }">
      <input
        ref="input"
        v-model="buffer"
        type="text"
        class="block w-full rounded border-gray-300 shadow-sm text-xs font-mono"
        :placeholder="$t('adminGroups.pathPlaceholder')"
        @keydown.enter.prevent="commit"
        @keydown.esc.prevent="cancel"
        @blur="commit"
      />
      <p class="mt-1 text-[11px] text-gray-400">{{ $t('adminGroups.renameHint') }}</p>
    </div>

    <ul>
      <GroupTreeNode
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :counts="counts"
        :selected-path="selectedPath"
        :editing="editing"
        @select="$emit('select', $event)"
        @rename-start="$emit('rename-start', $event)"
        @add-start="$emit('add-start', $event)"
        @delete="$emit('delete', $event)"
        @commit="$emit('commit', $event)"
        @cancel="$emit('cancel')"
      />

      <!-- New sub-group, typed in place under its parent. -->
      <li v-if="adding" class="px-2 py-1" :style="{ paddingLeft: `${node.depth * 14 + 8}px` }">
        <input
          ref="input"
          v-model="buffer"
          type="text"
          :maxlength="GROUP_TAG_SEGMENT_MAX"
          class="block w-full rounded border-gray-300 shadow-sm text-xs"
          :placeholder="$t('adminGroups.newSubGroup')"
          @keydown.enter.prevent="commit"
          @keydown.esc.prevent="cancel"
          @blur="commit"
        />
      </li>
    </ul>
  </li>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  GROUP_TAG_MAX_DEPTH,
  GROUP_TAG_SEGMENT_MAX,
  type GroupPathNode,
} from '@/utils/groupTags'

// One row of the admin group tree, rendered recursively (1-3 levels). Editing
// state lives in the parent manager — only one row is ever editable — and the
// text being typed is local, so a keystroke doesn't re-render the whole tree.
const props = defineProps<{
  node: GroupPathNode
  /** path -> benchmarks under it, including sub-groups. */
  counts: Record<string, number>
  selectedPath: string | null
  editing: { path: string; mode: 'rename' | 'add' } | null
}>()

const emit = defineEmits<{
  select: [path: string]
  'rename-start': [path: string]
  'add-start': [path: string]
  delete: [path: string]
  commit: [value: string]
  cancel: []
}>()

const renaming = computed(() => props.editing?.mode === 'rename' && props.editing.path === props.node.path)
const adding = computed(() => props.editing?.mode === 'add' && props.editing.path === props.node.path)

const buffer = ref('')
const input = ref<HTMLInputElement | null>(null)
// Escape unmounts the input, and in some browsers that also fires blur — which
// would commit the very edit the user just abandoned. One flag settles it.
let cancelled = false

function commit() {
  if (cancelled) return
  emit('commit', buffer.value)
}

function cancel() {
  cancelled = true
  emit('cancel')
}

watch(
  () => (renaming.value ? 'rename' : adding.value ? 'add' : ''),
  async (mode) => {
    if (!mode) return
    cancelled = false
    buffer.value = mode === 'rename' ? props.node.path : ''
    await nextTick()
    input.value?.focus()
    input.value?.select()
  },
  { immediate: true },
)
</script>
