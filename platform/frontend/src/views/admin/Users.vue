<template>
  <div class="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="text-xl font-bold text-gray-900">{{ $t('nav.users') }}</h1>
        <p class="text-sm text-gray-500 mt-1">
          {{ $t('users.subtitle') }}
        </p>
      </div>
      <button
        class="text-sm text-gray-500 hover:text-gray-700"
        :disabled="loading"
        @click="load"
      >
        {{ $t('adminSubs.refresh') }}
      </button>
    </div>

    <div v-if="error" class="mb-4 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
      {{ error }}
    </div>

    <div v-if="loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>

    <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
      <table class="min-w-full divide-y divide-gray-200 text-sm">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('common.userId') }}</th>
            <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('common.username') }}</th>
            <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('common.email') }}</th>
            <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('users.role') }}</th>
            <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('users.registered') }}</th>
            <th class="px-4 py-2 text-right font-medium text-gray-500">{{ $t('adminBench.actions') }}</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          <tr v-for="u in users" :key="u.id">
            <td class="px-4 py-2 font-mono text-gray-500 tabular-nums">{{ u.id }}</td>
            <td class="px-4 py-2 font-medium text-gray-900">
              {{ u.username }}
              <span v-if="u.id === auth.user?.id" class="ml-1 text-xs text-gray-400">(you)</span>
            </td>
            <td class="px-4 py-2 text-gray-600">{{ u.email }}</td>
            <td class="px-4 py-2">
              <span
                :class="badgeClass(u.role)"
                class="inline-block rounded px-2 py-0.5 text-xs font-semibold"
              >
                {{ u.role }}
              </span>
            </td>
            <td class="px-4 py-2 text-gray-500">{{ formatDate(u.created_at) }}</td>
            <td class="px-4 py-2 text-right">
              <!-- super_admin rows are managed only in the backend; service
                   accounts by the deployment (seed.py) -->
              <span v-if="u.role === 'super_admin' || u.role === 'service'" class="text-xs text-gray-400">{{ $t('users.protected') }}</span>
              <!-- only super admins can mutate roles -->
              <template v-else-if="auth.isSuperAdmin">
                <button
                  v-if="u.role === 'user'"
                  :disabled="busyId === u.id"
                  class="text-xs font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                  @click="setRole(u, 'admin')"
                >
                  {{ $t('users.promote') }}
                </button>
                <button
                  v-else
                  :disabled="busyId === u.id || u.id === auth.user?.id"
                  :title="u.id === auth.user?.id ? $t('users.selfDemoteTip') : ''"
                  class="text-xs font-medium text-red-600 hover:text-red-800 disabled:opacity-40 disabled:cursor-not-allowed"
                  @click="setRole(u, 'user')"
                >
                  {{ $t('users.demote') }}
                </button>
              </template>
              <!-- plain admins get a read-only list -->
              <span v-else class="text-xs text-gray-300">—</span>
            </td>
          </tr>
          <tr v-if="users.length === 0">
            <td colspan="6" class="px-4 py-6 text-center text-gray-400">{{ $t('users.empty') }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Password reset queue — super admins only. Approving mints a one-time
         link the admin hands to the user out-of-band. -->
    <div v-if="auth.isSuperAdmin" class="mt-12">
      <div class="flex items-center justify-between mb-3">
        <div>
          <h2 class="text-lg font-bold text-gray-900">{{ $t('users.resetRequests') }}</h2>
          <p class="text-sm text-gray-500 mt-1">
            {{ $t('users.resetSubtitle') }}
          </p>
        </div>
        <button
          class="text-sm text-gray-500 hover:text-gray-700"
          :disabled="resetsLoading"
          @click="loadResets"
        >
          {{ $t('adminSubs.refresh') }}
        </button>
      </div>

      <div v-if="resetError" class="mb-4 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
        {{ resetError }}
      </div>

      <!-- Shown once, right after an approval. -->
      <div v-if="revealedLink" class="mb-4 bg-amber-50 border border-amber-200 rounded-lg p-4">
        <p class="text-sm font-medium text-amber-900">
          {{ $t('users.resetLinkOnce') }}
        </p>
        <div class="mt-2 flex items-center gap-2">
          <input
            ref="linkInput"
            readonly
            :value="revealedLink"
            class="flex-1 rounded-md border-gray-300 bg-white text-xs font-mono shadow-sm"
            @focus="selectOnFocus"
          />
          <button
            class="shrink-0 rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700"
            @click="copyLink"
          >
            {{ copied ? $t('users.copied') : $t('users.copy') }}
          </button>
        </div>
        <p class="mt-1 text-xs text-amber-700">Expires {{ formatDate(revealedExpires) }}.</p>
      </div>

      <div v-if="resetsLoading" class="text-center py-8 text-gray-500">{{ $t('common.loading') }}</div>

      <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
        <table class="min-w-full divide-y divide-gray-200 text-sm">
          <thead class="bg-gray-50">
            <tr>
              <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('lb.user') }}</th>
              <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('common.email') }}</th>
              <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('subDetail.status') }}</th>
              <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('users.requested') }}</th>
              <th class="px-4 py-2 text-right font-medium text-gray-500">{{ $t('adminBench.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr v-for="r in resets" :key="r.id">
              <td class="px-4 py-2 font-medium text-gray-900">{{ r.username }}</td>
              <td class="px-4 py-2 text-gray-600">{{ r.email }}</td>
              <td class="px-4 py-2">
                <span
                  :class="resetBadgeClass(r.status)"
                  class="inline-block rounded px-2 py-0.5 text-xs font-semibold"
                >
                  {{ r.status }}
                </span>
              </td>
              <td class="px-4 py-2 text-gray-500">{{ formatDate(r.created_at) }}</td>
              <td class="px-4 py-2 text-right space-x-3">
                <button
                  :disabled="resetBusyId === r.id"
                  class="text-xs font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                  @click="approveReset(r)"
                >
                  {{ r.status === 'pending' ? $t('users.approve') : $t('users.reissue') }}
                </button>
                <button
                  :disabled="resetBusyId === r.id"
                  class="text-xs font-medium text-red-600 hover:text-red-800 disabled:opacity-50"
                  @click="rejectReset(r)"
                >
                  {{ $t('users.reject') }}
                </button>
              </td>
            </tr>
            <tr v-if="resets.length === 0">
              <td colspan="5" class="px-4 py-6 text-center text-gray-400">{{ $t('users.noRequests') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  adminUsersApi,
  passwordResetApi,
  type AdminUser,
  type PasswordResetRequest,
} from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { copyToClipboard } from '@/utils/clipboard'

const { t } = useI18n()
const auth = useAuthStore()
const users = ref<AdminUser[]>([])
const loading = ref(true)
const error = ref('')
const busyId = ref<number | null>(null)

// Password reset queue (super admins only).
const resets = ref<PasswordResetRequest[]>([])
const resetsLoading = ref(false)
const resetError = ref('')
const resetBusyId = ref<number | null>(null)
const revealedLink = ref('')
const revealedExpires = ref('')
const copied = ref(false)
const linkInput = ref<HTMLInputElement | null>(null)

function formatDate(iso: string): string {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function badgeClass(role: AdminUser['role']): string {
  if (role === 'super_admin') return 'bg-amber-100 text-amber-800'
  if (role === 'admin') return 'bg-indigo-100 text-indigo-800'
  if (role === 'service') return 'bg-teal-100 text-teal-800'
  return 'bg-gray-100 text-gray-700'
}

function resetBadgeClass(status: PasswordResetRequest['status']): string {
  if (status === 'approved') return 'bg-green-100 text-green-800'
  if (status === 'pending') return 'bg-yellow-100 text-yellow-800'
  return 'bg-gray-100 text-gray-700'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // Re-sync our own role first, so the super-admin-only controls below are
    // decided on a fresh role (not a stale login-time cache).
    await auth.fetchMe()
    const { data } = await adminUsersApi.list()
    users.value = data
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('users.loadFailed')
  } finally {
    loading.value = false
  }
}

async function setRole(u: AdminUser, role: 'admin' | 'user') {
  const msg = role === 'admin' ? t('users.promoteConfirm', { name: u.username }) : t('users.demoteConfirm', { name: u.username })
  if (!window.confirm(msg)) return
  busyId.value = u.id
  error.value = ''
  try {
    const { data } = await adminUsersApi.setRole(u.id, role)
    const i = users.value.findIndex((x) => x.id === u.id)
    if (i !== -1) users.value[i] = data
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('users.roleFailed')
  } finally {
    busyId.value = null
  }
}

async function loadResets() {
  resetsLoading.value = true
  resetError.value = ''
  try {
    const { data } = await passwordResetApi.list()
    resets.value = data
  } catch (e: any) {
    resetError.value = e.response?.data?.detail || t('users.resetsLoadFailed')
  } finally {
    resetsLoading.value = false
  }
}

async function approveReset(r: PasswordResetRequest) {
  if (!window.confirm(t('users.approveConfirm', { name: r.username }))) return
  resetBusyId.value = r.id
  resetError.value = ''
  try {
    const { data } = await passwordResetApi.approve(r.id)
    // The backend returns a site-relative path; build the full link with our
    // own origin so it works behind whatever host/proxy the UI is served from.
    revealedLink.value = `${window.location.origin}${data.path}`
    revealedExpires.value = data.expires_at
    copied.value = false
    await loadResets()
  } catch (e: any) {
    resetError.value = e.response?.data?.detail || t('users.approveFailed')
  } finally {
    resetBusyId.value = null
  }
}

async function rejectReset(r: PasswordResetRequest) {
  if (!window.confirm(t('users.rejectConfirm', { name: r.username }))) return
  resetBusyId.value = r.id
  resetError.value = ''
  try {
    await passwordResetApi.reject(r.id)
    await loadResets()
  } catch (e: any) {
    resetError.value = e.response?.data?.detail || t('users.rejectFailed')
  } finally {
    resetBusyId.value = null
  }
}

async function copyLink() {
  copied.value = await copyToClipboard(revealedLink.value)
  // Copy blocked even via the legacy fallback (see utils/clipboard) — select
  // the field so the admin can ⌘C manually.
  if (!copied.value) linkInput.value?.select()
}

function selectOnFocus(e: FocusEvent) {
  ;(e.target as HTMLInputElement).select()
}

onMounted(async () => {
  await load()
  // load() re-syncs our role via fetchMe, so isSuperAdmin is now accurate.
  if (auth.isSuperAdmin) await loadResets()
})
</script>
