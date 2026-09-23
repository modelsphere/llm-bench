<template>
  <div class="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6">
      <h1 class="text-2xl font-bold text-gray-900">{{ $t('nav.version') }}</h1>
      <p class="mt-1 text-sm text-gray-500">
        Image identifiers for the running k8s deployment. Use this to verify that
        the cluster is running the code you just built — if the backend or frontend
        SHA below doesn't match
        <code class="font-mono">git rev-parse HEAD</code> in your working tree, the
        image hasn't been rebuilt/redeployed yet.
      </p>
    </div>

    <div v-if="loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>
    <div
      v-else-if="error"
      class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm whitespace-pre-wrap"
    >
      {{ error }}
    </div>

    <div v-else class="space-y-6">
      <!-- Backend -->
      <section class="bg-white border border-gray-200 rounded-lg shadow-sm overflow-hidden">
        <div class="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
          <h2 class="text-sm font-semibold text-gray-900">Backend</h2>
          <span
            v-if="backend?.git_sha === 'unknown'"
            class="text-xs px-2 py-0.5 rounded bg-yellow-100 text-yellow-800"
          >no build info</span>
        </div>
        <dl class="divide-y divide-gray-100">
          <KV label="Image tag" :value="backend?.image_tag" />
          <KV label="Git SHA" :value="backend?.git_sha" mono />
          <KV label="Git ref" :value="backend?.git_ref" />
          <KV label="Build time" :value="backend?.build_time" />
          <KV label="Alembic revision" :value="alembicRevision ?? '(unknown)'" mono />
        </dl>
      </section>

      <!-- Frontend -->
      <section class="bg-white border border-gray-200 rounded-lg shadow-sm overflow-hidden">
        <div class="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
          <h2 class="text-sm font-semibold text-gray-900">Frontend</h2>
          <span
            v-if="frontend?.git_sha === 'unknown'"
            class="text-xs px-2 py-0.5 rounded bg-yellow-100 text-yellow-800"
          >no build info</span>
        </div>
        <dl class="divide-y divide-gray-100">
          <KV label="Image tag" :value="frontend?.image_tag" />
          <KV label="Git SHA" :value="frontend?.git_sha" mono />
          <KV label="Git ref" :value="frontend?.git_ref" />
          <KV label="Build time" :value="frontend?.build_time" />
        </dl>
      </section>

      <!-- Match indicator -->
      <section
        v-if="backend && frontend"
        class="text-sm rounded-md border px-4 py-3"
        :class="
          isLocalDev
            ? 'bg-blue-50 border-blue-200 text-blue-800'
            : shasMatch
              ? 'bg-green-50 border-green-200 text-green-800'
              : 'bg-yellow-50 border-yellow-200 text-yellow-800'
        "
      >
        <span v-if="isLocalDev">
          Running in local dev mode — backend served by uvicorn against the
          working tree, frontend served by Vite. SHA comparison disabled.
        </span>
        <span v-else-if="shasMatch">
          Backend &amp; frontend were built from the same commit.
        </span>
        <span v-else>
          Backend SHA <code class="font-mono">{{ shortSha(backend.git_sha) }}</code> does
          NOT match frontend SHA
          <code class="font-mono">{{ shortSha(frontend.git_sha) }}</code> — one of the
          images is stale.
        </span>
      </section>

      <button
        @click="load"
        class="text-sm text-indigo-600 hover:text-indigo-700"
      >{{ $t('adminSubs.refresh') }}</button>

      <!-- Easter egg: the wordmark below the hashes. Five rapid clicks says hi. -->
      <div class="pt-6 flex flex-col items-center gap-2 select-none">
        <BrandLogo
          class="h-10 w-auto opacity-60 hover:opacity-100 transition-opacity cursor-pointer"
          @click="onLogoClick"
        />
        <p v-if="greeting" class="text-sm font-medium text-green-600">{{ greeting }}</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, h } from 'vue'
import { adminApi, type BuildInfo } from '@/api/client'
import BrandLogo from '@/components/BrandLogo.vue'

const loading = ref(true)
const error = ref<string | null>(null)
const backend = ref<BuildInfo | null>(null)
const frontend = ref<BuildInfo | null>(null)
const alembicRevision = ref<string | null>(null)

const shortSha = (s?: string) => (s && s !== 'unknown' ? s.slice(0, 12) : 'unknown')

// "local-dev" is the sentinel baked into public/build-info.json so Vite serves
// SOMETHING valid in dev mode, and emitted by the backend when it falls back
// to `git rev-parse HEAD` (no /app/build_info.json present). Don't warn about
// SHA mismatches when either side is local-dev — they're not really comparable.
const isLocalDev = computed(() =>
  backend.value?.build_time === 'local-dev' || frontend.value?.git_sha === 'local-dev'
)

const shasMatch = computed(() => {
  if (!backend.value || !frontend.value) return false
  if (backend.value.git_sha === 'unknown' || frontend.value.git_sha === 'unknown') return false
  return backend.value.git_sha === frontend.value.git_sha
})

async function load() {
  loading.value = true
  error.value = null
  try {
    const [backendRes, frontendRes] = await Promise.allSettled([
      adminApi.version(),
      adminApi.frontendBuildInfo(),
    ])

    if (backendRes.status === 'fulfilled') {
      backend.value = backendRes.value.data.backend
      alembicRevision.value = backendRes.value.data.alembic_revision
    } else {
      const msg = backendRes.reason?.response?.data?.detail || backendRes.reason?.message
      error.value = `Backend /admin/version failed: ${msg}`
    }

    if (frontendRes.status === 'fulfilled') {
      frontend.value = frontendRes.value.data
    } else {
      // /build-info.json is only baked into the production nginx image. In
      // Vite dev mode it 404s; the frontend SHA is whatever's in the running
      // checkout, which matches the backend (running from the same checkout).
      // Mark it as local-dev so the match-indicator doesn't false-positive.
      const isLocalDev = backend.value?.build_time === 'local-dev'
      frontend.value = {
        component: 'frontend',
        git_sha: isLocalDev ? (backend.value?.git_sha ?? 'unknown') : 'unknown',
        git_ref: isLocalDev ? (backend.value?.git_ref ?? 'unknown') : 'unknown',
        build_time: isLocalDev ? 'local-dev (vite)' : 'unknown',
        image_tag: isLocalDev ? 'local-dev' : 'unknown',
      }
    }
  } finally {
    loading.value = false
  }
}

// Tiny key/value row helper to keep markup tidy.
const KV = (props: { label: string; value?: string | null; mono?: boolean }) =>
  h('div', { class: 'px-4 py-3 flex items-baseline gap-4' }, [
    h('dt', { class: 'w-40 shrink-0 text-xs uppercase tracking-wide text-gray-500' }, props.label),
    h(
      'dd',
      { class: ['text-sm text-gray-900 break-all', props.mono ? 'font-mono' : ''].join(' ') },
      props.value || '—',
    ),
  ])

// Easter egg: five clicks within 1.5s of each other says hi.
const greeting = ref<string | null>(null)
const clickTimes: number[] = []
let greetingTimer: ReturnType<typeof setTimeout> | null = null

function onLogoClick() {
  const now = Date.now()
  clickTimes.push(now)
  // Keep only the last 5 clicks.
  if (clickTimes.length > 5) clickTimes.shift()
  // Fire when the 5 most recent clicks span less than 1.5s.
  if (clickTimes.length === 5 && now - clickTimes[0] < 1500) {
    clickTimes.length = 0
    greeting.value = '👋 hi!'
    if (greetingTimer) clearTimeout(greetingTimer)
    greetingTimer = setTimeout(() => { greeting.value = null }, 3000)
  }
}

onMounted(load)
</script>
