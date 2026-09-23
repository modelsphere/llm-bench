<template>
  <div class="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="text-xl font-bold text-gray-900">{{ $t('nav.cardTypes') }}</h1>
        <p class="text-sm text-gray-500 mt-1">
          {{ $t('cardTypes.subtitle') }}
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

    <!-- Add a new card type -->
    <form
      class="mb-6 bg-white rounded-lg border border-gray-200 shadow-sm p-4 flex items-end gap-3"
      @submit.prevent="addCardType"
    >
      <div class="flex-1">
        <label for="new_name" class="block text-sm font-medium text-gray-700">{{ $t('adminBench.name') }}</label>
        <input
          id="new_name"
          v-model="newName"
          type="text"
          placeholder="e.g. H100"
          class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
        />
      </div>
      <div class="w-32">
        <label for="new_order" class="block text-sm font-medium text-gray-700">{{ $t('cardTypes.order') }}</label>
        <input
          id="new_order"
          v-model.number="newOrder"
          type="number"
          step="1"
          min="0"
          class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
        />
      </div>
      <button
        type="submit"
        :disabled="adding || !newName.trim()"
        class="py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
      >
        {{ $t('cardTypes.add') }}
      </button>
    </form>

    <div v-if="loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>

    <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
      <table class="min-w-full divide-y divide-gray-200 text-sm">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-4 py-2 text-left font-medium text-gray-500">{{ $t('adminBench.name') }}</th>
            <th class="px-4 py-2 text-left font-medium text-gray-500 w-40">{{ $t('cardTypes.displayOrder') }}</th>
            <th class="px-4 py-2 text-right font-medium text-gray-500 w-48">{{ $t('adminBench.actions') }}</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          <tr v-for="c in cardTypes" :key="c.id">
            <td class="px-4 py-2">
              <input
                v-model="c.name"
                type="text"
                class="block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
              />
            </td>
            <td class="px-4 py-2">
              <input
                v-model.number="c.display_order"
                type="number"
                step="1"
                min="0"
                class="block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm"
              />
            </td>
            <td class="px-4 py-2 text-right space-x-3">
              <button
                :disabled="busyId === c.id || !c.name.trim()"
                class="text-xs font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                @click="save(c)"
              >
                {{ $t('common.save') }}
              </button>
              <button
                :disabled="busyId === c.id"
                class="text-xs font-medium text-red-600 hover:text-red-800 disabled:opacity-50"
                @click="remove(c)"
              >
                {{ $t('common.delete') }}
              </button>
            </td>
          </tr>
          <tr v-if="cardTypes.length === 0">
            <td colspan="3" class="px-4 py-6 text-center text-gray-400">{{ $t('cardTypes.empty') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { cardTypesApi, type CardType } from '@/api/client'

const { t } = useI18n()
const cardTypes = ref<CardType[]>([])
const loading = ref(true)
const error = ref('')
const busyId = ref<number | null>(null)

const newName = ref('')
const newOrder = ref(0)
const adding = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await cardTypesApi.list()
    cardTypes.value = data.card_types
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('cardTypes.loadFailed')
  } finally {
    loading.value = false
  }
}

async function addCardType() {
  if (!newName.value.trim()) return
  adding.value = true
  error.value = ''
  try {
    await cardTypesApi.create({ name: newName.value.trim(), display_order: newOrder.value || 0 })
    newName.value = ''
    newOrder.value = 0
    await load()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('cardTypes.addFailed')
  } finally {
    adding.value = false
  }
}

async function save(c: CardType) {
  busyId.value = c.id
  error.value = ''
  try {
    await cardTypesApi.update(c.id, { name: c.name.trim(), display_order: c.display_order })
    await load()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('cardTypes.saveFailed')
  } finally {
    busyId.value = null
  }
}

async function remove(c: CardType) {
  if (!window.confirm(t('cardTypes.deleteConfirm', { name: c.name }))) return
  busyId.value = c.id
  error.value = ''
  try {
    await cardTypesApi.remove(c.id)
    await load()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('cardTypes.deleteFailed')
  } finally {
    busyId.value = null
  }
}

onMounted(load)
</script>
