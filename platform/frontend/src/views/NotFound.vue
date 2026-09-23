<template>
  <div class="min-h-[70vh] flex items-center justify-center px-4">
    <div class="text-center max-w-md">
      <p class="text-6xl font-bold text-indigo-600">404</p>
      <h1 class="mt-4 text-xl font-bold text-gray-900">{{ $t('notFound.title') }}</h1>
      <p class="mt-2 text-sm text-gray-500">
        {{ $t('notFound.before') }}<code class="px-1 py-0.5 bg-gray-100 rounded text-gray-700">{{ path }}</code>{{ $t('notFound.after') }}
      </p>
      <p class="mt-1 text-sm text-gray-500">
        {{ $t('notFound.apiDocsHint') }}
        <a :href="docsUrl" class="text-indigo-600 hover:underline">{{ docsUrl }}</a>
      </p>
      <router-link
        to="/"
        class="mt-6 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
      >
        {{ $t('notFound.goHome') }}
      </router-link>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
const path = route.fullPath
// The backend is served under /api by the frontend nginx; Swagger UI lives there.
const docsUrl = `${window.location.origin}/api/docs`
</script>
