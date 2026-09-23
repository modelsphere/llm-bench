<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-50">
    <div class="max-w-md w-full space-y-8 p-8 bg-white rounded-xl shadow-sm border border-gray-200">
      <div>
        <h2 class="text-center text-2xl font-bold text-gray-900">{{ $t('auth.forgot.title') }}</h2>
        <p class="mt-2 text-center text-sm text-gray-600">
          {{ $t('auth.forgot.hint') }}
        </p>
      </div>

      <div
        v-if="submitted"
        class="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded text-sm"
      >
        {{ $t('auth.forgot.submitted') }}
      </div>

      <form v-else class="space-y-6" @submit.prevent="handleSubmit">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
          {{ error }}
        </div>

        <div>
          <label for="email" class="block text-sm font-medium text-gray-700">{{ $t('common.email') }}</label>
          <input
            id="email"
            v-model="email"
            type="email"
            required
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
            placeholder="you@example.com"
          />
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
        >
          {{ loading ? $t('auth.forgot.submitting') : $t('auth.forgot.request') }}
        </button>
      </form>

      <p class="text-center text-sm text-gray-600">
        <router-link to="/login" class="font-medium text-indigo-600 hover:text-indigo-500">
          {{ $t('auth.forgot.backToSignIn') }}
        </router-link>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { passwordResetApi } from '@/api/client'

const { t } = useI18n()
const email = ref('')
const loading = ref(false)
const error = ref('')
const submitted = ref(false)
const message = ref('')

async function handleSubmit() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await passwordResetApi.requestReset(email.value)
    // The backend returns the same generic message regardless of whether the
    // account exists or is eligible — no enumeration.
    message.value = data.message
    submitted.value = true
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('auth.forgot.failed')
  } finally {
    loading.value = false
  }
}
</script>
