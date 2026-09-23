<template>
  <div class="prose prose-sm max-w-none" v-html="rendered"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{ source: string }>()

const rendered = computed(() => renderMarkdown(props.source))
</script>
