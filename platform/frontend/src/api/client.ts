import axios from 'axios'

const API_BASE = import.meta.env.VITE_API_URL || '/api'

export const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
})

// Inject JWT token on every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Redirect to login on 401 — the session (token) is dead or missing.
//
// EXCEPT for the login attempt itself: its 401 means "wrong email/password",
// and the login page shows that inline. Redirecting would reload /login and
// wipe the error before the user can read it (which made a failed login look
// like a silent no-op). No other auth endpoint returns a user-facing 401:
// change-password uses 400 for a wrong current password.
api.interceptors.response.use(
  (res) => res,
  (err) => {
    const isLoginAttempt = (err.config?.url || '').endsWith('/auth/login')
    if (err.response?.status === 401 && !isLoginAttempt) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  },
)

// ---------------------------------------------------------------------------
// Types mirroring the backend schemas
// ---------------------------------------------------------------------------

// Pure display metadata for one metric key a module can emit
export interface MetricDescriptor {
  name: string
  display_name: string
  unit: string
  description: string
  higher_is_better: boolean
}

// Evaluation rule for one metric within a specific benchmark configuration
export type MetricRole = 'score' | 'redline' | 'display'
export type ScoreFormula = 'ratio' | 'ratio_capped' | 'inverse_ratio_capped' | 'linear' | 'passthrough' | 'passthrough_scaled'

export interface MetricConfig {
  key: string
  role: MetricRole
  // score fields
  weight?: number
  formula?: ScoreFormula
  baseline?: number | null
  zero_at?: number | null
  one_at?: number | null
  clip_low?: number | null
  clip_high?: number | null
  // redline fields
  min_val?: number | null
  max_val?: number | null
}

export interface ModuleMetricsSchema {
  metrics_descriptors: MetricDescriptor[]
  default_metric_configs: MetricConfig[]
}

export interface ModuleDescriptor {
  name: string
  display_name: string
  description: string
  params_schema: Record<string, any>
  default_params: Record<string, any>
  metrics_schema?: ModuleMetricsSchema
  deprecated?: boolean
  deprecation_note?: string
  // True if this module has a `concurrency` param the submission-level
  // concurrency override applies to. Derived on the backend from the module
  // schema, so new modules surface automatically with no frontend edits.
  supports_concurrency_override?: boolean
}

export interface BenchmarkModule {
  id: number
  module_name: string
  display_name: string
  params_json: Record<string, any>
  metric_configs: MetricConfig[]
  params_schema: { properties?: Record<string, { description?: string; default?: any }> }
  weight: number
  order_index: number
  // Skip this module if the immediately-preceding module failed/skipped/breached
  // a redline (the skip cascades down the module order).
  skip_if_prev_failed?: boolean
  metrics_schema: ModuleMetricsSchema
  // Whether the concurrency override affects this module (backend-derived).
  supports_concurrency_override?: boolean
}

export interface Benchmark {
  id: number
  slug: string
  name: string
  description: string
  version: string
  status: 'draft' | 'active' | 'archived'
  config_hash: string | null
  is_locked: boolean
  // Display grouping: "Top/Mid/Low" paths (1-3 levels). See utils/groupTags.ts.
  group_tags: string[]
  created_by_user_id: number
  created_at: string
  modules: BenchmarkModule[]
}

export interface Submission {
  id: number
  user_id: number
  // Owner's username — populated only by the admin "all submissions" listing.
  username?: string | null
  benchmark_id: number | null
  benchmark_slug: string | null
  module_name: string | null
  endpoint_url: string
  endpoint_model: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'canceled'
  score_total: number | null
  passed: boolean | null
  error: string | null
  benchmark_config_hash: string | null
  contributor: string | null
  // Free-form bag of submission-level knobs. The two concurrency mechanisms are
  // mutually exclusive (backend-enforced): one global value, OR a per-module map.
  extra_params: SubmissionExtraParams | null
  // Optional hardware section — null when omitted at submit time.
  cards_per_machine: number | null
  machine_count: number | null
  card_type: string | null
  // Submitter-authored description: one-line summary (≤100 chars, also shown
  // on the leaderboard) + longer markdown detail (submission detail page).
  description_summary: string | null
  description_detail: string | null
  // Optional link back to the system that produced this submission (a Baseline
  // AutoTune run, a CI job, …). Opaque — never parsed or rewritten; the backend
  // guarantees only that it is an http(s) URL, which is what makes it safe to
  // bind straight to an href.
  source_url: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

// Submission-level knobs bag (backend SubmissionExtraParams). Set at most one
// of the two concurrency override mechanisms — the backend 422s when both are set.
export interface SubmissionExtraParams {
  concurrency_override?: number | null
  module_concurrency_overrides?: Record<string, number> | null
}

export interface PaginatedSubmissions {
  items: Submission[]
  total: number
  limit: number
  offset: number
}

// Result of a pre-submit endpoint probe (see backend app/core/preflight.py).
export type PreflightStatus = 'pass' | 'fail' | 'warn' | 'skip'

export interface PreflightCheck {
  name: string
  status: PreflightStatus
  detail: string
}

export interface PreflightResult {
  ok: boolean
  latency_ms: number | null
  endpoint_tested: string | null
  checks: PreflightCheck[]
}

export interface SubmissionRun {
  id: number
  module_name: string
  params_json: Record<string, any>
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
  score: number | null
  passed: boolean | null
  metrics_json: Record<string, any> | null
  metric_configs_json: MetricConfig[] | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  artifact_path: string | null
  // Present only when the submission-level concurrency override affected this
  // module (backend-derived). `effective` is the value the run actually used,
  // after clamping to the module's own range.
  concurrency_override?: {
    param: string
    original: number | null
    requested: number
    effective: number
  } | null
}

export interface SubmissionDetail extends Submission {
  runs: SubmissionRun[]
  benchmark_slug: string | null
  benchmark_name: string | null
}

export interface LeaderboardModule {
  name: string
  order_index: number
}

export interface LeaderboardRow {
  rank: number
  submission_id: number
  created_at: string | null
  username: string
  endpoint_model: string
  // One-line, submitter-authored description of the service/optimizations.
  description_summary: string | null
  score_total: number | null
  passed: boolean | null
  config_hash: string | null
  runs: {
    module_name: string
    order_index: number
    score: number | null
    passed: boolean | null
    metrics: Record<string, any> | null
  }[]
}

export interface Leaderboard {
  benchmark_slug: string
  benchmark_name: string
  current_config_hash: string | null
  total_submissions: number
  modules: LeaderboardModule[]
  module_names: string[]
  rows: LeaderboardRow[]
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export const authApi = {
  register(data: { email: string; username: string; password: string }) {
    return api.post<{ access_token: string; token_type: string; role: string }>('/auth/register', data)
  },
  login(data: { email: string; password: string }) {
    return api.post<{ access_token: string; token_type: string; role: string }>('/auth/login', data)
  },
  me() {
    return api.get<{ id: number; email: string; username: string; role: string }>('/auth/me')
  },
  changePassword(data: { current_password: string; new_password: string }) {
    // Returns a fresh token: the change invalidates older sessions, so the
    // caller must adopt this one to stay logged in.
    return api.post<{ access_token: string; token_type: string; role: string }>(
      '/auth/change-password',
      data,
    )
  },
}

export interface ApiKey {
  id: number
  name: string
  key_prefix: string
  created_at: string
  last_used_at: string | null
}

// Returned only at creation — carries the plaintext key once.
export interface ApiKeyCreated extends ApiKey {
  key: string
}

export const apiKeysApi = {
  list() {
    return api.get<ApiKey[]>('/auth/api-keys')
  },
  create(name: string) {
    return api.post<ApiKeyCreated>('/auth/api-keys', { name })
  },
  revoke(id: number) {
    return api.delete(`/auth/api-keys/${id}`)
  },
}

export const modulesApi = {
  list() {
    return api.get<{ modules: ModuleDescriptor[] }>('/modules')
  },
  get(name: string) {
    return api.get<ModuleDescriptor>(`/modules/${name}`)
  },
}

export interface AdminUser {
  id: number
  email: string
  username: string
  role: 'super_admin' | 'admin' | 'service' | 'user'
  created_at: string
}

export const adminUsersApi = {
  list() {
    return api.get<AdminUser[]>('/auth/admin/users')
  },
  setRole(id: number, role: 'admin' | 'user') {
    return api.patch<AdminUser>(`/auth/admin/users/${id}/role`, { role })
  },
}

// An admin-managed GPU card type offered in the submit "Hardware" dropdown.
export interface CardType {
  id: number
  name: string
  display_order: number
  created_at: string
}

export const cardTypesApi = {
  // Any authenticated user — feeds the submit-form dropdown.
  list() {
    return api.get<{ card_types: CardType[] }>('/card-types')
  },
  // Admin only.
  create(data: { name: string; display_order?: number }) {
    return api.post<CardType>('/card-types', data)
  },
  update(id: number, data: { name?: string; display_order?: number }) {
    return api.put<CardType>(`/card-types/${id}`, data)
  },
  remove(id: number) {
    return api.delete(`/card-types/${id}`)
  },
}

// ---------------------------------------------------------------------------
// Rolling replay datasets ("the feed")
// ---------------------------------------------------------------------------

// The build the datasets volume's pointer currently names — i.e. what a
// benchmark configured for this profile would replay right now. Read from the
// pointer file, not the build history, so it reflects reality.
export interface ReplayDatasetCurrent {
  build_id: string
  path: string
  records: number
  bytes: number
  sha256: string
  built_at: string
  age_hours: number
  stale: boolean
  window_start?: string | null
  window_end?: string | null
  buckets?: Record<string, number> | null
  summary?: {
    models?: Record<string, number>
    forwarded_to?: Record<string, number>
    prompt_tokens?: { count: number; min: number; p50: number; p90: number; max: number; mean: number } | null
    completion_tokens?: { count: number; min: number; p50: number; p90: number; max: number; mean: number } | null
    total_prompt_tokens?: number
    total_completion_tokens?: number
    total_cached_tokens?: number
    cache_hit_rate?: number | null
  } | null
}

export interface ReplayDatasetBuild {
  id: number
  build_id?: string | null
  status: 'pending' | 'running' | 'ready' | 'failed'
  trigger: string
  records?: number | null
  size_bytes?: number | null
  sha256?: string | null
  path?: string | null
  window_start?: string | null
  window_end?: string | null
  stats_json?: Record<string, any> | null
  progress?: string | null
  error?: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  // Whether the build's bytes are still on the datasets volume. Retention prunes
  // the FILE while the row lives on, so `status === 'ready'` alone is not enough
  // to offer a download. Only listBuilds/getBuild populate it.
  downloadable?: boolean | null
}

export interface ReplayDatasetProfile {
  id: number
  name: string
  display_name: string
  description?: string | null
  enabled: boolean
  source_url: string
  models: string[]
  statuses: string[]
  forwarded_to: string[]
  uris: string[]
  exclude_truncated: boolean
  extra_logsql?: string | null
  window_hours: number
  window_timezone: string
  subwindow_minutes: number
  sample_size: number
  oversample_factor: number
  max_carry_multiple: number
  max_bytes: number
  clean: boolean
  max_model_len: number
  keep_response_body: boolean
  compress: boolean
  header_denylist?: string[] | null
  min_records: number
  min_buckets: number
  schedule_interval_hours: number
  schedule_anchor_hour?: number | null
  max_age_hours: number
  keep_builds: number
  min_retain_hours: number
  created_at: string
  updated_at?: string | null
  current?: ReplayDatasetCurrent | null
  last_build?: ReplayDatasetBuild | null
}

export type ReplayDatasetProfileInput = Partial<Omit<ReplayDatasetProfile,
  'id' | 'created_at' | 'updated_at' | 'current' | 'last_build'>>

// A rolling build promoted to a permanent, retention-immune file. `path` is
// what an admin pastes into a fixed-source benchmark's dataset_path; `used_by`
// lists the benchmarks that already do, so deleting one is guarded.
export interface FrozenDataset {
  name: string
  path: string
  bytes: number
  records: number
  sha256: string
  frozen_at?: string | null
  source_profile: string
  source_build_id: string
  window_start?: string | null
  window_end?: string | null
  frozen_by: string
  used_by: string[]
}

export const replayDatasetsApi = {
  listProfiles() {
    return api.get<{ profiles: ReplayDatasetProfile[]; collector_configured: boolean }>(
      '/replay-datasets/profiles')
  },
  createProfile(data: ReplayDatasetProfileInput) {
    return api.post<ReplayDatasetProfile>('/replay-datasets/profiles', data)
  },
  updateProfile(id: number, data: ReplayDatasetProfileInput) {
    return api.put<ReplayDatasetProfile>(`/replay-datasets/profiles/${id}`, data)
  },
  removeProfile(id: number) {
    return api.delete(`/replay-datasets/profiles/${id}`)
  },
  listBuilds(id: number, limit = 30) {
    return api.get<{ builds: ReplayDatasetBuild[] }>(
      `/replay-datasets/profiles/${id}/builds`, { params: { limit } })
  },
  triggerBuild(id: number) {
    return api.post<{ build_row_id: number; queue_depth?: number }>(
      `/replay-datasets/profiles/${id}/build`)
  },
  // Dry-run the filters over a short window so a typo shows up as matched: 0
  // in the editor rather than as an empty dataset hours later.
  probe(id: number, hours = 1) {
    return api.post<{
      matched: number; query: string; sample_fields: string[]
      sample_prompt_tokens?: number | null; error?: string | null
    }>(`/replay-datasets/profiles/${id}/probe`, { hours })
  },
  names() {
    return api.get<{ profiles: { name: string; display_name: string }[] }>(
      '/replay-datasets/names')
  },
  // Promote a completed build to a permanent frozen dataset (proxied to the
  // collector, the writer of the datasets volume).
  freezeBuild(profileId: number, buildRowId: number, name: string) {
    return api.post<FrozenDataset>(
      `/replay-datasets/profiles/${profileId}/builds/${buildRowId}/freeze`, { name })
  },
  // A direct link, not an axios call: the dataset is hundreds of MB, so it must
  // stream to disk through the browser's own downloader instead of being
  // buffered into a Blob. That rules out the Authorization header, hence the
  // ?token= the endpoint accepts — the same trade the SSE log stream makes.
  buildDownloadUrl(profileId: number, buildRowId: number) {
    const token = localStorage.getItem('token') || ''
    return `${API_BASE}/replay-datasets/profiles/${profileId}/builds/${buildRowId}`
      + `/download?token=${encodeURIComponent(token)}`
  },
  listFrozen() {
    return api.get<{ frozen: FrozenDataset[]; collector_configured: boolean }>(
      '/replay-datasets/frozen')
  },
  removeFrozen(name: string, force = false) {
    return api.delete(`/replay-datasets/frozen/${encodeURIComponent(name)}`,
      { params: { force } })
  },
}

// One row in the super_admin password-reset queue.
export interface PasswordResetRequest {
  id: number
  user_id: number
  username: string
  email: string
  role: 'super_admin' | 'admin' | 'service' | 'user'
  status: 'pending' | 'approved' | 'used' | 'expired' | 'canceled'
  created_at: string
  approved_at: string | null
  expires_at: string | null
}

// Returned once on approval — carries the plaintext token + a site-relative
// reset path. The caller prepends its own origin to build the full link.
export interface PasswordResetApproval {
  token: string
  token_prefix: string
  expires_at: string
  path: string
}

export const passwordResetApi = {
  // Public — no auth. File a request (self-service forgot-password).
  requestReset(email: string) {
    return api.post<{ message: string }>('/auth/forgot-password', { email })
  },
  // Public — no auth. Consume an approved token to set a new password.
  reset(token: string, newPassword: string) {
    return api.post<void>('/auth/reset-password', { token, new_password: newPassword })
  },
  // super_admin only.
  list() {
    return api.get<PasswordResetRequest[]>('/auth/admin/password-resets')
  },
  approve(id: number) {
    return api.post<PasswordResetApproval>(`/auth/admin/password-resets/${id}/approve`)
  },
  reject(id: number) {
    return api.post<void>(`/auth/admin/password-resets/${id}/reject`)
  },
}

export const benchmarksApi = {
  list() {
    return api.get<{ benchmarks: Benchmark[] }>('/benchmarks')
  },
  get(slug: string) {
    return api.get<Benchmark>(`/benchmarks/${slug}`)
  },
  adminList() {
    return api.get<{ benchmarks: Benchmark[] }>('/benchmarks/admin/benchmarks')
  },
  create(data: {
    slug: string
    name: string
    description?: string
    version?: string
    status?: string
    group_tags?: string[]
    modules: { module_name: string; params_json: Record<string, any>; metric_configs: MetricConfig[]; weight: number; order_index: number; skip_if_prev_failed?: boolean }[]
  }) {
    return api.post<Benchmark>('/benchmarks/admin/benchmarks', data)
  },
  update(id: number, data: Partial<Benchmark> & { modules?: Benchmark['modules'] }) {
    return api.put<Benchmark>(`/benchmarks/admin/benchmarks/${id}`, data)
  },
  delete(id: number) {
    return api.delete(`/benchmarks/admin/benchmarks/${id}`)
  },
  toggleLock(id: number) {
    return api.put<Benchmark>(`/benchmarks/admin/benchmarks/${id}/lock`)
  },
  exportYaml(id: number) {
    return api.get(`/benchmarks/admin/benchmarks/${id}/export`, { responseType: 'blob' })
  },
  importYaml(file: File) {
    const form = new FormData()
    form.append('file', file)
    return api.post<Benchmark>('/benchmarks/admin/benchmarks/import', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  // Rewrite group_tags on many benchmarks in one transaction — what the admin
  // Groups tab saves. Renaming or deleting a group touches every benchmark
  // carrying that path, and doing that as N separate updates could leave the
  // grouping half-renamed if one call failed.
  bulkGroupTags(assignments: { benchmark_id: number; group_tags: string[] }[]) {
    return api.put<{ updated: number }>('/benchmarks/admin/benchmarks/group-tags', { assignments })
  },
}

// Caller's active-submission quota — the submit pages warn and disable the
// submit button when active >= limit (exempt admins are never blocked).
export interface QuotaStatus {
  limit: number
  active: number
  exempt: boolean
  active_ids: number[]
}

export const submissionsApi = {
  quota() {
    return api.get<QuotaStatus>('/submissions/quota')
  },
  submitBenchmark(
    slug: string,
    data: {
      endpoint_url: string
      model: string
      api_key: string
      contributor?: string | null
      extra_params?: SubmissionExtraParams | null
      cards_per_machine?: number | null
      machine_count?: number | null
      card_type?: string | null
      description_summary?: string | null
      description_detail?: string | null
      source_url?: string | null
    },
  ) {
    return api.post<Submission>(`/submissions/benchmarks/${slug}/submit`, data)
  },
  submitModule(
    name: string,
    data: {
      endpoint_url: string
      model: string
      api_key: string
      contributor?: string | null
      extra_params?: SubmissionExtraParams | null
      cards_per_machine?: number | null
      machine_count?: number | null
      card_type?: string | null
      description_summary?: string | null
      description_detail?: string | null
      source_url?: string | null
    },
  ) {
    return api.post<Submission>(`/submissions/modules/${name}/submit`, data)
  },
  // Quick endpoint probe before submitting an expensive run.
  preflight(data: { endpoint_url: string; model: string; api_key: string }) {
    return api.post<PreflightResult>('/submissions/preflight', data)
  },
  // Probe an LLM-judge endpoint (module judge_* params) with a real
  // grading-shaped call at the configured judge_max_tokens.
  judgePreflight(data: { endpoint_url: string; model: string; api_key: string; max_tokens: number }) {
    return api.post<PreflightResult>('/submissions/judge-preflight', data)
  },
  get(id: number) {
    return api.get<SubmissionDetail>(`/submissions/${id}`)
  },
  listMine() {
    return api.get<Submission[]>('/submissions/me')
  },
  // Admin only: all submissions across users/benchmarks, newest first, paginated.
  listAll(params: { limit: number; offset: number }) {
    return api.get<PaginatedSubmissions>('/submissions/admin/all', { params })
  },
  cancel(id: number) {
    return api.post<Submission>(`/submissions/${id}/cancel`)
  },
  downloadLogs(id: number) {
    return api.get(`/submissions/${id}/logs/download`, { responseType: 'blob' })
  },
}

export const leaderboardApi = {
  get(slug: string) {
    return api.get<Leaderboard>(`/leaderboard/${slug}`)
  },
}

export interface BuildInfo {
  component: string
  git_sha: string
  git_ref: string
  build_time: string
  image_tag: string
}

export interface VersionResponse {
  backend: BuildInfo
  alembic_revision: string | null
}

export const adminApi = {
  version() {
    return api.get<VersionResponse>('/admin/version')
  },
  // Frontend bundle's build-info.json is served by nginx, not the API.
  // Use a separate axios instance with no /api prefix.
  frontendBuildInfo() {
    return axios.get<BuildInfo>('/build-info.json', {
      headers: { 'Content-Type': 'application/json' },
    })
  },
}
