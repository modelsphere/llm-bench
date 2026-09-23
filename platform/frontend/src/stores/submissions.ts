import { defineStore } from 'pinia'
import { i18n } from '@/i18n'
import { ref } from 'vue'
import { submissionsApi, type SubmissionDetail, type Submission } from '@/api/client'

export const useSubmissionsStore = defineStore('submissions', () => {
  const mySubmissions = ref<Submission[]>([])
  const currentSubmission = ref<SubmissionDetail | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const pollTimer = ref<ReturnType<typeof setTimeout> | null>(null)
  const isPolling = ref(false)

  async function fetchMine() {
    loading.value = true
    error.value = null
    try {
      const { data } = await submissionsApi.listMine()
      mySubmissions.value = data
    } catch (e: any) {
      error.value = e.response?.data?.detail || i18n.global.t('storeErrors.submissions')
    } finally {
      loading.value = false
    }
  }

  async function fetchOne(id: number) {
    loading.value = false
    error.value = null
    try {
      const { data } = await submissionsApi.get(id)
      currentSubmission.value = data
      return data
    } catch (e: any) {
      error.value = e.response?.data?.detail || i18n.global.t('storeErrors.submission')
      return null
    }
  }

  function startPolling(id: number, intervalMs = 3000) {
    stopPolling()
    isPolling.value = true
    fetchOne(id)

    const tick = async () => {
      if (!isPolling.value) return
      const data = await fetchOne(id)
      if (!isPolling.value) return
      if (data && ['done', 'failed', 'canceled'].includes(data.status)) {
        stopPolling()
        return
      }
      pollTimer.value = setTimeout(tick, intervalMs)
    }

    pollTimer.value = setTimeout(tick, intervalMs)
  }

  function stopPolling() {
    isPolling.value = false
    if (pollTimer.value !== null) {
      clearTimeout(pollTimer.value)
      pollTimer.value = null
    }
  }

  async function cancel(id: number) {
    await submissionsApi.cancel(id)
    await fetchOne(id)
  }

  async function refresh(id: number) {
    await fetchOne(id)
  }

  return { mySubmissions, currentSubmission, loading, error, fetchMine, fetchOne, startPolling, stopPolling, cancel, refresh }
})
