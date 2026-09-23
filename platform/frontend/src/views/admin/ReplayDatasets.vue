<template>
  <div class="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="text-xl font-bold text-gray-900">{{ $t('nav.replayDatasets') }}</h1>
        <p class="text-sm text-gray-500 mt-1">{{ $t('replayDs.subtitle') }}</p>
      </div>
      <div class="space-x-4">
        <button class="text-sm text-gray-500 hover:text-gray-700" :disabled="loading" @click="load">
          {{ $t('adminSubs.refresh') }}
        </button>
        <button
          class="py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
          @click="startCreate"
        >
          {{ $t('replayDs.newProfile') }}
        </button>
      </div>
    </div>

    <div v-if="error" class="mb-4 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">
      {{ error }}
    </div>
    <div
      v-if="!collectorConfigured && !loading"
      class="mb-4 bg-amber-50 border border-amber-200 text-amber-800 px-4 py-3 rounded text-sm"
    >
      {{ $t('replayDs.noCollector') }}
    </div>

    <div v-if="loading" class="text-center py-12 text-gray-500">{{ $t('common.loading') }}</div>

    <div v-else-if="profiles.length === 0" class="text-center py-12 text-gray-400">
      {{ $t('replayDs.empty') }}
    </div>

    <div v-else class="space-y-4">
      <div
        v-for="p in profiles"
        :key="p.id"
        class="bg-white rounded-lg border border-gray-200 shadow-sm"
      >
        <!-- Header row: identity, freshness, actions -->
        <div class="p-4 flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="font-semibold text-gray-900">{{ p.display_name }}</span>
              <code class="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">{{ p.name }}</code>
              <span
                v-if="!p.enabled"
                class="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500"
              >{{ $t('replayDs.disabled') }}</span>
            </div>
            <p v-if="p.description" class="text-sm text-gray-500 mt-1">{{ p.description }}</p>
            <p class="text-xs text-gray-500 mt-1">
              <span v-if="p.schedule_interval_hours > 0">
                {{ $t('replayDs.summaryLine', {
                  window: p.window_hours, samples: p.sample_size,
                  every: p.schedule_interval_hours,
                }) }}
              </span>
              <span v-else>
                {{ $t('replayDs.summaryLineManual', {
                  window: p.window_hours, samples: p.sample_size,
                }) }}
              </span>
            </p>
          </div>

          <div class="flex items-center gap-3">
            <span :class="['text-xs px-2 py-1 rounded-full font-medium', freshnessClass(p)]">
              {{ freshnessLabel(p) }}
            </span>
            <button
              class="text-xs font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
              :disabled="busyId === p.id || !collectorConfigured"
              @click="rebuild(p)"
            >
              {{ $t('replayDs.rebuild') }}
            </button>
            <button
              class="text-xs font-medium text-indigo-600 hover:text-indigo-800"
              @click="toggleBuilds(p)"
            >
              {{ expandedId === p.id ? $t('replayDs.hideBuilds') : $t('replayDs.showBuilds') }}
            </button>
            <button class="text-xs font-medium text-gray-600 hover:text-gray-800" @click="startEdit(p)">
              {{ $t('common.edit') }}
            </button>
            <button class="text-xs font-medium text-red-600 hover:text-red-800" @click="remove(p)">
              {{ $t('common.delete') }}
            </button>
          </div>
        </div>

        <!-- Current build -->
        <div v-if="p.current" class="px-4 pb-4 text-xs text-gray-600 space-y-2">
          <div class="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span>{{ $t('replayDs.currentBuild') }}: <code>{{ p.current.build_id }}</code></span>
            <span>{{ $t('replayDs.records') }}: {{ p.current.records.toLocaleString() }}</span>
            <span>{{ formatBytes(p.current.bytes) }}</span>
            <button class="text-indigo-600 hover:text-indigo-800" @click="copyPath(p.current!.path)">
              {{ copiedPath === p.current.path ? $t('replayDs.copied') : $t('replayDs.copyPath') }}
            </button>
          </div>

          <!-- What / when: the questions asked of a build nobody watched being made -->
          <dl class="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2">
            <div>
              <dt class="text-gray-400">{{ $t('replayDs.builtAt') }}</dt>
              <dd class="text-gray-700">
                {{ new Date(p.current.built_at).toLocaleString() }}
                <span class="text-gray-400">({{ $t('replayDs.agoShort', { hours: p.current.age_hours.toFixed(1) }) }})</span>
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">{{ $t('replayDs.collectedFrom') }}</dt>
              <dd class="text-gray-700">{{ shortRange(p.current.window_start, p.current.window_end) }}</dd>
            </div>
            <div>
              <dt class="text-gray-400">{{ $t('replayDs.model') }}</dt>
              <dd class="font-mono text-gray-700">{{ topKeys(p.current.summary?.models) }}</dd>
            </div>
            <div>
              <dt class="text-gray-400">{{ $t('replayDs.backend') }}</dt>
              <dd class="font-mono text-gray-700 break-all">{{ topKeys(p.current.summary?.forwarded_to) }}</dd>
            </div>
          </dl>

          <dl v-if="p.current.summary" class="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-2">
            <div v-if="p.current.summary.prompt_tokens">
              <dt class="text-gray-400">{{ $t('replayDs.inputLen') }}</dt>
              <dd class="font-mono text-gray-700">{{ spreadLine(p.current.summary.prompt_tokens) }}</dd>
            </div>
            <div v-if="p.current.summary.completion_tokens">
              <dt class="text-gray-400">{{ $t('replayDs.outputLen') }}</dt>
              <dd class="font-mono text-gray-700">{{ spreadLine(p.current.summary.completion_tokens) }}</dd>
            </div>
            <div v-if="p.current.summary.cache_hit_rate !== null && p.current.summary.cache_hit_rate !== undefined">
              <dt class="text-gray-400">{{ $t('replayDs.cacheHit') }}</dt>
              <dd class="text-gray-700">
                {{ (p.current.summary.cache_hit_rate * 100).toFixed(1) }}%
                <span class="text-gray-400">
                  ({{ (p.current.summary.total_cached_tokens ?? 0).toLocaleString() }} /
                  {{ (p.current.summary.total_prompt_tokens ?? 0).toLocaleString() }} tok)
                </span>
              </dd>
            </div>
          </dl>

          <div v-if="p.current.buckets">
            <span class="text-gray-400">{{ $t('replayDs.buckets') }}:</span>
            <span class="font-mono text-gray-500">{{ bucketLine(p.current.buckets) }}</span>
          </div>
          <p class="text-gray-400">{{ $t('replayDs.pinHint') }}</p>
        </div>
        <div v-else class="px-4 pb-4 text-xs text-amber-700">
          {{ $t('replayDs.noBuildYet') }}
        </div>

        <!-- Build history -->
        <div v-if="expandedId === p.id" class="border-t border-gray-100 px-4 py-3">
          <div v-if="buildsLoading" class="text-sm text-gray-500">{{ $t('common.loading') }}</div>
          <table v-else-if="builds.length" class="min-w-full text-xs">
            <thead class="text-gray-500">
              <tr>
                <th class="py-1 pr-4 text-left">{{ $t('replayDs.buildId') }}</th>
                <th class="py-1 pr-4 text-left">{{ $t('replayDs.status') }}</th>
                <th class="py-1 pr-4 text-left">{{ $t('replayDs.records') }}</th>
                <th class="py-1 pr-4 text-left">{{ $t('replayDs.window') }}</th>
                <th class="py-1 pr-4 text-left">{{ $t('replayDs.finished') }}</th>
                <th class="py-1 pr-4 text-left">{{ $t('replayDs.detail') }}</th>
                <th class="py-1 text-left">{{ $t('replayDs.action') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100">
              <tr v-for="b in builds" :key="b.id">
                <td class="py-1 pr-4 font-mono">{{ b.build_id || '—' }}</td>
                <td class="py-1 pr-4">
                  <span :class="statusClass(b.status)">{{ b.status }}</span>
                </td>
                <td class="py-1 pr-4">{{ b.records?.toLocaleString() ?? '—' }}</td>
                <td class="py-1 pr-4">{{ shortRange(b.window_start, b.window_end) }}</td>
                <td class="py-1 pr-4">{{ b.finished_at ? new Date(b.finished_at).toLocaleString() : '—' }}</td>
                <td class="py-1 pr-4 text-gray-500 max-w-md truncate" :title="b.error || b.progress || ''">
                  {{ b.error || b.progress || '' }}
                </td>
                <td class="py-1">
                  <div class="flex items-center gap-3">
                    <button
                      v-if="b.status === 'ready' && b.build_id"
                      class="font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                      :disabled="busyId === p.id || !collectorConfigured"
                      :title="collectorConfigured ? '' : $t('replayDs.noCollector')"
                      @click="freezeBuild(p, b)"
                    >
                      {{ $t('replayDs.freeze') }}
                    </button>
                    <!-- A plain link, so the browser streams the file to disk
                         rather than the app holding hundreds of MB in memory.
                         Served by the backend off the datasets volume, so it
                         works even when the collector is down. -->
                    <a
                      v-if="b.status === 'ready' && b.build_id && b.downloadable"
                      class="font-medium text-indigo-600 hover:text-indigo-800"
                      :href="downloadUrl(p, b)"
                      :title="downloadTitle(b)"
                      download
                    >
                      {{ $t('replayDs.download') }}
                    </a>
                    <span
                      v-else-if="b.status === 'ready' && b.downloadable === false"
                      class="text-gray-400"
                      :title="$t('replayDs.prunedHint')"
                    >
                      {{ $t('replayDs.pruned') }}
                    </span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="text-sm text-gray-400">{{ $t('replayDs.noBuilds') }}</div>
        </div>
      </div>
    </div>

    <!-- Frozen datasets -->
    <div v-if="!loading" class="mt-10">
      <div class="flex items-center justify-between mb-3">
        <div>
          <h2 class="text-lg font-semibold text-gray-900">{{ $t('replayDs.frozenTitle') }}</h2>
          <p class="text-sm text-gray-500">{{ $t('replayDs.frozenSubtitle') }}</p>
        </div>
        <button class="text-sm text-gray-500 hover:text-gray-700" :disabled="frozenLoading" @click="loadFrozen">
          {{ $t('adminSubs.refresh') }}
        </button>
      </div>

      <div
        v-if="frozen.length === 0"
        class="text-sm text-gray-400 py-8 text-center border border-dashed border-gray-200 rounded-lg"
      >
        {{ $t('replayDs.frozenEmpty') }}
      </div>
      <div v-else class="bg-white rounded-lg border border-gray-200 shadow-sm overflow-x-auto">
        <table class="min-w-full text-xs">
          <thead class="text-gray-500 bg-gray-50">
            <tr>
              <th class="py-2 px-4 text-left">{{ $t('replayDs.fName') }}</th>
              <th class="py-2 px-4 text-left">{{ $t('replayDs.frozenSource') }}</th>
              <th class="py-2 px-4 text-left">{{ $t('replayDs.records') }}</th>
              <th class="py-2 px-4 text-left">{{ $t('replayDs.frozenSize') }}</th>
              <th class="py-2 px-4 text-left">{{ $t('replayDs.frozenAt') }}</th>
              <th class="py-2 px-4 text-left">{{ $t('replayDs.frozenPath') }}</th>
              <th class="py-2 px-4"></th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr v-for="f in frozen" :key="f.name">
              <td class="py-2 px-4">
                <div class="font-medium text-gray-800">{{ f.name }}</div>
                <div v-if="f.used_by.length" class="text-amber-700 mt-0.5">
                  {{ $t('replayDs.frozenUsedBy', { benchmarks: f.used_by.join(', ') }) }}
                </div>
              </td>
              <td class="py-2 px-4 font-mono text-gray-600">
                <span v-if="f.source_profile">{{ f.source_profile }}</span>
                <span v-if="f.source_build_id" class="text-gray-400"> / {{ f.source_build_id }}</span>
                <span v-if="!f.source_profile && !f.source_build_id">—</span>
              </td>
              <td class="py-2 px-4">{{ f.records ? f.records.toLocaleString() : '—' }}</td>
              <td class="py-2 px-4">{{ formatBytes(f.bytes) }}</td>
              <td class="py-2 px-4 text-gray-600">
                {{ f.frozen_at ? new Date(f.frozen_at).toLocaleString() : '—' }}
              </td>
              <td class="py-2 px-4">
                <button class="text-indigo-600 hover:text-indigo-800" @click="copyPath(f.path)">
                  {{ copiedPath === f.path ? $t('replayDs.copied') : $t('replayDs.copyPath') }}
                </button>
              </td>
              <td class="py-2 px-4 text-right">
                <button class="font-medium text-red-600 hover:text-red-800" @click="removeFrozen(f)">
                  {{ $t('common.delete') }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Editor -->
    <div v-if="editing" class="fixed inset-0 bg-black/40 flex items-start justify-center overflow-y-auto p-4 z-50">
      <div class="bg-white rounded-lg shadow-xl w-full max-w-3xl my-8">
        <div class="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
          <h2 class="font-semibold text-gray-900">
            {{ editing.id ? $t('replayDs.editProfile') : $t('replayDs.newProfile') }}
          </h2>
          <button class="text-gray-400 hover:text-gray-600" @click="editing = null">✕</button>
        </div>

        <div class="px-6 py-4 space-y-5 text-sm">
          <div v-if="formError" class="bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded">
            {{ formError }}
          </div>

          <!-- identity -->
          <div class="grid grid-cols-2 gap-4">
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fName') }}</span>
              <input
                v-model="editing.name" :disabled="!!editing.id" type="text" placeholder="glm5-daily"
                class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm disabled:bg-gray-100"
              />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fNameHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fDisplayName') }}</span>
              <input v-model="editing.display_name" type="text"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
            </label>
          </div>
          <label class="block">
            <span class="font-medium text-gray-700">{{ $t('replayDs.fDescription') }}</span>
            <input v-model="editing.description" type="text"
                   class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
          </label>
          <label class="flex items-center gap-2">
            <input v-model="editing.enabled" type="checkbox" class="rounded border-gray-300" />
            <span class="text-gray-700">{{ $t('replayDs.fEnabled') }}</span>
          </label>

          <!-- source -->
          <fieldset class="border border-gray-200 rounded-md p-4 space-y-4">
            <legend class="px-1 text-xs font-semibold text-gray-500 uppercase">
              {{ $t('replayDs.sectionSource') }}
            </legend>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fSourceType') }}</span>
              <select v-model="editing.source_type"
                      class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm">
                <option value="bodylog_files">{{ $t('replayDs.sourceBodylogFiles') }}</option>
                <option value="victorialogs">{{ $t('replayDs.sourceVictoriaLogs') }}</option>
              </select>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fSourceUrl') }}</span>
              <input v-model="editing.source_url" type="text"
                     :placeholder="editing.source_type === 'victorialogs' ? 'http://victorialogs:9428' : 'file:///data/bodylog'"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm font-mono" />
              <span class="text-xs text-gray-500">{{
                editing.source_type === 'victorialogs' ? $t('replayDs.fSourceUrlHintVl') : $t('replayDs.fSourceUrlHintFiles')
              }}</span>
            </label>
            <div class="grid grid-cols-2 gap-4">
              <label class="block">
                <span class="font-medium text-gray-700">{{ $t('replayDs.fModels') }}</span>
                <input v-model="modelsText" type="text" placeholder="glm-5"
                       class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm font-mono" />
                <span class="text-xs text-gray-500">{{ $t('replayDs.fModelsHint') }}</span>
              </label>
              <label class="block">
                <span class="font-medium text-gray-700">{{ $t('replayDs.fForwardedTo') }}</span>
                <input v-model="forwardedText" type="text" placeholder="http://198.51.100.20:8052"
                       class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm font-mono" />
                <span class="text-xs text-gray-500">{{ $t('replayDs.fForwardedToHint') }}</span>
              </label>
              <label class="block">
                <span class="font-medium text-gray-700">{{ $t('replayDs.fStatuses') }}</span>
                <input v-model="statusesText" type="text" placeholder="200"
                       class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm font-mono" />
              </label>
              <label class="block">
                <span class="font-medium text-gray-700">{{ $t('replayDs.fUris') }}</span>
                <input v-model="urisText" type="text" placeholder="/v1/chat/completions"
                       class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm font-mono" />
              </label>
            </div>
            <label v-if="editing.source_type === 'victorialogs'" class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fExtra') }}</span>
              <input v-model="editing.extra_logsql" type="text" placeholder='req_headers.x-app:="cli"'
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm font-mono" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fExtraHint') }}</span>
            </label>
            <label class="flex items-center gap-2">
              <input v-model="editing.exclude_truncated" type="checkbox" class="rounded border-gray-300" />
              <span class="text-gray-700">{{ $t('replayDs.fExcludeTruncated') }}</span>
            </label>
            <div v-if="editing.id" class="pt-1">
              <button
                class="text-xs font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
                :disabled="probing" @click="runProbe"
              >
                {{ probing ? $t('replayDs.probing') : $t('replayDs.probe') }}
              </button>
              <div v-if="probeResult" class="mt-2 text-xs bg-gray-50 border border-gray-200 rounded p-2">
                <div v-if="probeResult.error" class="text-red-600">{{ probeResult.error }}</div>
                <template v-else>
                  <div :class="probeResult.matched ? 'text-green-700' : 'text-red-600'">
                    {{ $t('replayDs.probeMatched', { n: probeResult.matched }) }}
                  </div>
                  <code class="block mt-1 text-gray-500 break-all">{{ probeResult.query }}</code>
                </template>
              </div>
            </div>
          </fieldset>

          <!-- window + sampling -->
          <fieldset class="border border-gray-200 rounded-md p-4 grid grid-cols-3 gap-4">
            <legend class="px-1 text-xs font-semibold text-gray-500 uppercase">
              {{ $t('replayDs.sectionWindow') }}
            </legend>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fWindowHours') }}</span>
              <input v-model.number="editing.window_hours" type="number" min="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fWindowHoursHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fSubwindow') }}</span>
              <input v-model.number="editing.subwindow_minutes" type="number" min="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fSubwindowHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fTimezone') }}</span>
              <input v-model="editing.window_timezone" type="text"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fSampleSize') }}</span>
              <input v-model.number="editing.sample_size" type="number" min="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fOversample') }}</span>
              <input v-model.number="editing.oversample_factor" type="number" min="1" step="0.5"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fOversampleHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMaxCarry') }}</span>
              <input v-model.number="editing.max_carry_multiple" type="number" min="1" step="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fMaxCarryHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMaxGb') }}</span>
              <input v-model.number="maxGb" type="number" min="0.1" step="0.5"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
            </label>
            <!-- The per-slice cap is otherwise invisible: make the arithmetic explicit. -->
            <div v-if="sliceCap" class="col-span-3 text-xs bg-gray-50 border border-gray-200 rounded p-2 text-gray-600">
              {{ $t('replayDs.sliceCapLine', {
                slices: sliceCap.slices, base: sliceCap.baseQuota,
                mult: Math.max(1, editing.max_carry_multiple || 1), cap: sliceCap.cap,
              }) }}
              <span :class="sliceCap.minSlices > sliceCap.slices ? 'text-amber-700' : 'text-gray-500'">
                {{ $t('replayDs.sliceCapReach', { need: sliceCap.minSlices }) }}
              </span>
            </div>
          </fieldset>

          <!-- sanitation + gates -->
          <fieldset class="border border-gray-200 rounded-md p-4 grid grid-cols-3 gap-4">
            <legend class="px-1 text-xs font-semibold text-gray-500 uppercase">
              {{ $t('replayDs.sectionClean') }}
            </legend>
            <label class="flex items-center gap-2 col-span-3">
              <input v-model="editing.clean" type="checkbox" class="rounded border-gray-300" />
              <span class="text-gray-700">{{ $t('replayDs.fClean') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMaxModelLen') }}</span>
              <input v-model.number="editing.max_model_len" type="number" min="2048"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fMaxModelLenHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMinRecords') }}</span>
              <input v-model.number="editing.min_records" type="number" min="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fMinRecordsHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMinBuckets') }}</span>
              <input v-model.number="editing.min_buckets" type="number" min="0" max="7"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fMinBucketsHint') }}</span>
            </label>
            <label class="flex items-center gap-2 col-span-3">
              <input v-model="editing.keep_response_body" type="checkbox" class="rounded border-gray-300" />
              <span class="text-gray-700">{{ $t('replayDs.fKeepRespBody') }}</span>
            </label>
            <label class="flex items-center gap-2 col-span-3">
              <input v-model="editing.compress" type="checkbox" class="rounded border-gray-300" />
              <span class="text-gray-700">{{ $t('replayDs.fCompress') }}</span>
            </label>
          </fieldset>

          <!-- schedule + retention -->
          <fieldset class="border border-gray-200 rounded-md p-4 grid grid-cols-4 gap-4">
            <legend class="px-1 text-xs font-semibold text-gray-500 uppercase">
              {{ $t('replayDs.sectionSchedule') }}
            </legend>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fInterval') }}</span>
              <input v-model.number="editing.schedule_interval_hours" type="number" min="0" step="0.25"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fIntervalHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fAnchorHour') }}</span>
              <input v-model.number="anchorHour" type="number" min="-1" max="23"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fAnchorHourHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMaxAge') }}</span>
              <input v-model.number="editing.max_age_hours" type="number" min="0" step="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fMaxAgeHint') }}</span>
            </label>
            <label class="block">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fKeepBuilds') }}</span>
              <input v-model.number="editing.keep_builds" type="number" min="1"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
            </label>
            <label class="block col-span-2">
              <span class="font-medium text-gray-700">{{ $t('replayDs.fMinRetain') }}</span>
              <input v-model.number="editing.min_retain_hours" type="number" min="0"
                     class="mt-1 block w-full rounded-md border-gray-300 shadow-sm sm:text-sm" />
              <span class="text-xs text-gray-500">{{ $t('replayDs.fMinRetainHint') }}</span>
            </label>
          </fieldset>
        </div>

        <div class="px-6 py-4 border-t border-gray-200 flex justify-end gap-3">
          <button class="text-sm text-gray-600 hover:text-gray-800" @click="editing = null">
            {{ $t('common.cancel') }}
          </button>
          <button
            class="py-2 px-4 rounded-md text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
            :disabled="saving" @click="save"
          >
            {{ $t('common.save') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  replayDatasetsApi,
  type FrozenDataset,
  type ReplayDatasetBuild,
  type ReplayDatasetProfile,
} from '@/api/client'
import { copyToClipboard } from '@/utils/clipboard'

const { t } = useI18n()

const profiles = ref<ReplayDatasetProfile[]>([])
const collectorConfigured = ref(true)
const loading = ref(true)
const error = ref('')
const busyId = ref<number | null>(null)
const copiedPath = ref('')

const frozen = ref<FrozenDataset[]>([])
const frozenLoading = ref(false)

const expandedId = ref<number | null>(null)
const builds = ref<ReplayDatasetBuild[]>([])
const buildsLoading = ref(false)

const editing = ref<any | null>(null)
const saving = ref(false)
const formError = ref('')
const probing = ref(false)
const probeResult = ref<any | null>(null)

// Comma-separated text bindings for the list fields — one input is far easier
// to use than a tag widget, and the API trims/drops blanks anyway.
function listBinding(key: string) {
  return computed({
    get: () => (editing.value?.[key] || []).join(', '),
    set: (v: string) => {
      if (editing.value) editing.value[key] = v.split(',').map(s => s.trim()).filter(Boolean)
    },
  })
}
const modelsText = listBinding('models')
const forwardedText = listBinding('forwarded_to')
const statusesText = listBinding('statuses')
const urisText = listBinding('uris')

const maxGb = computed({
  get: () => Number(((editing.value?.max_bytes ?? 0) / 1024 ** 3).toFixed(2)),
  set: (v: number) => { if (editing.value) editing.value.max_bytes = Math.round(v * 1024 ** 3) },
})
// -1 in the input means "no anchor" (build whenever the interval elapses).
const anchorHour = computed({
  get: () => (editing.value?.schedule_anchor_hour ?? -1),
  set: (v: number) => {
    if (editing.value) editing.value.schedule_anchor_hour = v < 0 || v > 23 ? null : v
  },
})

// The effective per-slice keep cap, computed exactly as the builder does:
// base_quota = ceil(sample_size / n_slices); a single slice keeps at most
// base_quota * max_carry_multiple. Surfaced so the ceiling that actually limits a
// bursty model (traffic in a few slices) is visible instead of implicit.
const sliceCap = computed(() => {
  const e = editing.value
  if (!e || !e.sample_size || !e.window_hours || !e.subwindow_minutes) return null
  const slices = Math.max(1, Math.ceil((e.window_hours * 60) / Math.max(1, e.subwindow_minutes)))
  const baseQuota = Math.max(1, Math.ceil(e.sample_size / slices))
  const cap = baseQuota * Math.max(1, e.max_carry_multiple || 1)
  const minSlices = Math.ceil(e.sample_size / cap)
  return { slices, baseQuota, cap, minSlices }
})

function blankProfile() {
  return {
    id: 0, name: '', display_name: '', description: '', enabled: true,
    source_type: 'bodylog_files', source_url: '', models: [], statuses: ['200'], forwarded_to: [],
    uris: ['/v1/chat/completions'], exclude_truncated: true, extra_logsql: '',
    window_hours: 24, window_timezone: 'UTC', subwindow_minutes: 60,
    sample_size: 2000, oversample_factor: 3, max_carry_multiple: 4, max_bytes: 8 * 1024 ** 3,
    clean: true, max_model_len: 262144, keep_response_body: false, compress: true,
    min_records: 100, min_buckets: 1,
    schedule_interval_hours: 24, schedule_anchor_hour: null,
    max_age_hours: 48, keep_builds: 7, min_retain_hours: 8,
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await replayDatasetsApi.listProfiles()
    profiles.value = data.profiles
    collectorConfigured.value = data.collector_configured
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.loadFailed')
  } finally {
    loading.value = false
  }
}

function startCreate() {
  probeResult.value = null
  formError.value = ''
  editing.value = blankProfile()
}

function startEdit(p: ReplayDatasetProfile) {
  probeResult.value = null
  formError.value = ''
  editing.value = { ...p }
}

async function save() {
  if (!editing.value) return
  saving.value = true
  formError.value = ''
  const payload: any = { ...editing.value }
  delete payload.id; delete payload.created_at; delete payload.updated_at
  delete payload.current; delete payload.last_build
  try {
    if (editing.value.id) {
      delete payload.name  // immutable: it names the directory the builds live in
      await replayDatasetsApi.updateProfile(editing.value.id, payload)
    } else {
      await replayDatasetsApi.createProfile(payload)
    }
    editing.value = null
    await load()
  } catch (e: any) {
    const detail = e.response?.data?.detail
    formError.value = Array.isArray(detail)
      ? detail.map((d: any) => `${d.loc?.slice(-1)}: ${d.msg}`).join('; ')
      : (detail || t('replayDs.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function remove(p: ReplayDatasetProfile) {
  if (!window.confirm(t('replayDs.deleteConfirm', { name: p.display_name }))) return
  busyId.value = p.id
  try {
    await replayDatasetsApi.removeProfile(p.id)
    await load()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.deleteFailed')
  } finally {
    busyId.value = null
  }
}

async function rebuild(p: ReplayDatasetProfile) {
  busyId.value = p.id
  error.value = ''
  try {
    await replayDatasetsApi.triggerBuild(p.id)
    if (expandedId.value === p.id) await loadBuilds(p.id)
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.rebuildFailed')
  } finally {
    busyId.value = null
  }
}

async function toggleBuilds(p: ReplayDatasetProfile) {
  if (expandedId.value === p.id) { expandedId.value = null; return }
  expandedId.value = p.id
  await loadBuilds(p.id)
}

async function loadBuilds(id: number) {
  buildsLoading.value = true
  builds.value = []
  try {
    const { data } = await replayDatasetsApi.listBuilds(id)
    builds.value = data.builds
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.loadFailed')
  } finally {
    buildsLoading.value = false
  }
}

async function loadFrozen() {
  frozenLoading.value = true
  try {
    const { data } = await replayDatasetsApi.listFrozen()
    frozen.value = data.frozen
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.loadFailed')
  } finally {
    frozenLoading.value = false
  }
}

// A sensible default the admin can accept: profile + build id, slugified (the
// server only accepts a lowercase slug).
function suggestFrozenName(p: ReplayDatasetProfile, b: ReplayDatasetBuild): string {
  return `${p.name}-${b.build_id || ''}`
    .toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64)
}

async function freezeBuild(p: ReplayDatasetProfile, b: ReplayDatasetBuild) {
  const raw = window.prompt(t('replayDs.freezePrompt', { build: b.build_id }), suggestFrozenName(p, b))
  if (raw === null) return
  const name = raw.trim().toLowerCase()
  if (!name) return
  busyId.value = p.id
  error.value = ''
  try {
    await replayDatasetsApi.freezeBuild(p.id, b.id, name)
    await loadFrozen()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.freezeFailed')
  } finally {
    busyId.value = null
  }
}

async function removeFrozen(f: FrozenDataset) {
  // A frozen dataset a benchmark still points at can't be removed without force
  // — deleting it would make that benchmark silently replay the default dataset.
  const inUse = f.used_by.length > 0
  const msg = inUse
    ? t('replayDs.frozenInUseConfirm', { name: f.name, benchmarks: f.used_by.join(', ') })
    : t('replayDs.frozenDeleteConfirm', { name: f.name })
  if (!window.confirm(msg)) return
  error.value = ''
  try {
    await replayDatasetsApi.removeFrozen(f.name, inUse)
    await loadFrozen()
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('replayDs.deleteFailed')
  }
}

async function runProbe() {
  if (!editing.value?.id) return
  probing.value = true
  probeResult.value = null
  try {
    const { data } = await replayDatasetsApi.probe(editing.value.id, 1)
    probeResult.value = data
  } catch (e: any) {
    probeResult.value = { error: e.response?.data?.detail || t('replayDs.probeFailed') }
  } finally {
    probing.value = false
  }
}

async function copyPath(path: string) {
  // Via the shared helper, not navigator.clipboard directly: that API only
  // exists on HTTPS/localhost, and the platform is served over plain HTTP
  // in-cluster, where a bare writeText() throws.
  if (await copyToClipboard(path)) {
    copiedPath.value = path
    setTimeout(() => { if (copiedPath.value === path) copiedPath.value = '' }, 2000)
  } else {
    error.value = t('replayDs.copyFailed')
  }
}

function freshnessClass(p: ReplayDatasetProfile) {
  if (!p.current) return 'bg-red-100 text-red-700'
  return p.current.stale ? 'bg-amber-100 text-amber-800' : 'bg-green-100 text-green-700'
}

function freshnessLabel(p: ReplayDatasetProfile) {
  if (!p.current) return t('replayDs.freshNone')
  if (p.current.stale) return t('replayDs.freshStale', { hours: p.current.age_hours.toFixed(1) })
  return t('replayDs.freshOk', { hours: p.current.age_hours.toFixed(1) })
}

function statusClass(status: string) {
  if (status === 'ready') return 'text-green-700'
  if (status === 'failed') return 'text-red-600'
  if (status === 'running') return 'text-indigo-600'
  return 'text-gray-500'
}

// "glm-5" for a clean single-value build; "glm-5 +2 more" when a profile's
// filters let several through — which is itself worth seeing at a glance.
function topKeys(counts?: Record<string, number> | null): string {
  const entries = Object.entries(counts || {})
  if (!entries.length) return '—'
  entries.sort((a, b) => b[1] - a[1])
  const head = entries[0][0]
  return entries.length === 1 ? head : `${head} +${entries.length - 1} more`
}

function spreadLine(s?: { p50: number; p90: number; max: number; mean: number } | null): string {
  if (!s) return '—'
  const n = (v: number) => v.toLocaleString()
  return `p50 ${n(s.p50)} · p90 ${n(s.p90)} · max ${n(s.max)} · avg ${n(s.mean)}`
}

function bucketLine(buckets: Record<string, number>) {
  return Object.entries(buckets).map(([k, v]) => `${k}=${v}`).join('  ')
}

// The link the browser follows to fetch a build's dataset file. Carries the
// session token as a query param because an <a download> cannot set headers.
function downloadUrl(p: ReplayDatasetProfile, b: ReplayDatasetBuild) {
  return replayDatasetsApi.buildDownloadUrl(p.id, b.id)
}

function downloadTitle(b: ReplayDatasetBuild) {
  return t('replayDs.downloadHint', {
    size: b.size_bytes ? formatBytes(b.size_bytes) : '—',
  })
}

function formatBytes(n: number) {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}

function shortRange(a?: string | null, b?: string | null) {
  if (!a || !b) return '—'
  const fmt = (s: string) => new Date(s).toLocaleString(undefined, {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
  return `${fmt(a)} → ${fmt(b)}`
}

onMounted(() => { load(); loadFrozen() })
</script>
