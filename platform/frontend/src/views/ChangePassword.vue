<template>
  <div class="max-w-md mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="bg-white rounded-xl shadow-sm border border-gray-200 p-8 space-y-6">
      <div>
        <h1 class="text-xl font-bold text-gray-900">{{ $t('auth.changePw.title') }}</h1>
        <p class="text-sm text-gray-500 mt-1">{{ $t('auth.changePw.signedInAs', { name: auth.user?.username }) }}</p>
      </div>

      <form class="space-y-6" @submit.prevent="submit">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
          {{ error }}
        </div>
        <div v-if="success" class="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded text-sm">
          {{ $t('auth.changePw.updated') }}
        </div>

        <div>
          <label for="current" class="block text-sm font-medium text-gray-700">{{ $t('auth.changePw.current') }}</label>
          <input
            id="current"
            v-model="current"
            type="password"
            required
            autocomplete="current-password"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
        </div>

        <div>
          <label for="new" class="block text-sm font-medium text-gray-700">{{ $t('auth.changePw.new') }}</label>
          <input
            id="new"
            v-model="next"
            type="password"
            required
            minlength="8"
            autocomplete="new-password"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
          <p class="mt-1 text-xs text-gray-400">{{ $t('auth.changePw.min8') }}</p>
        </div>

        <div>
          <label for="confirm" class="block text-sm font-medium text-gray-700">{{ $t('auth.changePw.confirmNew') }}</label>
          <input
            id="confirm"
            v-model="confirm"
            type="password"
            required
            autocomplete="new-password"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
        >
          {{ loading ? $t('auth.changePw.updating') : $t('auth.changePw.update') }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { authApi } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useI18n } from 'vue-i18n'

const auth = useAuthStore()
const { t } = useI18n()

const current = ref('')
const next = ref('')
const confirm = ref('')
const loading = ref(false)
const error = ref('')
const success = ref(false)

async function submit() {
  error.value = ''
  success.value = false
  if (next.value !== confirm.value) {
    error.value = t('auth.changePw.mismatch')
    return
  }
  loading.value = true
  try {
    const { data } = await authApi.changePassword({ current_password: current.value, new_password: next.value })
    // The change invalidated our old token; adopt the fresh one so this session
    // (and the 401 interceptor) don't bounce us to the login page.
    auth.setToken(data.access_token)
    success.value = true
    current.value = ''
    next.value = ''
    confirm.value = ''
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('auth.changePw.failed')
  } finally {
    loading.value = false
  }
}
</script>
