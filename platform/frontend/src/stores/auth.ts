import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api/client'

interface User {
  id: number
  email: string
  username: string
  role: 'super_admin' | 'admin' | 'service' | 'user'
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('token'))
  const user = ref<User | null>(JSON.parse(localStorage.getItem('user') || 'null'))

  const isAuthenticated = computed(() => !!token.value)
  // super_admin is strictly above admin, so it passes every admin gate.
  const isAdmin = computed(() => user.value?.role === 'admin' || user.value?.role === 'super_admin')
  const isSuperAdmin = computed(() => user.value?.role === 'super_admin')

  function setAuth(newToken: string, newUser: User) {
    token.value = newToken
    user.value = newUser
    localStorage.setItem('token', newToken)
    localStorage.setItem('user', JSON.stringify(newUser))
  }

  // Replace just the token, keeping the current user. Used after change-password,
  // which invalidates older tokens and hands back a fresh one for this session.
  function setToken(newToken: string) {
    token.value = newToken
    localStorage.setItem('token', newToken)
  }

  async function fetchMe() {
    if (!token.value) return
    try {
      const { data } = await authApi.me()
      user.value = { id: data.id, email: data.email, username: data.username, role: data.role as User['role'] }
      localStorage.setItem('user', JSON.stringify(user.value))
    } catch {
      logout()
    }
  }

  async function login(email: string, password: string) {
    const { data } = await authApi.login({ email, password })
    // Save token first so me() request can attach it
    setAuth(data.access_token, { id: 0, email, username: '', role: data.role as User['role'] })
    // Fetch full user profile after token is saved
    const { data: meData } = await authApi.me()
    user.value = { id: meData.id, email: meData.email, username: meData.username, role: meData.role as User['role'] }
  }

  async function register(email: string, username: string, password: string) {
    const { data } = await authApi.register({ email, username, password })
    // Save token first so the me() request can attach it (mirrors login()).
    // Otherwise me() fires unauthenticated → 401 → interceptor bounces the
    // brand-new user to /login on signup.
    setAuth(data.access_token, { id: 0, email, username, role: data.role as User['role'] })
    const { data: meData } = await authApi.me()
    user.value = { id: meData.id, email: meData.email, username: meData.username, role: meData.role as User['role'] }
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  return { token, user, isAuthenticated, isAdmin, isSuperAdmin, setToken, login, register, logout, fetchMe }
})
