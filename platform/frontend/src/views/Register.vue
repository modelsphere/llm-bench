<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-50">
    <div class="max-w-md w-full space-y-8 p-8 bg-white rounded-xl shadow-sm border border-gray-200">
      <div>
        <h2 class="text-center text-2xl font-bold text-gray-900">{{ $t('auth.register.title') }}</h2>
      </div>

      <form class="space-y-6" @submit.prevent="handleRegister">
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

        <div>
          <label for="username" class="block text-sm font-medium text-gray-700">{{ $t('common.username') }}</label>
          <input
            id="username"
            v-model="username"
            type="text"
            required
            minlength="3"
            maxlength="100"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
        </div>

        <div>
          <label for="password" class="block text-sm font-medium text-gray-700">{{ $t('common.password') }}</label>
          <input
            id="password"
            v-model="password"
            type="password"
            required
            minlength="8"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50"
        >
          {{ loading ? $t('auth.register.creating') : $t('auth.register.create') }}
        </button>
      </form>

      <p class="text-center text-sm text-gray-600">
        {{ $t('auth.register.haveAccount') }}
        <router-link to="/login" class="font-medium text-indigo-600 hover:text-indigo-500">
          {{ $t('auth.register.signInHere') }}
        </router-link>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useI18n } from 'vue-i18n'

const router = useRouter()
const auth = useAuthStore()
const { t } = useI18n()

const email = ref('')
const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function handleRegister() {
  loading.value = true
  error.value = ''
  try {
    await auth.register(email.value, username.value, password.value)
    router.push('/benchmarks')
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('auth.register.failed')
  } finally {
    loading.value = false
  }
}
</script>
