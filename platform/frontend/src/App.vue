<template>
  <div class="min-h-screen bg-gray-50">
    <nav v-if="auth.isAuthenticated" class="bg-white shadow-sm border-b border-gray-200 relative z-40">
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="flex justify-between h-16">
          <!-- Left: primary nav -->
          <div class="flex items-center space-x-8">
            <router-link to="/" class="flex items-center" :aria-label="$t('nav.home')">
              <BrandLogo class="h-7 w-auto" />
            </router-link>
            <div class="flex space-x-4 items-center">
              <router-link to="/benchmarks" class="nav-link" :class="{ active: $route.path.startsWith('/benchmarks') && !$route.path.includes('/admin') }">
                {{ $t('nav.benchmarks') }}
              </router-link>
              <router-link to="/submissions" class="nav-link" :class="{ active: $route.path === '/submissions' }">
                {{ $t('nav.mySubmissions') }}
              </router-link>

              <!-- Admin dropdown -->
              <div v-if="auth.isAdmin" class="relative">
                <button
                  type="button"
                  class="nav-link inline-flex items-center gap-1"
                  :class="{ active: onAdminSection }"
                  aria-haspopup="true"
                  :aria-expanded="adminMenuOpen"
                  @click="toggleAdmin"
                >
                  {{ $t('nav.admin') }}
                  <svg class="h-4 w-4 transition-transform" :class="{ 'rotate-180': adminMenuOpen }" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.06l3.71-3.83a.75.75 0 111.08 1.04l-4.25 4.39a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z" clip-rule="evenodd"/></svg>
                </button>
                <div v-if="adminMenuOpen" class="dropdown left-0" role="menu">
                  <router-link to="/admin/benchmarks" class="menu-item" :class="{ active: $route.path.startsWith('/admin/benchmarks') }" @click="closeMenus">{{ $t('nav.manageBenchmarks') }}</router-link>
                  <router-link to="/admin/submissions" class="menu-item" :class="{ active: $route.path.startsWith('/admin/submissions') }" @click="closeMenus">{{ $t('nav.allSubmissions') }}</router-link>
                  <router-link to="/admin/users" class="menu-item" :class="{ active: $route.path.startsWith('/admin/users') }" @click="closeMenus">{{ $t('nav.users') }}</router-link>
                  <router-link to="/admin/card-types" class="menu-item" :class="{ active: $route.path.startsWith('/admin/card-types') }" @click="closeMenus">{{ $t('nav.cardTypes') }}</router-link>
                  <router-link to="/admin/replay-datasets" class="menu-item" :class="{ active: $route.path.startsWith('/admin/replay-datasets') }" @click="closeMenus">{{ $t('nav.replayDatasets') }}</router-link>
                </div>
              </div>
            </div>
          </div>

          <!-- Right: locale switcher + account menu -->
          <div class="flex items-center gap-3">
            <button
              type="button"
              class="px-2 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-600 hover:bg-gray-50 hover:border-gray-400"
              :title="$t('nav.switchLocale')"
              @click="toggleLocale"
            >
              {{ locale === 'en' ? '中文' : 'EN' }}
            </button>
            <div class="relative">
              <button
                type="button"
                class="flex items-center gap-2 rounded-full border bg-white pl-1 pr-2.5 py-1 transition-colors hover:bg-gray-50"
                :class="userMenuOpen ? 'border-indigo-300 ring-2 ring-indigo-100' : 'border-gray-300 hover:border-gray-400'"
                aria-haspopup="true"
                :aria-expanded="userMenuOpen"
                :title="$t('nav.accountMenu')"
                @click="toggleUser"
              >
                <span class="flex items-center justify-center h-6 w-6 rounded-full bg-indigo-600 text-white text-xs font-semibold uppercase">{{ userInitial }}</span>
                <span class="text-sm font-medium text-gray-700 max-w-[10rem] truncate">{{ auth.user?.username }}</span>
                <svg class="h-4 w-4 text-gray-400 transition-transform" :class="{ 'rotate-180': userMenuOpen }" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.06l3.71-3.83a.75.75 0 111.08 1.04l-4.25 4.39a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z" clip-rule="evenodd"/></svg>
              </button>

              <div v-if="userMenuOpen" class="dropdown right-0 w-56" role="menu">
                <div class="px-4 py-2 border-b border-gray-100">
                  <div class="text-sm font-medium text-gray-900 truncate">{{ auth.user?.username }}</div>
                  <div class="text-xs text-gray-500 truncate">{{ auth.user?.email }} · {{ auth.user?.role }}</div>
                  <div v-if="auth.user?.id" class="text-xs text-gray-400">
                    {{ $t('common.userId') }}: <span class="font-mono text-gray-500">{{ auth.user.id }}</span>
                  </div>
                </div>
                <router-link to="/account/api-keys" class="menu-item" :class="{ active: $route.path === '/account/api-keys' }" @click="closeMenus">{{ $t('nav.apiKeys') }}</router-link>
                <router-link v-if="auth.isAdmin" to="/admin/version" class="menu-item" :class="{ active: $route.path === '/admin/version' }" @click="closeMenus">{{ $t('nav.version') }}</router-link>
                <router-link to="/account/password" class="menu-item" :class="{ active: $route.path === '/account/password' }" @click="closeMenus">{{ $t('nav.changePassword') }}</router-link>

                <div class="border-t border-gray-100 mt-1 pt-1">
                  <button v-if="!logoutConfirm" type="button" class="menu-item w-full text-left text-red-600 hover:bg-red-50" @click="logoutConfirm = true">
                    {{ $t('nav.logout') }}
                  </button>
                  <template v-else>
                    <div class="px-4 pt-1 pb-2 text-xs text-gray-500">{{ $t('nav.logoutConfirm') }}</div>
                    <div class="flex gap-2 px-3 pb-2">
                      <button type="button" class="flex-1 px-2 py-1.5 text-xs rounded-md bg-red-600 text-white hover:bg-red-700" @click="confirmLogout">{{ $t('nav.logoutYes') }}</button>
                      <button type="button" class="flex-1 px-2 py-1.5 text-xs rounded-md border border-gray-300 hover:bg-gray-50" @click="logoutConfirm = false">{{ $t('common.cancel') }}</button>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </nav>

    <!-- Click-outside backdrop: closes any open menu. Below the nav (z-40), above the page. -->
    <div v-if="adminMenuOpen || userMenuOpen" class="fixed inset-0 z-30" @click="closeMenus"></div>

    <main>
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { setLocale, type AppLocale } from '@/i18n'
import BrandLogo from '@/components/BrandLogo.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const { locale } = useI18n()
function toggleLocale() {
  setLocale((locale.value === 'en' ? 'zh-CN' : 'en') as AppLocale)
}

const adminMenuOpen = ref(false)
const userMenuOpen = ref(false)
const logoutConfirm = ref(false)

const userInitial = computed(() => (auth.user?.username || '?').charAt(0))
// Highlight the Admin trigger for the pages that live in its dropdown (Version
// now lives in the account menu, so it doesn't light up Admin).
const onAdminSection = computed(() =>
  ['/admin/benchmarks', '/admin/submissions', '/admin/users', '/admin/card-types', '/admin/replay-datasets'].some((p) => route.path.startsWith(p)),
)

function closeMenus() {
  adminMenuOpen.value = false
  userMenuOpen.value = false
  logoutConfirm.value = false
}
function toggleAdmin() {
  const next = !adminMenuOpen.value
  closeMenus()
  adminMenuOpen.value = next
}
function toggleUser() {
  const next = !userMenuOpen.value
  closeMenus()
  userMenuOpen.value = next
}

// Close menus whenever the route changes (covers clicking a menu link).
watch(() => route.fullPath, closeMenus)

onMounted(() => {
  // Re-sync the cached profile (especially role) on every full load, so a
  // role change made in the backend takes effect without forcing a re-login.
  if (auth.isAuthenticated) auth.fetchMe()
})

function confirmLogout() {
  closeMenus()
  auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.nav-link {
  @apply px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-900 rounded-md;
}
.nav-link.active {
  @apply text-indigo-600 bg-indigo-50;
}
.dropdown {
  @apply absolute top-full mt-2 w-52 bg-white rounded-md shadow-lg border border-gray-200 py-1 z-50;
}
.menu-item {
  @apply block px-4 py-2 text-sm text-gray-700 hover:bg-gray-50;
}
.menu-item.active {
  @apply text-indigo-600 bg-indigo-50;
}
</style>
