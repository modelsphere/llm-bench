<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-50">
    <div class="max-w-md w-full space-y-8 p-8 bg-white rounded-xl shadow-sm border border-gray-200">
      <div>
        <h2 class="text-center text-2xl font-bold text-gray-900">{{ $t('auth.reset.title') }}</h2>
        <p class="mt-2 text-center text-sm text-gray-600">
          {{ $t('auth.reset.subtitle') }}
        </p>
      </div>

      <div
        v-if="!token"
        class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm"
      >
        {{ $t('auth.reset.missingToken') }}
      </div>

      <div v-else-if="done" class="space-y-6">
        <div class="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded text-sm">
          {{ $t('auth.reset.done') }}
        </div>
        <router-link
          to="/login"
          class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
        >
          {{ $t('auth.reset.goToSignIn') }}
        </router-link>
      </div>

      <form v-else class="space-y-6" @submit.prevent="handleSubmit">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
          {{ error }}
        </div>

        <div>
          <label for="new" class="block text-sm font-medium text-gray-700">{{ $t('auth.reset.newPassword') }}</label>
          <input
            id="new"
            v-model="newPassword"
            type="password"
            required
            minlength="8"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
          <p class="mt-1 text-xs text-gray-500">{{ $t('auth.reset.min8') }}</p>
        </div>

        <div>
          <label for="confirm" class="block text-sm font-medium text-gray-700">{{ $t('auth.reset.confirmPassword') }}</label>
          <input
            id="confirm"
            v-model="confirm"
            type="password"
            required
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
        >
          {{ loading ? $t('auth.reset.updating') : $t('auth.reset.update') }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { passwordResetApi } from '@/api/client'

const { t } = useI18n()
const route = useRoute()
const token = computed(() => (route.query.token as string) || '')

const newPassword = ref('')
const confirm = ref('')
const loading = ref(false)
const error = ref('')
const done = ref(false)

async function handleSubmit() {
  error.value = ''
  if (newPassword.value !== confirm.value) {
    error.value = t('auth.reset.mismatch')
    return
  }
  loading.value = true
  try {
    await passwordResetApi.reset(token.value, newPassword.value)
    done.value = true
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('auth.reset.failed')
  } finally {
    loading.value = false
  }
}
</script>
