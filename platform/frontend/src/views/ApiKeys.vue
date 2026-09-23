<template>
  <div class="max-w-2xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
    <div class="bg-white rounded-xl shadow-sm border border-gray-200 p-8 space-y-6">
      <div>
        <h1 class="text-xl font-bold text-gray-900">{{ $t('nav.apiKeys') }}</h1>
        <p class="text-sm text-gray-500 mt-1">
          {{ $t('apiKeys.subtitle1') }}
          <code class="px-1 py-0.5 bg-gray-100 rounded text-gray-700">Authorization: Bearer &lt;key&gt;</code>.
          {{ $t('apiKeys.subtitle2') }}
        </p>
      </div>

      <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
        {{ error }}
      </div>

      <!-- Freshly created key — shown once -->
      <div v-if="created" class="bg-green-50 border border-green-200 rounded p-4 space-y-2">
        <p class="text-sm font-medium text-green-800">
          {{ $t('apiKeys.createdOnce') }}
        </p>
        <div class="flex items-center gap-2">
          <code class="flex-1 break-all bg-white border border-green-200 rounded px-3 py-2 text-sm text-gray-800">{{ created.key }}</code>
          <button
            type="button"
            class="shrink-0 px-3 py-2 text-sm font-medium rounded-md bg-green-600 text-white hover:bg-green-700"
            @click="copyKey"
          >
            {{ copied ? $t('users.copied') : $t('users.copy') }}
          </button>
        </div>
      </div>

      <!-- Create form -->
      <form class="flex items-end gap-3" @submit.prevent="create">
        <div class="flex-1">
          <label for="keyname" class="block text-sm font-medium text-gray-700">{{ $t('apiKeys.newKeyName') }}</label>
          <input
            id="keyname"
            v-model="newName"
            type="text"
            required
            maxlength="100"
            placeholder="e.g. ci-pipeline"
            class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
          />
        </div>
        <button
          type="submit"
          :disabled="creating || !newName.trim()"
          class="py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
        >
          {{ creating ? $t('editor.creating') : $t('apiKeys.createKey') }}
        </button>
      </form>
    </div>

    <!-- Existing keys -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
      <div v-if="loading" class="p-8 text-center text-sm text-gray-400">{{ $t('common.loading') }}</div>
      <div v-else-if="keys.length === 0" class="p-8 text-center text-sm text-gray-400">
        No API keys yet.
      </div>
      <table v-else class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('adminBench.name') }}</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('apiKeys.key') }}</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('apiKeys.created') }}</th>
            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('apiKeys.lastUsed') }}</th>
            <th class="px-6 py-3"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          <tr v-for="k in keys" :key="k.id">
            <td class="px-6 py-3 text-sm text-gray-900">{{ k.name }}</td>
            <td class="px-6 py-3 text-sm text-gray-500"><code>{{ k.key_prefix }}…</code></td>
            <td class="px-6 py-3 text-sm text-gray-500">{{ fmt(k.created_at) }}</td>
            <td class="px-6 py-3 text-sm text-gray-500">{{ k.last_used_at ? fmt(k.last_used_at) : $t('apiKeys.never') }}</td>
            <td class="px-6 py-3 text-right">
              <button
                type="button"
                class="text-sm text-red-600 hover:text-red-800"
                @click="revoke(k)"
              >
                {{ $t('apiKeys.revoke') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { apiKeysApi, type ApiKey, type ApiKeyCreated } from '@/api/client'
import { copyToClipboard } from '@/utils/clipboard'

const { t } = useI18n()
const keys = ref<ApiKey[]>([])
const loading = ref(true)
const error = ref('')
const newName = ref('')
const creating = ref(false)
const created = ref<ApiKeyCreated | null>(null)
const copied = ref(false)

function fmt(iso: string) {
  return new Date(iso).toLocaleString()
}

async function load() {
  loading.value = true
  try {
    keys.value = (await apiKeysApi.list()).data
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('apiKeys.loadFailed')
  } finally {
    loading.value = false
  }
}

async function create() {
  error.value = ''
  creating.value = true
  copied.value = false
  try {
    created.value = (await apiKeysApi.create(newName.value.trim())).data
    newName.value = ''
    await load()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('apiKeys.createFailed')
  } finally {
    creating.value = false
  }
}

async function copyKey() {
  if (!created.value) return
  copied.value = await copyToClipboard(created.value.key)
}

async function revoke(k: ApiKey) {
  if (!confirm(t('apiKeys.revokeConfirm', { name: k.name }))) return
  error.value = ''
  try {
    await apiKeysApi.revoke(k.id)
    // If we just revoked the key we were showing, clear the one-time banner.
    if (created.value && created.value.id === k.id) created.value = null
    await load()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('apiKeys.revokeFailed')
  }
}

onMounted(load)
</script>
