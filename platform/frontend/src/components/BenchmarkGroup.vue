<template>
  <section :class="depth === 1 ? 'mb-8' : 'mt-4'">
    <button
      type="button"
      class="w-full flex items-center gap-2 text-left group"
      :class="headingClass"
      :aria-expanded="!collapsed"
      @click="$emit('toggle', node.path)"
    >
      <span
        class="inline-block text-gray-400 transition-transform"
        :class="collapsed ? '' : 'rotate-90'"
      >▸</span>
      <span>{{ node.name }}</span>
      <span class="text-xs font-normal text-gray-400">{{ $t('benchList.groupCount', { n: total }) }}</span>
      <span v-if="depth === 1" class="flex-1 border-t border-gray-200 ml-2"></span>
    </button>

    <div v-show="!collapsed" :class="depth === 1 ? 'mt-4' : 'mt-2 pl-4 border-l-2 border-gray-100'">
      <template v-if="node.benchmarks.length">
        <div v-if="mode === 'cards'" class="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
          <BenchmarkCard
            v-for="b in node.benchmarks"
            :key="b.slug"
            :benchmark="b"
          />
        </div>
        <div v-else class="rounded-lg border border-gray-200 divide-y divide-gray-100 overflow-hidden">
          <BenchmarkRow v-for="b in node.benchmarks" :key="b.slug" :benchmark="b" />
        </div>
      </template>

      <BenchmarkGroup
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
        :mode="mode"
        :collapsed-paths="collapsedPaths"
        @toggle="$emit('toggle', $event)"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import BenchmarkCard from '@/components/BenchmarkCard.vue'
import BenchmarkRow from '@/components/BenchmarkRow.vue'
import { countInTree, type GroupNode } from '@/utils/groupTags'

// One group heading plus its direct benchmarks and nested sub-groups. Renders
// itself recursively, so the 1-3 level tree only needs one component;
// heading weight steps down with depth so the hierarchy reads at a glance.
const props = defineProps<{
  node: GroupNode
  depth: number
  mode: 'cards' | 'list'
  collapsedPaths: Set<string>
}>()

defineEmits<{ toggle: [path: string] }>()

const collapsed = computed(() => props.collapsedPaths.has(props.node.path))
const total = computed(() => countInTree(props.node))

const headingClass = computed(() => {
  switch (props.depth) {
    case 1:
      return 'text-lg font-semibold text-gray-900'
    case 2:
      return 'text-base font-medium text-gray-800'
    default:
      return 'text-sm font-medium text-gray-600'
  }
})
</script>
