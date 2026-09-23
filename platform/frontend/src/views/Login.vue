<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-50">
    <div class="max-w-md w-full space-y-8 p-8 bg-white rounded-xl shadow-sm border border-gray-200">
      <div>
        <h2 class="text-center text-2xl font-bold text-gray-900">{{ $t('auth.login.title') }}</h2>
        <p class="mt-2 text-center text-sm text-gray-600">
          {{ $t('common.appName') }}
        </p>
      </div>

      <form class="space-y-6" @submit.prevent="handleLogin">
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
          <label for="password" class="block text-sm font-medium text-gray-700">{{ $t('common.password') }}</label>
          <input
            id="password"
            v-model="password"
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
          {{ loading ? $t('auth.login.signingIn') : $t('auth.login.signIn') }}
        </button>

        <p class="text-right text-sm">
          <router-link to="/forgot-password" class="font-medium text-indigo-600 hover:text-indigo-500">
            {{ $t('auth.login.forgot') }}
          </router-link>
        </p>
      </form>

      <p class="text-center text-sm text-gray-600">
        {{ $t('auth.login.noAccount') }}
        <router-link to="/register" class="font-medium text-indigo-600 hover:text-indigo-500">
          {{ $t('auth.login.registerHere') }}
        </router-link>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useI18n } from 'vue-i18n'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const { t } = useI18n()

const email = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function handleLogin() {
  loading.value = true
  error.value = ''
  try {
    await auth.login(email.value, password.value)
    const redirect = (route.query.redirect as string) || '/benchmarks'
    router.push(redirect)
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('auth.login.failed')
  } finally {
    loading.value = false
  }
}
</script>
