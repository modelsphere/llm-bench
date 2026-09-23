<template>
  <!-- Nothing rendered for exempt users (admins) or a clean slate. -->
  <div v-if="quota && !quota.exempt && quota.active > 0">
    <div
      v-if="blocked"
      class="bg-red-50 border border-red-300 text-red-800 px-4 py-3 rounded text-sm"
    >
      <p class="font-semibold">
        {{ $t('quota.limitReached', { active: quota.active, limit: quota.limit }) }}
      </p>
      <p class="mt-1">
        {{ $t('quota.blockedHint') }}
        <router-link to="/submissions" class="underline font-medium hover:text-red-900">
          {{ $t('quota.cancelLink') }}</router-link>{{ $t('quota.blockedHintEnd') }}
      </p>
    </div>
    <p v-else class="text-xs text-gray-500">
      {{ $t('quota.active', { active: quota.active, limit: quota.limit }) }}
      <span v-if="quota.limit - quota.active === 1" class="text-amber-600 font-medium">
        {{ $t('quota.oneSlotLeft') }}</span>
    </p>
  </div>
</template>

<script setup lang="ts">
// Proactive per-user submission-quota banner for the submit pages: shows usage,
// warns near the cap, and hard-blocks (red banner + emits blocked=true so the
// page disables its submit buttons) when active >= limit. The backend enforces
// the same cap with a 429, so this failing open (quota=null on fetch error) is
// safe — it only loses the early warning, never the enforcement.
import { computed, onMounted, ref, watch } from 'vue'
import { submissionsApi, type QuotaStatus } from '../api/client'

const emit = defineEmits<{ (e: 'blocked', value: boolean): void }>()

const quota = ref<QuotaStatus | null>(null)
const blocked = computed(
  () => !!quota.value && !quota.value.exempt && quota.value.active >= quota.value.limit,
)
watch(blocked, (v) => emit('blocked', v), { immediate: true })

async function refresh() {
  try {
    quota.value = (await submissionsApi.quota()).data
  } catch {
    quota.value = null
  }
}

onMounted(refresh)
defineExpose({ refresh })
</script>
