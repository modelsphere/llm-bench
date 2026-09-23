<template>
  <div class="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="mb-6">
      <router-link to="/admin/benchmarks" class="text-sm text-gray-500 hover:text-gray-700">{{ $t('common.back') }}</router-link>
    </div>

    <div class="bg-white rounded-lg border border-gray-200 shadow-sm p-6">
      <!-- Header with lock state and hash -->
      <div class="flex items-center justify-between mb-6">
        <h1 class="text-xl font-bold text-gray-900">{{ isEdit ? $t('editor.titleEdit') : (cloneSource ? $t('editor.titleClone') : $t('editor.titleCreate')) }}</h1>
        <div v-if="isEdit" class="flex items-center gap-3">
          <span
            v-if="benchmarkConfigHash"
            class="font-mono text-xs px-2 py-1 bg-gray-100 text-gray-500 rounded"
            :title="$t('benchDetail.configTip')"
          >{{ benchmarkConfigHash }}</span>
          <button
            type="button"
            :disabled="locking"
            @click="handleToggleLock"
            class="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded border"
            :class="benchmarkIsLocked
              ? 'border-blue-300 bg-blue-50 text-blue-700 hover:bg-blue-100'
              : 'border-gray-300 bg-white text-gray-600 hover:bg-gray-50'"
          >
            {{ benchmarkIsLocked ? $t('editor.lockedBtn') : $t('editor.unlockedBtn') }}
          </button>
        </div>
      </div>

      <!-- Lock notice -->
      <div v-if="benchmarkIsLocked" class="mb-4 flex items-start gap-2 bg-blue-50 border border-blue-200 text-blue-800 px-4 py-3 rounded text-sm">
        <span>🔒</span>
        <span>{{ $t('editor.lockedNotice') }}</span>
      </div>

      <form class="space-y-6" @submit.prevent="handleSubmit">
        <div v-if="error" class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded text-sm">{{ error }}</div>

        <!-- Validation summary (top). Mirrored above the submit button. -->
        <div v-if="validationErrors.length" class="bg-amber-50 border border-amber-200 text-amber-800 px-4 py-3 rounded text-sm">
          <p class="font-medium mb-1">{{ $t('editor.fixIssues', { n: validationErrors.length }) }}</p>
          <ul class="list-disc list-inside space-y-0.5">
            <li v-for="(v, idx) in validationErrors" :key="idx">
              <button
                v-if="v.moduleUid != null"
                type="button"
                class="text-left underline decoration-dotted hover:text-amber-900"
                @click="jumpToModule(v.moduleUid)"
              >{{ v.msg }}</button>
              <span v-else>{{ v.msg }}</span>
            </li>
          </ul>
        </div>

        <!-- Advisory warnings (top). Non-blocking: distinct yellow styling and
             wording so they read clearly as "review this" rather than "fix before
             saving". Mirrored above the submit button. -->
        <div v-if="metricConfigWarnings.length" class="bg-yellow-50 border border-yellow-300 text-yellow-800 px-4 py-3 rounded text-sm">
          <p class="font-medium mb-1">⚠ {{ $t('editor.dupWarn', { n: metricConfigWarningCount }) }}</p>
          <ul class="list-disc list-inside space-y-0.5">
            <li v-for="(w, idx) in metricConfigWarnings" :key="idx">
              <button
                v-if="w.moduleUid != null"
                type="button"
                class="text-left underline decoration-dotted hover:text-yellow-900"
                @click="jumpToModule(w.moduleUid)"
              >{{ w.label }}: {{ w.names.join(', ') }}</button>
              <span v-else>{{ w.label }}: {{ w.names.join(', ') }}</span>
            </li>
          </ul>
        </div>

        <div v-if="cloneSource" class="flex items-start gap-2 bg-indigo-50 border border-indigo-200 text-indigo-800 px-4 py-3 rounded text-sm">
          <span>📋</span>
          <span>
            Cloning from <span class="font-mono font-medium">{{ cloneSource }}</span>.
            <span v-html="$t('editor.cloneNotice')"></span>
          </span>
        </div>

        <!-- Basic info -->
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700">{{ $t('editor.benchName') }}</label>
            <input v-model="form.name" type="text" required class="mt-1 block w-full rounded-md border-gray-300 shadow-sm" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700">{{ $t('editor.slug') }}</label>
            <input v-model="form.slug" type="text" required pattern="[a-z0-9-]+" :disabled="isEdit" :class="isEdit ? 'bg-gray-100' : ''" class="mt-1 block w-full rounded-md border-gray-300 shadow-sm" />
          </div>
        </div>

        <div>
          <label class="block text-sm font-medium text-gray-700">{{ $t('subDetail.description') }}</label>
          <textarea v-model="form.description" rows="2" class="mt-1 block w-full rounded-md border-gray-300 shadow-sm"></textarea>
        </div>

        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700">{{ $t('subDetail.status') }}</label>
            <select v-model="form.status" class="mt-1 rounded-md border-gray-300 shadow-sm">
              <option value="draft">{{ $t('adminBench.draft') }}</option>
              <option value="active">{{ $t('benchList.active') }}</option>
            </select>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700">
              {{ $t('editor.version') }}
              <span class="ml-1 text-xs text-gray-400 font-normal">{{ $t('editor.versionHint') }}</span>
            </label>
            <input
              v-model="form.version"
              type="text"
              required
              :disabled="benchmarkIsLocked"
              :class="benchmarkIsLocked ? 'bg-gray-100 cursor-not-allowed' : ''"
              class="mt-1 block w-full rounded-md border-gray-300 shadow-sm text-sm"
            />
          </div>
        </div>

        <!-- Group tags (display grouping on the benchmarks list) -->
        <div>
          <div class="flex items-center justify-between">
            <label class="block text-sm font-medium text-gray-700">
              {{ $t('editor.groupTags') }}
              <span class="text-xs text-gray-400 ml-1">{{ $t('submit.optional') }}</span>
            </label>
            <button
              type="button"
              @click="addGroupTag"
              class="text-sm text-indigo-600 hover:text-indigo-500"
            >{{ $t('editor.addGroupTag') }}</button>
          </div>
          <p class="mt-1 text-xs text-gray-500">{{ $t('editor.groupTagsHint') }}</p>
          <div v-if="groupTagRows.length" class="mt-2 space-y-2">
            <div
              v-for="(row, ri) in groupTagRows"
              :key="row._uid"
              class="flex flex-wrap sm:flex-nowrap items-center gap-2"
            >
              <template v-for="(_, li) in row.levels" :key="li">
                <span v-if="li > 0" class="text-gray-300 select-none">/</span>
                <input
                  v-model="row.levels[li]"
                  type="text"
                  :maxlength="GROUP_TAG_SEGMENT_MAX"
                  :disabled="li > 0 && !row.levels[li - 1].trim()"
                  :placeholder="$t(['editor.groupTagTop', 'editor.groupTagMid', 'editor.groupTagLow'][li])"
                  :list="`group-tag-options-${row._uid}-${li}`"
                  :title="li > 0 && !row.levels[li - 1].trim() ? $t('editor.groupTagFillAbove') : ''"
                  class="block w-full sm:w-48 rounded-md border-gray-300 shadow-sm text-sm disabled:bg-gray-100 disabled:cursor-not-allowed"
                  @input="onGroupLevelInput(row, li)"
                />
                <datalist :id="`group-tag-options-${row._uid}-${li}`">
                  <option v-for="opt in groupLevelOptions(row, li)" :key="opt" :value="opt" />
                </datalist>
              </template>
              <button
                type="button"
                @click="groupTagRows.splice(ri, 1)"
                class="text-xs text-red-500 hover:text-red-700 shrink-0"
                :title="$t('editor.removeGroupTag')"
              >{{ $t('editor.removeGroupTag') }}</button>
            </div>
          </div>
        </div>

        <!-- Modules -->
        <div>
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-3">
              <h3 class="text-sm font-medium text-gray-700">
                Modules <span class="text-xs text-gray-400 font-normal">({{ form.modules.length }})</span>
              </h3>
              <button
                v-if="form.modules.length > 1"
                type="button"
                @click="setAllCollapsed(!allCollapsed)"
                class="text-xs text-gray-500 hover:text-gray-700"
              >{{ allCollapsed ? $t('editor.expandAll') : $t('editor.collapseAll') }}</button>
            </div>
            <button
              v-if="!benchmarkIsLocked"
              type="button"
              @click="addModule"
              class="text-sm text-indigo-600 hover:text-indigo-500"
            >{{ $t('editor.addModule') }}</button>
            <span v-else class="text-xs text-gray-400">{{ $t('editor.unlockToEdit') }}</span>
          </div>

          <p v-if="!benchmarkIsLocked && form.modules.length > 1" class="mb-2 text-xs text-gray-400">
            Drag <span class="font-mono">⠿</span> to reorder · click a header to fold/unfold.
          </p>

          <draggable
            v-model="form.modules"
            item-key="_uid"
            handle=".module-drag-handle"
            :disabled="benchmarkIsLocked"
            :animation="150"
            ghost-class="drag-ghost"
            class="space-y-4"
            @end="onReorder"
          >
            <template #item="{ element: mod, index: i }">
              <div
                :id="`module-card-${mod._uid}`"
                class="border rounded-lg transition-shadow"
                :class="justAddedUid === mod._uid ? 'border-indigo-400 ring-2 ring-indigo-300 shadow' : 'border-gray-200'"
              >
                <!-- Module header (drag handle · collapse toggle · summary · remove) -->
                <div
                  class="flex items-center gap-2 px-3 py-2.5 bg-gray-50 rounded-t-lg"
                  :class="mod.collapsed ? 'rounded-b-lg' : 'border-b border-gray-200'"
                >
                  <button
                    v-if="!benchmarkIsLocked"
                    type="button"
                    class="module-drag-handle cursor-grab active:cursor-grabbing text-gray-400 hover:text-gray-600 select-none leading-none"
                    :title="$t('editor.dragTip')"
                  >⠿</button>
                  <button
                    type="button"
                    class="flex items-center gap-2 flex-1 min-w-0 text-left"
                    @click="toggleCollapse(mod)"
                    :title="mod.collapsed ? $t('editor.expand') : $t('editor.collapse')"
                  >
                    <span class="text-gray-400 text-[10px] transition-transform" :class="mod.collapsed ? '' : 'rotate-90'">▶</span>
                    <span class="text-sm font-semibold text-gray-700 truncate">{{ $t('editor.moduleN', { n: i + 1 }) }}: {{ displayName(mod.module_name) }}</span>
                  </button>
                  <div v-if="mod.collapsed" class="flex items-center gap-2 text-[11px] text-gray-500 shrink-0">
                    <span class="px-1.5 py-0.5 bg-white border border-gray-200 rounded font-mono">w={{ mod.weight }}</span>
                    <span v-if="mod.skip_if_prev_failed && i > 0" class="px-1.5 py-0.5 bg-amber-50 border border-amber-200 text-amber-700 rounded" :title="$t('editor.skipChainTip')">⏭ skip-chain</span>
                    <span class="hidden sm:inline">{{ scoreTermCount(mod) }} score · {{ redlineCount(mod) }} redline · {{ displayCount(mod) }} display</span>
                  </div>
                  <button
                    v-if="!benchmarkIsLocked"
                    type="button"
                    @click="removeModule(i)"
                    class="text-red-500 hover:text-red-700 text-xs shrink-0"
                  >{{ $t('editor.remove') }}</button>
                </div>

                <div v-show="!mod.collapsed" class="p-4 space-y-4">
                  <!-- Module selector + weight -->
                  <div class="grid grid-cols-2 gap-3">
                    <div>
                      <label class="block text-xs text-gray-500 mb-1">{{ $t('editor.module') }}</label>
                      <select v-model="mod.module_name" required class="text-sm rounded border-gray-300 w-full" @change="onModuleChange(i)">
                        <option v-for="m in availableModules" :key="m.name" :value="m.name">{{ m.display_name }}{{ m.deprecated ? ` (${$t('submit.deprecated')})` : '' }}</option>
                      </select>
                    </div>
                    <div>
                      <label class="block text-xs text-gray-500 mb-1 flex items-center gap-1">
                        {{ $t('subDetail.weight') }}
                        <span v-if="scoreTermCount(mod) === 0" class="text-[10px] font-normal text-orange-600" :title="$t('editor.noScoreTip')">
                          {{ $t('editor.displayOnlyW0') }}
                        </span>
                      </label>
                      <input v-model.number="mod.weight" type="number" min="0" max="1" step="0.001" required
                        :disabled="scoreTermCount(mod) === 0"
                        :class="['text-sm rounded border-gray-300 w-full', scoreTermCount(mod) === 0 ? 'bg-gray-100 text-gray-400 cursor-not-allowed' : '']" />
                    </div>
                  </div>

                  <!-- Cascade-skip: don't run this module if the previous one failed -->
                  <div class="flex items-start gap-2">
                    <input
                      :id="`skip-${mod._uid}`"
                      v-model="mod.skip_if_prev_failed"
                      type="checkbox"
                      :disabled="i === 0"
                      class="mt-0.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 disabled:opacity-40"
                    />
                    <label :for="`skip-${mod._uid}`" class="text-xs" :class="i === 0 ? 'text-gray-400' : 'text-gray-600'">
                      {{ $t('editor.skipIfPrev') }}
                      <span class="text-gray-400">— don't run this module when the one above failed, was skipped, or breached a redline. The skip cascades down the chain, so a single failure short-circuits the rest.</span>
                      <span v-if="i === 0" class="italic"> · no effect on the first module</span>
                    </label>
                  </div>

                  <!-- Deprecated-module warning -->
                  <div v-if="isModuleDeprecated(mod.module_name)"
                    class="bg-amber-50 border border-amber-300 text-amber-800 px-3 py-2 rounded text-xs">
                    <span class="font-semibold">⚠ Deprecated module.</span>
                    {{ moduleDeprecationNote(mod.module_name) || 'Scheduled for removal — avoid using it in new benchmarks.' }}
                  </div>

                  <!-- Module params -->
                  <div class="border-t border-gray-100 pt-3">
                    <p class="text-xs font-medium text-gray-500 mb-2">{{ $t('editor.parameters') }}</p>
                    <ModuleParamForm
                      :schema="selectedModuleSchema(mod.module_name)"
                      :suggestions="paramSuggestions"
                      v-model="mod.params_json"
                    />
                    <!-- Judge probe for modules with an LLM-judge (opencompass
                         SimpleQA + HLE, hallucination, …): judge failures at run
                         time degrade silently rather than erroring — SimpleQA
                         grades NOT_ATTEMPTED, HLE falls back to exact-match — so
                         test the endpoint here. The probe is SimpleQA-shaped;
                         HLE's larger token budget is checked by the worker. -->
                    <div v-if="hasJudgeParams(mod.module_name)" class="mt-3">
                      <JudgePreflight
                        :endpoint-url="String(mod.params_json.judge_api_url ?? '')"
                        :model="String(mod.params_json.judge_model ?? '')"
                        :api-key="String(mod.params_json.judge_api_key ?? '')"
                        :max-tokens="Number(mod.params_json.judge_max_tokens) || 256"
                      />
                    </div>
                  </div>

                  <!-- Metric configuration -->
                  <div class="border-t border-gray-100 pt-3">
                    <div class="flex items-center justify-between mb-2">
                      <p class="text-xs font-medium text-gray-500">{{ $t('editor.metricConfig') }}</p>
                      <span class="text-xs text-gray-400">
                        {{ scoreTermCount(mod) }} score · {{ redlineCount(mod) }} redline · {{ displayCount(mod) }} display
                      </span>
                    </div>
                    <div class="overflow-x-auto">
                      <table class="w-full text-xs border-collapse">
                        <thead>
                          <tr class="bg-gray-50 text-left text-gray-500">
                            <th class="px-2 py-1.5 border border-gray-200 font-medium w-32">{{ $t('subDetail.metric') }}</th>
                            <th class="px-2 py-1.5 border border-gray-200 font-medium w-32">{{ $t('editor.role') }}</th>
                            <th class="px-2 py-1.5 border border-gray-200 font-medium">{{ $t('editor.configuration') }}</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            v-for="(mc, mcIdx) in mod.metric_configs"
                            :key="mcIdx"
                            class="hover:bg-gray-50"
                            :class="isDuplicateMetricRow(mod, mc) ? 'bg-yellow-50' : ''"
                            :title="isDuplicateMetricRow(mod, mc) ? $t('editor.dupRowTip', { key: mc.key, role: mc.role }) : ''"
                          >
                            <td class="px-2 py-1.5 border border-gray-200">
                              <div class="font-medium text-gray-700">{{ metricDisplayName(mod.module_name, mc.key) }}</div>
                              <div class="text-gray-400 text-[10px]">{{ metricUnit(mod.module_name, mc.key) }}</div>
                            </td>
                            <td class="px-2 py-1.5 border border-gray-200">
                              <!-- min-width so a squeezed table can't truncate
                                   "Redline"/"Display" down to "Redli…" -->
                              <select v-model="mc.role" class="text-xs rounded border-gray-300 w-full min-w-[6.5rem]"
                                :class="mc.role === 'score' ? 'bg-blue-50' : mc.role === 'redline' ? 'bg-orange-50' : ''">
                                <option value="score">Score</option>
                                <option value="redline">Redline</option>
                                <option value="display">Display</option>
                              </select>
                            </td>
                            <td class="px-2 py-1.5 border border-gray-200">
                              <!-- Score config -->
                              <div v-if="mc.role === 'score'" class="flex flex-wrap gap-2 items-center">
                                <div class="flex items-center gap-1">
                                  <label class="text-gray-400">{{ $t('subDetail.weight') }}</label>
                                  <input v-model.number="mc.weight" type="number" step="any" min="0" class="w-16 text-xs rounded border-gray-300" />
                                </div>
                                <div class="flex items-center gap-1">
                                  <label class="text-gray-400">{{ $t('subDetail.formula') }}</label>
                                  <select v-model="mc.formula" class="text-xs rounded border-gray-300">
                                    <option value="ratio">ratio (÷ baseline)</option>
                                    <option value="ratio_capped">ratio capped (÷ baseline, max 1)</option>
                                    <option value="inverse_ratio_capped">inv ratio (baseline ÷ val, max 1)</option>
                                    <option value="linear">linear (zero_at → one_at)</option>
                                    <option value="passthrough">passthrough (raw value)</option>
                                    <option value="passthrough_scaled">passthrough × weight (unnormalized, score can exceed 1)</option>
                                  </select>
                                </div>
                                <template v-if="mc.formula === 'ratio' || mc.formula === 'ratio_capped' || mc.formula === 'inverse_ratio_capped'">
                                  <div class="flex items-center gap-1">
                                    <label class="text-gray-400">{{ $t('editor.baseline') }}</label>
                                    <input v-model.number="mc.baseline" type="number" step="any" class="w-24 text-xs rounded border-gray-300" placeholder="e.g. 5000" />
                                  </div>
                                </template>
                                <template v-if="mc.formula === 'linear'">
                                  <div class="flex items-center gap-1">
                                    <label class="text-gray-400">Zero at</label>
                                    <input v-model.number="mc.zero_at" type="number" step="any" class="w-20 text-xs rounded border-gray-300" />
                                  </div>
                                  <div class="flex items-center gap-1">
                                    <label class="text-gray-400">One at</label>
                                    <input v-model.number="mc.one_at" type="number" step="any" class="w-20 text-xs rounded border-gray-300" />
                                  </div>
                                </template>
                                <template v-if="mc.formula !== 'passthrough' && mc.formula !== 'passthrough_scaled'">
                                  <div class="flex items-center gap-1">
                                    <label class="text-gray-400">Clip [</label>
                                    <input v-model.number="mc.clip_low" type="number" step="any" class="w-14 text-xs rounded border-gray-300" placeholder="min" />
                                    <label class="text-gray-400">,</label>
                                    <input v-model.number="mc.clip_high" type="number" step="any" class="w-14 text-xs rounded border-gray-300" placeholder="max" />
                                    <label class="text-gray-400">]</label>
                                  </div>
                                </template>
                                <span v-if="mc.formula === 'passthrough_scaled'" class="text-[10px] text-blue-600 italic">
                                  contributes weight × value (no normalization)
                                </span>
                              </div>
                              <!-- Redline config -->
                              <div v-else-if="mc.role === 'redline'" class="flex flex-wrap gap-3 items-center">
                                <div class="flex items-center gap-1">
                                  <label class="text-gray-400">≥ (min)</label>
                                  <input v-model.number="mc.min_val" type="number" step="any" class="w-24 text-xs rounded border-gray-300" placeholder="—" />
                                </div>
                                <div class="flex items-center gap-1">
                                  <label class="text-gray-400">≤ (max)</label>
                                  <input v-model.number="mc.max_val" type="number" step="any" class="w-24 text-xs rounded border-gray-300" placeholder="—" />
                                </div>
                              </div>
                              <!-- Display role needs no configuration — leave the cell blank -->
                              <span v-else></span>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>

                    <!-- Score formula preview -->
                    <div v-if="scoreTermCount(mod) > 0" class="mt-2 p-2 bg-blue-50 rounded text-xs text-blue-700 font-mono">
                      score = {{ scoreFormulaPreview(mod) }}
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </draggable>

          <div v-if="form.modules.length === 0" class="text-center py-6 text-sm text-gray-500 border border-dashed border-gray-200 rounded">
            No modules added yet. Click "+ Add module" to begin.
          </div>

          <!-- Add module (bottom) — mirrors the top button so long lists don't
               require scrolling back up. -->
          <button
            v-if="!benchmarkIsLocked && form.modules.length > 0"
            type="button"
            @click="addModule"
            class="mt-4 w-full border border-dashed border-indigo-300 text-indigo-600 hover:bg-indigo-50 rounded-lg py-2.5 text-sm font-medium transition-colors"
          >{{ $t('editor.addModule') }}</button>
        </div>

        <!-- Validation summary (bottom). Mirror of the top panel so a save-
             blocking error is visible right where the user clicks Save. -->
        <div v-if="validationErrors.length" class="bg-amber-50 border border-amber-200 text-amber-800 px-4 py-3 rounded text-sm">
          <p class="font-medium mb-1">{{ $t('editor.fixIssues', { n: validationErrors.length }) }}</p>
          <ul class="list-disc list-inside space-y-0.5">
            <li v-for="(v, idx) in validationErrors" :key="idx">
              <button
                v-if="v.moduleUid != null"
                type="button"
                class="text-left underline decoration-dotted hover:text-amber-900"
                @click="jumpToModule(v.moduleUid)"
              >{{ v.msg }}</button>
              <span v-else>{{ v.msg }}</span>
            </li>
          </ul>
        </div>

        <!-- Advisory warnings (bottom). Mirror of the top panel so a non-blocking
             warning is visible right where the user clicks Save. -->
        <div v-if="metricConfigWarnings.length" class="bg-yellow-50 border border-yellow-300 text-yellow-800 px-4 py-3 rounded text-sm">
          <p class="font-medium mb-1">⚠ {{ $t('editor.dupWarn', { n: metricConfigWarningCount }) }}</p>
          <ul class="list-disc list-inside space-y-0.5">
            <li v-for="(w, idx) in metricConfigWarnings" :key="idx">
              <button
                v-if="w.moduleUid != null"
                type="button"
                class="text-left underline decoration-dotted hover:text-yellow-900"
                @click="jumpToModule(w.moduleUid)"
              >{{ w.label }}: {{ w.names.join(', ') }}</button>
              <span v-else>{{ w.label }}: {{ w.names.join(', ') }}</span>
            </li>
          </ul>
        </div>

        <div class="flex justify-end space-x-3">
          <router-link to="/admin/benchmarks" class="py-2 px-4 border border-gray-300 rounded-md text-sm text-gray-600 hover:bg-gray-50">
            Cancel
          </router-link>
          <button
            type="submit"
            :disabled="loading || validationErrors.length > 0"
            class="py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50"
          >
            {{ loading ? (isEdit ? $t('editor.saving') : $t('editor.creating')) : (isEdit ? $t('editor.saveChanges') : $t('editor.titleCreate')) }}
          </button>
        </div>
      </form>

      <!-- Danger zone: delete -->
      <div v-if="isEdit" class="mt-8 border-t border-gray-200 pt-6">
        <h3 class="text-sm font-medium text-gray-500 mb-3">{{ $t('editor.dangerZone') }}</h3>
        <div v-if="!showDeleteConfirm" class="flex items-center justify-between bg-red-50 border border-red-200 rounded-lg px-4 py-3">
          <div>
            <p class="text-sm font-medium text-red-800">{{ $t('editor.deleteTitle') }}</p>
            <p class="text-xs text-red-600 mt-0.5">{{ $t('editor.deleteHint') }}</p>
          </div>
          <button
            type="button"
            @click="showDeleteConfirm = true"
            class="px-3 py-1.5 text-xs font-medium text-red-700 border border-red-300 rounded hover:bg-red-100"
          >{{ $t('common.delete') }}</button>
        </div>
        <div v-else class="bg-red-50 border border-red-300 rounded-lg px-4 py-4 space-y-3">
          <p class="text-sm font-semibold text-red-800">{{ $t('editor.deleteConfirm') }}</p>
          <div class="flex gap-3">
            <button
              type="button"
              :disabled="deleting"
              @click="handleDelete"
              class="px-4 py-1.5 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded disabled:opacity-50"
            >{{ deleting ? $t('editor.deleting') : $t('editor.deleteYes') }}</button>
            <button
              type="button"
              @click="showDeleteConfirm = false"
              class="px-4 py-1.5 text-sm font-medium text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
            >{{ $t('common.cancel') }}</button>
          </div>
        </div>
      </div>

    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter, useRoute } from 'vue-router'
import draggable from 'vuedraggable'
import { benchmarksApi, replayDatasetsApi } from '@/api/client'
import {
  GROUP_TAG_SEGMENT_MAX,
  groupTagSuggestions,
  joinGroupTag,
  splitGroupTag,
  suggestionsUnder,
  type GroupTagLevels,
} from '@/utils/groupTags'
import { useBenchmarksStore } from '@/stores/benchmarks'
import ModuleParamForm from '@/components/ModuleParamForm.vue'
import JudgePreflight from '@/components/JudgePreflight.vue'
import type { ModuleDescriptor, MetricConfig } from '@/api/client'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const store = useBenchmarksStore()

const isEdit = computed(() => route.name === 'benchmark-edit')
const benchmarkSlug = computed(() => route.params.id as string | undefined)
const benchmarkId = ref<number | null>(null)
const benchmarkIsLocked = ref(false)
const benchmarkConfigHash = ref<string | null>(null)
// Slug of the benchmark this create-form was cloned from (null = blank create).
const cloneSource = ref<string | null>(null)

const modules = ref<ModuleDescriptor[]>([])

interface ModuleForm {
  // Stable per-row id (UI only, never sent to the backend). Used as the v-for /
  // draggable key so reordering doesn't make Vue recycle the wrong DOM, and as
  // the scroll/flash target when a module is added.
  _uid: number
  // Collapsed state is UI-only — lets long multi-module benchmarks fold away.
  collapsed: boolean
  module_name: string
  params_json: Record<string, any>
  metric_configs: MetricConfig[]
  weight: number
  order_index: number
  // Skip this module when the preceding one fails/skips/breaches a redline.
  skip_if_prev_failed: boolean
}

// Monotonic counter for ModuleForm._uid. Module objects get reordered/removed,
// so positional indexes are not stable keys; this is.
let _moduleUid = 0
function nextUid(): number {
  _moduleUid += 1
  return _moduleUid
}

// Auto-collapse on load only when there are enough modules that scrolling hurts;
// a 1–2 module benchmark stays fully expanded (least surprising).
const COLLAPSE_ON_LOAD_THRESHOLD = 3

const form = ref({
  name: '',
  slug: '',
  description: '',
  version: '1',
  status: 'draft',
  modules: [] as ModuleForm[],
})

// Display grouping ("Top/Mid/Low" paths) — edited as rows of three top-down
// inputs so the "no mid without top" rule is enforced by the UI itself; the
// backend re-validates. Serialised via joinGroupTag at submit time.
interface GroupTagRow { _uid: number; levels: GroupTagLevels }
const groupTagRows = ref<GroupTagRow[]>([])
// Every path prefix already used by any benchmark, for datalist autocomplete
// so admins converge on the same spelling instead of forking groups.
const knownGroupPaths = ref<Set<string>>(new Set())

function groupRowsFrom(tags: string[] | null | undefined): GroupTagRow[] {
  return (tags ?? []).map((t) => ({ _uid: nextUid(), levels: splitGroupTag(t) }))
}

function addGroupTag() {
  groupTagRows.value.push({ _uid: nextUid(), levels: ['', '', ''] })
}

// Clearing a level empties everything below it, so a row can never hold a
// low-level tag with a blank mid-level above it.
function onGroupLevelInput(row: GroupTagRow, level: number) {
  if (!row.levels[level].trim()) {
    for (let i = level + 1; i < row.levels.length; i++) row.levels[i] = ''
  }
}

function groupLevelOptions(row: GroupTagRow, level: number): string[] {
  const parent = joinGroupTag([
    level > 0 ? row.levels[0] : '',
    level > 1 ? row.levels[1] : '',
    '',
  ] as GroupTagLevels)
  if (level > 0 && !parent) return []
  return suggestionsUnder(knownGroupPaths.value, parent)
}

function serializedGroupTags(): string[] {
  const out: string[] = []
  for (const row of groupTagRows.value) {
    const path = joinGroupTag(row.levels)
    if (path && !out.includes(path)) out.push(path)
  }
  return out
}

async function loadGroupTagSuggestions() {
  // Best-effort: autocomplete only. A failure here must not block the editor.
  try {
    const { data } = await benchmarksApi.adminList()
    knownGroupPaths.value = groupTagSuggestions(data.benchmarks)
  } catch {
    knownGroupPaths.value = new Set()
  }
}
const loading = ref(false)
const locking = ref(false)
const deleting = ref(false)
const showDeleteConfirm = ref(false)
const error = ref('')

// Modules without scoring metrics cannot contribute to the overall score —
// force their weight to 0 so the backend's 400-on-mismatch validation never
// fires from user keystrokes. Triggers whenever roles flip or metrics are
// added/removed in any module.
watch(
  () => form.value.modules.map(m => ({
    hasScore: m.metric_configs.some(mc => mc.role === 'score'),
    weight: m.weight,
  })),
  () => {
    for (const mod of form.value.modules) {
      const hasScore = mod.metric_configs.some(mc => mc.role === 'score')
      if (!hasScore && mod.weight !== 0) {
        mod.weight = 0
      }
    }
  },
  { deep: true },
)

function formModulesFrom(data: any): ModuleForm[] {
  const raw = [...(data.modules || [])]
  // Show them in saved display order so the visual order matches order_index
  // (which drag-reordering then rewrites from array position).
  raw.sort((a: any, b: any) => (a.order_index ?? 0) - (b.order_index ?? 0))
  const autoCollapse = raw.length >= COLLAPSE_ON_LOAD_THRESHOLD
  return raw.map((m: any, i: number) => ({
    _uid: nextUid(),
    collapsed: autoCollapse,
    module_name: m.module_name,
    params_json: { ...(m.params_json || {}) },
    metric_configs: initialMetricConfigs(m.metric_configs ?? [], m.module_name),
    weight: m.weight,
    order_index: i,
    skip_if_prev_failed: m.skip_if_prev_failed ?? false,
  }))
}

async function loadBenchmark(slug: string) {
  const { data } = await benchmarksApi.get(slug)
  cloneSource.value = null
  benchmarkId.value = data.id
  benchmarkIsLocked.value = data.is_locked
  benchmarkConfigHash.value = data.config_hash
  form.value = {
    name: data.name,
    slug: data.slug,
    description: data.description,
    version: data.version,
    status: data.status,
    modules: formModulesFrom(data),
  }
  groupTagRows.value = groupRowsFrom(data.group_tags)
}

async function loadClone(slug: string) {
  const { data } = await benchmarksApi.get(slug)
  cloneSource.value = data.slug
  // Fresh benchmark — no id/lock/hash, so submit CREATEs rather than updates.
  benchmarkId.value = null
  benchmarkIsLocked.value = false
  benchmarkConfigHash.value = null
  form.value = {
    // Suffix slug + name so the unique-slug constraint passes and the admin is
    // nudged to rename. Slug stays editable in create mode.
    name: `${data.name} (copy)`,
    slug: `${data.slug}-copy`,
    description: data.description,
    version: data.version,
    // Clones start as draft so a copy never goes live by accident.
    status: 'draft',
    modules: formModulesFrom(data),
  }
  // Grouping is presentation, and a clone usually belongs next to its source.
  groupTagRows.value = groupRowsFrom(data.group_tags)
}

function resetForm() {
  cloneSource.value = null
  benchmarkId.value = null
  benchmarkIsLocked.value = false
  benchmarkConfigHash.value = null
  form.value = {
    name: '',
    slug: '',
    description: '',
    version: '1',
    status: 'draft',
    modules: [],
  }
  groupTagRows.value = []
}

// Values that only exist at runtime, offered to free-text params as
// suggestions (see ModuleParamForm's `suggestions` prop). Fetched best-effort:
// the rolling dataset feature is optional, so a failure here must not stop the
// editor from loading — the field simply stays plain free text.
const paramSuggestions = ref<Record<string, string[]>>({})

async function loadParamSuggestions() {
  try {
    const { data } = await replayDatasetsApi.names()
    paramSuggestions.value = { dataset_profile: data.profiles.map((p) => p.name) }
  } catch {
    paramSuggestions.value = {}
  }
}

onMounted(async () => {
  await store.fetchModules()
  modules.value = store.modules
  loadParamSuggestions()
  loadGroupTagSuggestions()

  if (isEdit.value && benchmarkSlug.value) {
    await loadBenchmark(benchmarkSlug.value)
  } else if (!isEdit.value && route.query.from) {
    await loadClone(route.query.from as string)
  }
})

// <router-view> in App.vue has no :key, so navigating between two edit pages
// (e.g. via the admin list) reuses this component without re-firing onMounted.
// Without this watcher, the form keeps showing the previously-loaded benchmark's
// data — which looks like "the edit page sometimes doesn't load current params".
watch(
  () => route.params.id,
  async (newSlug) => {
    if (!isEdit.value || !newSlug) return
    error.value = ''
    showDeleteConfirm.value = false
    await loadBenchmark(newSlug as string)
  },
)

// Same component is reused across create routes (no <router-view> :key), so a
// changing ?from= (clone→clone, or clone→blank "New Benchmark") won't re-fire
// onMounted. Re-derive the form from the query here.
watch(
  () => route.query.from,
  async (from) => {
    if (isEdit.value) return
    error.value = ''
    showDeleteConfirm.value = false
    if (from) await loadClone(from as string)
    else resetForm()
  },
)

const availableModules = computed(() => modules.value)

const weightWarning = computed(() => {
  const total = form.value.modules.reduce((s, m) => s + (m.weight || 0), 0)
  if (form.value.modules.length > 0 && Math.abs(total - 1.0) > 0.001) {
    return t('editor.vWeights', { total: total.toFixed(3) })
  }
  return ''
})

function selectedModuleSchema(name: string) {
  return modules.value.find(m => m.name === name)?.params_schema || {}
}

// Modules that carry an LLM-judge config expose judge_api_url in their params
// schema — that's the signal to offer the "Test judge endpoint" probe.
function hasJudgeParams(name: string): boolean {
  const props = (selectedModuleSchema(name) as any)?.properties
  return !!props?.judge_api_url
}

function defaultMetricConfigs(moduleName: string): MetricConfig[] {
  const mod = modules.value.find(m => m.name === moduleName)
  return (mod?.metrics_schema?.default_metric_configs ?? []).map(mc => ({ ...mc }))
}

function initialMetricConfigs(savedConfigs: MetricConfig[], moduleName: string): MetricConfig[] {
  // Brand-new module slot (nothing saved yet): seed from the module defaults.
  if (!savedConfigs || savedConfigs.length === 0) {
    return defaultMetricConfigs(moduleName)
  }
  // Existing benchmark: keep the saved rows verbatim (merging defaults on top
  // previously caused phantom rows / duplicate roles), but APPEND any metric the
  // module has gained since this benchmark was saved — otherwise a metric added
  // to the module later (e.g. a new benchmark in the suite) is invisible here and
  // never scored or reported. New rows default to 'display' so they show and are
  // reported without disturbing the existing score/redline setup; the admin can
  // promote them to score/redline.
  const configs = savedConfigs.map(c => ({ ...c }))
  const seen = new Set(configs.map(c => c.key))
  for (const d of metricDescriptors(moduleName)) {
    if (!seen.has(d.name)) {
      configs.push({ key: d.name, role: 'display' })
      seen.add(d.name)
    }
  }
  return configs
}

function displayName(moduleName: string) {
  return modules.value.find(m => m.name === moduleName)?.display_name || moduleName
}

function isModuleDeprecated(moduleName: string): boolean {
  return modules.value.find(m => m.name === moduleName)?.deprecated === true
}

function moduleDeprecationNote(moduleName: string): string {
  return modules.value.find(m => m.name === moduleName)?.deprecation_note || ''
}

function metricDescriptors(moduleName: string) {
  return modules.value.find(m => m.name === moduleName)?.metrics_schema?.metrics_descriptors ?? []
}

function metricDisplayName(moduleName: string, key: string) {
  return metricDescriptors(moduleName).find(d => d.name === key)?.display_name || key
}

function metricUnit(moduleName: string, key: string) {
  return metricDescriptors(moduleName).find(d => d.name === key)?.unit || ''
}

function scoreTermCount(mod: ModuleForm) {
  return mod.metric_configs.filter(mc => mc.role === 'score').length
}

function redlineCount(mod: ModuleForm) {
  return mod.metric_configs.filter(mc => mc.role === 'redline').length
}

function displayCount(mod: ModuleForm) {
  return mod.metric_configs.filter(mc => mc.role === 'display').length
}

function scoreFormulaPreview(mod: ModuleForm): string {
  const terms = mod.metric_configs.filter(mc => mc.role === 'score')
  // passthrough_scaled terms don't participate in the normalization denominator.
  const normTerms = terms.filter(t => t.formula !== 'passthrough_scaled')
  const totalW = normTerms.reduce((s, t) => s + (t.weight ?? 1), 0) || 1
  return terms.map(t => {
    const key = metricDisplayName(mod.module_name, t.key)
    if (t.formula === 'passthrough_scaled') {
      return `${t.weight ?? 1}×${key}`
    }
    const w = ((t.weight ?? 1) / totalW * 100).toFixed(0)
    switch (t.formula) {
      case 'ratio': return `${w}%×(${key}÷${t.baseline ?? '?'})`
      case 'ratio_capped': return `${w}%×min(${key}÷${t.baseline ?? '?'},1)`
      case 'inverse_ratio_capped': return `${w}%×min(${t.baseline ?? '?'}÷${key},1)`
      case 'linear': return `${w}%×linear(${key},[${t.zero_at}→${t.one_at}])`
      case 'passthrough': return `${w}%×${key}`
      default: return `${w}%×${key}`
    }
  }).join(' + ')
}

function onModuleChange(i: number) {
  const mod = form.value.modules[i]
  mod.metric_configs = defaultMetricConfigs(mod.module_name)
  mod.params_json = { ...(modules.value.find(m => m.name === mod.module_name)?.default_params || {}) }
}

// Flash highlight for the most-recently-added module so the user notices a new
// card appeared (especially when it's added from the top button and rendered
// below the fold). Cleared after the animation window.
const justAddedUid = ref<number | null>(null)

async function addModule() {
  const first = modules.value[0]
  const uid = nextUid()
  form.value.modules.push({
    _uid: uid,
    collapsed: false, // new module is always open so the user can configure it
    module_name: first?.name || '',
    params_json: { ...(first?.default_params || {}) },
    metric_configs: initialMetricConfigs([], first?.name || ''),
    weight: 0,
    order_index: form.value.modules.length,
    skip_if_prev_failed: false,
  })
  normalizeOrder()
  // Scroll the new card into view and flash it.
  justAddedUid.value = uid
  await nextTick()
  const el = document.getElementById(`module-card-${uid}`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  window.setTimeout(() => {
    if (justAddedUid.value === uid) justAddedUid.value = null
  }, 1600)
}

function removeModule(i: number) {
  form.value.modules.splice(i, 1)
  normalizeOrder()
}

// Reassign order_index from array position. Called after drag-reorder, add, and
// remove so order_index is always a clean 0..n-1 matching what the user sees.
function normalizeOrder() {
  form.value.modules.forEach((m, i) => { m.order_index = i })
}

// vuedraggable mutates form.modules in place via v-model; this fires on drop.
function onReorder() {
  normalizeOrder()
}

function toggleCollapse(mod: ModuleForm) {
  mod.collapsed = !mod.collapsed
}

const allCollapsed = computed(
  () => form.value.modules.length > 0 && form.value.modules.every(m => m.collapsed),
)

function setAllCollapsed(collapsed: boolean) {
  form.value.modules.forEach(m => { m.collapsed = collapsed })
}

// All client-side checks that must pass before save. Surfaced at the top and
// bottom of the form and used to disable the submit button. Entries with a
// `moduleUid` are clickable to scroll/expand the offending module.
const validationErrors = computed<{ msg: string; moduleUid?: number }[]>(() => {
  const errs: { msg: string; moduleUid?: number }[] = []
  if (!form.value.name.trim()) errs.push({ msg: t('editor.vName') })
  if (!isEdit.value) {
    if (!form.value.slug.trim()) {
      errs.push({ msg: t('editor.vSlug') })
    } else if (!/^[a-z0-9-]+$/.test(form.value.slug)) {
      errs.push({ msg: t('editor.vSlugFormat') })
    }
  }
  if (!String(form.value.version).trim()) errs.push({ msg: t('editor.vVersion') })
  if (form.value.modules.length === 0) {
    errs.push({ msg: t('editor.vModules') })
  }
  if (weightWarning.value) errs.push({ msg: weightWarning.value })
  // Per-module: a score metric whose formula needs reference points but has none
  // would be rejected by the backend — catch it here with a jump-to link.
  form.value.modules.forEach(mod => {
    const label = `${t('editor.moduleN', { n: mod.order_index + 1 })} (${displayName(mod.module_name)})`
    for (const mc of mod.metric_configs) {
      if (mc.role !== 'score') continue
      const metric = metricDisplayName(mod.module_name, mc.key)
      const needsBaseline = mc.formula === 'ratio' || mc.formula === 'ratio_capped' || mc.formula === 'inverse_ratio_capped'
      if (needsBaseline && (mc.baseline === null || mc.baseline === undefined || (mc.baseline as any) === '')) {
        errs.push({ msg: t('editor.vBaseline', { label, metric }), moduleUid: mod._uid })
      }
      if (mc.formula === 'linear' && (mc.zero_at == null || mc.one_at == null)) {
        errs.push({ msg: t('editor.vLinear', { label, metric }), moduleUid: mod._uid })
      }
    }
  })
  return errs
})

// Advisory warnings — surfaced reactively but, unlike validationErrors, they do
// NOT gate the submit button. A metric key may legitimately appear more than once
// under one role while an admin stages rows before splitting a metric into a
// dual-purpose (multi-role) setup, so duplicate (key, role) pairs are flagged for
// review but never block the save.
const metricConfigWarnings = computed<{ moduleUid: number; label: string; names: string[] }[]>(() => {
  const warns: { moduleUid: number; label: string; names: string[] }[] = []
  form.value.modules.forEach(mod => {
    // Count structurally (no string join/split round-trip): a previous
    // version encoded the (key, role) pair into one string and split it
    // back, which broke when the separator didn't round-trip — the
    // warning showed raw metric keys and "(undefined)" roles.
    const counts = new Map<string, { key: string; role: string; n: number }>()
    for (const mc of mod.metric_configs) {
      const id = JSON.stringify([mc.key, mc.role])
      const entry = counts.get(id)
      if (entry) entry.n += 1
      else counts.set(id, { key: mc.key, role: mc.role, n: 1 })
    }
    const names: string[] = []
    for (const { key, role, n } of counts.values()) {
      if (n <= 1) continue
      const metric = metricDisplayName(mod.module_name, key)
      names.push(role === 'display' ? metric : `${metric} (${role})`)
    }
    if (names.length) {
      warns.push({
        moduleUid: mod._uid,
        label: `Module ${mod.order_index + 1} (${displayName(mod.module_name)})`,
        names,
      })
    }
  })
  return warns
})

// Total duplicate rows across all modules — drives the panel header count.
const metricConfigWarningCount = computed(() =>
  metricConfigWarnings.value.reduce((sum, w) => sum + w.names.length, 0),
)

// True when another row in the same module already uses this (key, role) pair —
// drives the inline row highlight that mirrors the advisory warning panel.
function isDuplicateMetricRow(mod: ModuleForm, mc: MetricConfig): boolean {
  return mod.metric_configs.filter(x => x.key === mc.key && x.role === mc.role).length > 1
}

// Click a validation error → expand and scroll to the relevant module.
async function jumpToModule(uid: number | undefined) {
  if (uid == null) return
  const mod = form.value.modules.find(m => m._uid === uid)
  if (mod) mod.collapsed = false
  await nextTick()
  const el = document.getElementById(`module-card-${uid}`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function handleSubmit() {
  // Defensive: the submit button is disabled while errors exist, but if it's
  // reached anyway, surface the first error rather than POSTing an invalid config.
  if (validationErrors.value.length > 0) {
    const first = validationErrors.value[0]
    error.value = first.msg
    await jumpToModule(first.moduleUid)
    return
  }
  loading.value = true
  error.value = ''
  try {
    // order_index follows the on-screen (drag) order; _uid/collapsed are UI-only
    // and deliberately not sent.
    const payload = form.value.modules.map((m, i) => ({
      module_name: m.module_name,
      params_json: m.params_json,
      metric_configs: m.metric_configs,
      weight: m.weight,
      order_index: i,
      // First module has no predecessor, so the flag is meaningless there.
      skip_if_prev_failed: i === 0 ? false : m.skip_if_prev_failed,
    }))

    if (isEdit.value && benchmarkId.value) {
      const { data } = await benchmarksApi.update(benchmarkId.value, {
        name: form.value.name,
        description: form.value.description,
        version: form.value.version,
        status: form.value.status as any,
        group_tags: serializedGroupTags(),
        modules: payload as any,
      })
      benchmarkConfigHash.value = data.config_hash
    } else {
      await benchmarksApi.create({
        slug: form.value.slug,
        name: form.value.name,
        description: form.value.description,
        version: form.value.version,
        status: form.value.status,
        group_tags: serializedGroupTags(),
        modules: payload,
      })
    }
    router.push('/admin/benchmarks')
  } catch (e: any) {
    error.value = e.response?.data?.detail || (isEdit.value ? t('adminBench.updateFailed') : t('editor.createFailed'))
  } finally {
    loading.value = false
  }
}

async function handleToggleLock() {
  if (!benchmarkId.value) return
  locking.value = true
  error.value = ''
  try {
    const { data } = await benchmarksApi.toggleLock(benchmarkId.value)
    benchmarkIsLocked.value = data.is_locked
    benchmarkConfigHash.value = data.config_hash
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('editor.lockFailed')
  } finally {
    locking.value = false
  }
}

async function handleDelete() {
  if (!benchmarkId.value) return
  deleting.value = true
  error.value = ''
  try {
    await benchmarksApi.delete(benchmarkId.value)
    router.push('/admin/benchmarks')
  } catch (e: any) {
    error.value = e.response?.data?.detail || t('editor.deleteFailed')
    showDeleteConfirm.value = false
  } finally {
    deleting.value = false
  }
}
</script>

<style scoped>
/* Placeholder shown at the drop target while dragging a module (vuedraggable
   ghost-class). */
.drag-ghost {
  opacity: 0.5;
  background-color: #eef2ff; /* indigo-50 */
}
</style>
