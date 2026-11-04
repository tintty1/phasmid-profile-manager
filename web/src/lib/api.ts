// API client for the Phasmid Profile Manager backend.
//
// The UI is served by the API on the same origin, so the base URL is empty by
// default; NEXT_PUBLIC_API_URL overrides it for split deployments.

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? ''

// Canonical API prefix. The backend still serves the unversioned /api paths as
// aliases for older scripts; the bundled UI always talks to the current one.
const API_PREFIX = '/api/v1'

export type OsFamily = 'linux' | 'windows'

/** Chrome's WebRTC IP handling policy. */
export type WebRTCPolicy =
  | 'default'
  | 'default_public_interface_only'
  | 'default_public_and_private_interfaces'
  | 'disable_non_proxied_udp'

/**
 * What a person chooses about a profile's machine; Phasmid generates the rest.
 *
 * `os` and `gpu` pick the hardware and only take effect when a machine is
 * generated. `languages` and `timezone` unset follow the proxy's exit address,
 * which is the normal case; set, they win over it on the next launch.
 */
export interface BrowserSettings {
  os: OsFamily
  /** "<vendor_id>:<device_id>", e.g. 0x10de:0x2504; null = drawn from the captured devices. */
  gpu?: string | null
  languages?: string[] | null
  timezone?: string | null
  webrtc_policy?: WebRTCPolicy
}

export interface ProxyConfig {
  type?: string
  server?: string
  username?: string | null
  password?: string | null
  country?: string | null
}

/** A short description of the machine a profile is pinned to. */
export interface FingerprintSummary {
  id?: string | null
  seed?: string | null
  os?: string | null
  os_version?: string | null
  platform?: string | null
  hardware_concurrency?: number | null
  /** navigator.deviceMemory, in GB. */
  device_memory?: number | null
  /** "<width>x<height>" */
  screen?: string | null
  device_pixel_ratio?: number | null
  /** The WebGL renderer string pages see. */
  gpu?: string | null
  /** "<vendor_id>:<device_id>" of the pinned GPU. */
  gpu_id?: string | null
  font_count?: number | null
  timezone?: string | null
  languages?: string[] | null
  /** The exit (and WebRTC) address the machine was last moved to. */
  public_ip?: string | null
  has_geolocation?: boolean
  /** The Phasmid version installed, which is the one the machine reports. */
  browser_version?: string | null
  /** The OS the pinned machine itself reports. */
  pinned_os?: string | null
  /** The OS the profile's settings ask for. */
  settings_os?: string | null
  /** The setting names one operating system and the pinned machine another. */
  os_mismatch?: boolean
}

export interface Profile {
  id: string
  name: string
  group?: string | null
  status: string
  browser_settings: BrowserSettings
  proxy_config?: ProxyConfig | null
  storage_path?: string | null
  notes?: string | null
  /** Moving it to Trash or giving it a new machine is refused until it is unprotected. */
  protected?: boolean
  created_at: string
  updated_at?: string
  last_used?: string | null
  fingerprint?: FingerprintSummary | null
  /** Null until the proxy is checked, and again whenever the proxy changes. */
  proxy_check?: ProxyCheckRecord | null
  /**
   * Bumped by every save. Sent back with an edit so that a save someone else
   * landed in the meantime is refused rather than silently overwritten.
   */
  row_version: number
}

export interface ProfilesResponse {
  profiles: Profile[]
  total: number
  page: number
  per_page: number
  has_next: boolean
  has_prev: boolean
}

export interface Group {
  id: string
  name: string
  description?: string | null
  created_at: string
  profile_count: number
}

export interface SystemStatus {
  total_profiles: number
  active_profiles: number
  running_browsers: number
  total_groups: number
  system_load: number
  memory_usage: number
  disk_usage: number
  uptime_seconds: number
}

/**
 * A failed request, carrying enough to act on rather than only to display.
 *
 * The status and the API's own error code are what let a caller tell apart the
 * conflicts that share 409 — someone else saved this profile, versus someone
 * else is running it — and do something better than showing the message.
 */
export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

/** The profile was saved by someone else since this client last read it. */
export function isStaleWrite(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 409 && error.code === 'stale_write'
}

const API_KEY_STORAGE = 'phasmid-pm.api-key'

/** The key PPM_API_KEY expects, if the user has stored one. */
export function getApiKey(): string {
  if (typeof window === 'undefined') return ''
  return window.localStorage.getItem(API_KEY_STORAGE) ?? ''
}

export function setApiKey(key: string): void {
  if (typeof window === 'undefined') return
  if (key) window.localStorage.setItem(API_KEY_STORAGE, key)
  else window.localStorage.removeItem(API_KEY_STORAGE)
}

export function authHeaders(): Record<string, string> {
  const key = getApiKey()
  return key ? { 'X-API-Key': key } : {}
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    // Spread options first: putting it last let a caller's `headers` replace the
    // merged object, silently dropping Content-Type and the API key.
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...(options?.headers ?? {}),
    },
  })
  if (!response.ok) {
    let detail = response.statusText
    let code: string | undefined
    try {
      const body = await response.json()
      // Every API error carries error.message; detail is the legacy mirror,
      // kept as a fallback for anything not yet on the one error shape.
      const raw = body.error?.message ?? body.detail ?? body.message ?? detail
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
      code = body.error?.code
    } catch {
      // response had no JSON body
    }
    throw new ApiRequestError(detail, response.status, code)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function toQuery(params: Record<string, unknown>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  }
  return query.toString() ? `?${query.toString()}` : ''
}

export interface ProxyLocation {
  ip: string
  country: string | null
  timezone: string | null
  latitude: number | null
  longitude: number | null
}

export interface ProxyFinding {
  level: 'error' | 'warning' | 'info'
  field: string
  message: string
}

export interface ProxyCheck {
  reachable: boolean
  error: string | null
  latency_ms: number | null
  location: ProxyLocation | null
  findings: ProxyFinding[]
  /** Null when checking a proxy that is not saved yet, which is recorded nowhere. */
  checked_at: string | null
}

/** The last answer a profile's proxy gave, as the list shows it. */
export interface ProxyCheckRecord {
  checked_at: string
  reachable: boolean
  error: string | null
  latency_ms: number | null
  ip: string | null
  country: string | null
  timezone: string | null
  findings: ProxyFinding[]
}

export const profilesAPI = {
  getProfiles(params: Record<string, unknown> = {}): Promise<ProfilesResponse> {
    return request<ProfilesResponse>(`${API_PREFIX}/profiles${toQuery(params)}`)
  },

  getProfile(id: string): Promise<Profile> {
    return request<Profile>(`${API_PREFIX}/profiles/${id}`)
  },

  createProfile(data: Record<string, unknown>): Promise<Profile> {
    return request<Profile>(`${API_PREFIX}/profiles`, { method: 'POST', body: JSON.stringify(data) })
  },

  updateProfile(id: string, data: Record<string, unknown>): Promise<Profile> {
    return request<Profile>(`${API_PREFIX}/profiles/${id}`, { method: 'PUT', body: JSON.stringify(data) })
  },

  deleteProfile(id: string): Promise<void> {
    return request<void>(`${API_PREFIX}/profiles/${id}`, { method: 'DELETE' })
  },

  cloneProfile(id: string, newName: string): Promise<Profile> {
    return request<Profile>(`${API_PREFIX}/profiles/${id}/clone`, {
      method: 'POST',
      body: JSON.stringify({ new_name: newName }),
    })
  },

  /** Without a mode the server picks its default (see browsersAPI.modes). */
  startProfile(id: string, mode?: DisplayMode): Promise<unknown> {
    return request<unknown>(`${API_PREFIX}/profiles/${id}/launch`, {
      method: 'POST',
      body: JSON.stringify(mode ? { mode } : { headless: false }),
    })
  },

  closeProfile(id: string): Promise<unknown> {
    return request<unknown>(`${API_PREFIX}/profiles/${id}/close`, { method: 'POST' })
  },

  resetFingerprint(id: string): Promise<Profile> {
    return request<Profile>(`${API_PREFIX}/profiles/${id}/reset-fingerprint`, { method: 'POST' })
  },

  /** The whole Phasmid profile JSON the machine is stored as; 404 until one is generated. */
  getMachine(id: string): Promise<Record<string, unknown>> {
    return request<Record<string, unknown>>(`${API_PREFIX}/profiles/${id}/machine`)
  },

  /** keepMachine: move the OS setting to the pin. Otherwise: pin new hardware for the setting. */
  reconcileOs(id: string, keepMachine: boolean): Promise<Profile> {
    return request<Profile>(`${API_PREFIX}/profiles/${id}/reconcile-os`, {
      method: 'POST',
      body: JSON.stringify({ keep_machine: keepMachine }),
    })
  },

  // Takes a list because the profiles this exists for — created when every one
  // was given a random region — are all of them at once.
  clearGeography(ids: string[]): Promise<ClearGeographyResult> {
    return request<ClearGeographyResult>(`${API_PREFIX}/profiles/clear-geography`, {
      method: 'POST',
      body: JSON.stringify({ profile_ids: ids }),
    })
  },

  checkProxy(id: string): Promise<ProxyCheck> {
    return request<ProxyCheck>(`${API_PREFIX}/profiles/${id}/check-proxy`, { method: 'POST' })
  },

  // The same check for a profile that is still being filled in, so the form can
  // answer before anything is saved.
  checkUnsavedProxy(body: {
    proxy_config: Record<string, unknown> | null
    browser_settings: Record<string, unknown>
  }): Promise<ProxyCheck> {
    return request<ProxyCheck>(`${API_PREFIX}/proxy/check`, { method: 'POST', body: JSON.stringify(body) })
  },

  async exportExcel(): Promise<Blob> {
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/profiles/export/excel`, {
      headers: authHeaders(),
    })
    if (!response.ok) throw new Error(response.statusText)
    return response.blob()
  },

  async exportArchive(id: string): Promise<Blob> {
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/profiles/${id}/export`, {
      headers: authHeaders(),
    })
    if (!response.ok) {
      const body = await response.json().catch(() => null)
      throw new Error(body?.detail ?? response.statusText)
    }
    return response.blob()
  },

  async importArchive(file: File): Promise<Profile> {
    const body = new FormData()
    body.append('file', file)
    // No Content-Type: the browser has to set the multipart boundary itself.
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/profiles/import`, {
      method: 'POST',
      headers: authHeaders(),
      body,
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => null)
      throw new Error(detail?.detail ?? response.statusText)
    }
    return response.json() as Promise<Profile>
  },

  async importExcel(file: File): Promise<ImportResult> {
    const body = new FormData()
    body.append('file', file)
    // No Content-Type: the browser has to set the multipart boundary itself.
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/profiles/import/excel`, {
      method: 'POST',
      headers: authHeaders(),
      body,
    })
    return response.json() as Promise<ImportResult>
  },
}

export interface ClearGeographyResult {
  cleared: string[]
  unchanged: string[]
  not_found: string[]
}

export interface ImportResult {
  success: boolean
  message: string
  data?: { created_count?: number; errors?: string[] }
}

/** Where a browser draws: nowhere, its own VNC display on the server, or the server's desktop. */
export type DisplayMode = 'headless' | 'virtual' | 'window'

export interface BrowserModes {
  /** What a launch without a mode uses. */
  default: DisplayMode
  modes: { mode: DisplayMode; available: boolean; reason: string | null }[]
}

export interface ActiveBrowser {
  profile_id: string
  /** Viewable in the browser viewer (the server runs it on its own VNC display). */
  vnc?: boolean
  mode?: DisplayMode
  /** Drivable over CDP at cdpUrl(). */
  cdp?: boolean
}

function profileSocketUrl(profileId: string, channel: string): string {
  const base = new URL(API_BASE_URL || window.location.origin, window.location.href)
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
  base.pathname = `${API_PREFIX}/profiles/${encodeURIComponent(profileId)}/${channel}`
  return base.toString()
}

/** The WebSocket the viewer reads a profile's VNC display from. */
export function vncUrl(profileId: string): string {
  return profileSocketUrl(profileId, 'vnc')
}

/**
 * The CDP WebSocket an automation client connects to:
 * chromium.connect_over_cdp(url) in Playwright, or
 * puppeteer.connect({ browserWSEndpoint: url }).
 */
export function cdpUrl(profileId: string): string {
  return profileSocketUrl(profileId, 'cdp')
}

export const browsersAPI = {
  active(): Promise<{ active_browsers: ActiveBrowser[]; count: number }> {
    return request(`${API_PREFIX}/browsers/active`)
  },

  modes(): Promise<BrowserModes> {
    return request(`${API_PREFIX}/browsers/modes`)
  },

  closeAll(): Promise<{ closed_count: number; message: string }> {
    return request(`${API_PREFIX}/browsers/close-all`, { method: 'POST' })
  },
}

export const groupsAPI = {
  list(): Promise<{ groups: Group[]; total: number }> {
    return request(`${API_PREFIX}/groups`)
  },

  create(data: { name: string; description?: string }): Promise<Group> {
    return request<Group>(`${API_PREFIX}/groups`, { method: 'POST', body: JSON.stringify(data) })
  },

  update(id: string, data: { name?: string; description?: string }): Promise<Group> {
    return request<Group>(`${API_PREFIX}/groups/${id}`, { method: 'PUT', body: JSON.stringify(data) })
  },

  remove(id: string): Promise<void> {
    return request<void>(`${API_PREFIX}/groups/${id}`, { method: 'DELETE' })
  },
}

// --- Schedules ---------------------------------------------------------------

/** What a schedule does when it fires. Hardware regeneration is deliberately
 * not schedulable: it would hand a warmed-up account new hardware on a timer,
 * which is exactly what the pinned machine exists to prevent. Nor is a browser
 * update: Phasmid reports the version installed, for every profile at once. */
export type ScheduleAction = 'launch'

export type ScheduleKind = 'interval' | 'daily'

export type ScheduleRunOutcome = 'ok' | 'skipped' | 'error' | 'missed'

export interface ScheduleRun {
  id: number | null
  schedule_id: string
  started_at: string
  finished_at: string | null
  outcome: ScheduleRunOutcome
  message: string | null
}

export interface Schedule {
  id: string
  profile_id: string
  profile_name: string | null
  action: ScheduleAction
  kind: ScheduleKind
  interval_minutes: number | null
  /** HH:MM on the server's clock. */
  at_time: string | null
  /** Weekdays a daily schedule fires, 0=Monday … 6=Sunday; null = every day. */
  days: number[] | null
  run_minutes: number | null
  enabled: boolean
  next_run_at: string | null
  last_run: ScheduleRun | null
  created_at: string
  updated_at: string
}

export interface SchedulePayload {
  profile_id?: string
  action?: ScheduleAction
  kind?: ScheduleKind
  interval_minutes?: number | null
  at_time?: string | null
  days?: number[] | null
  run_minutes?: number | null
  enabled?: boolean
}

export const schedulesAPI = {
  list(): Promise<{ schedules: Schedule[]; total: number }> {
    return request(`${API_PREFIX}/schedules`)
  },

  create(data: SchedulePayload): Promise<Schedule> {
    return request<Schedule>(`${API_PREFIX}/schedules`, { method: 'POST', body: JSON.stringify(data) })
  },

  update(id: string, data: SchedulePayload): Promise<Schedule> {
    return request<Schedule>(`${API_PREFIX}/schedules/${id}`, { method: 'PUT', body: JSON.stringify(data) })
  },

  remove(id: string): Promise<void> {
    return request<void>(`${API_PREFIX}/schedules/${id}`, { method: 'DELETE' })
  },

  runNow(id: string): Promise<ScheduleRun> {
    return request<ScheduleRun>(`${API_PREFIX}/schedules/${id}/run`, { method: 'POST' })
  },

  runs(id: string): Promise<{ runs: ScheduleRun[]; total: number }> {
    return request(`${API_PREFIX}/schedules/${id}/runs`)
  },
}

export interface SystemConfig {
  version: string
  host: string
  port: number
  database_path: string
  api_key_set: boolean
  user_auth_enabled: boolean
  encryption_enabled: boolean
  cors_origins: string[]
  browser_available: boolean
  browser_version: string | null
  uptime_seconds: number
}

export interface BrowserReadiness {
  installed: boolean
  version: string | null
  state: 'idle' | 'downloading' | 'ready' | 'error'
  progress: number | null
  message: string
  error: string | null
}

export interface AppUpdate {
  current_version: string
  latest_version: string | null
  available: boolean
  release_url: string | null
  assets: { name: string; url: string }[]
  error?: string | null
}

export interface BackupInfo {
  id: string
  profile_id: string
  profile_name: string
  created_at: string
  reason: string
  size_bytes: number
}

export interface BackupResult {
  backups: BackupInfo[]
  skipped: { profile_id: string; reason: string }[]
}

export const recoveryAPI = {
  async trash(): Promise<(Profile & { deleted_at: string })[]> {
    const body = await request<{ data: { profiles: (Profile & { deleted_at: string })[] } }>(`${API_PREFIX}/trash/profiles`)
    return body.data.profiles
  },
  async restoreProfile(id: string): Promise<Profile> {
    const body = await request<{ data: Profile }>(`${API_PREFIX}/trash/profiles/${id}/restore`, { method: 'POST' })
    return body.data
  },
  permanentlyDelete(id: string): Promise<unknown> {
    return request(`${API_PREFIX}/trash/profiles/${id}?confirm=true`, { method: 'DELETE' })
  },
  async backups(): Promise<BackupInfo[]> {
    const body = await request<{ data: { backups: BackupInfo[] } }>(`${API_PREFIX}/system/backups`)
    return body.data.backups
  },
  async createBackup(): Promise<BackupResult> {
    const body = await request<{ data: BackupResult }>(`${API_PREFIX}/system/backups`, { method: 'POST', body: JSON.stringify({ reason: 'manual' }) })
    return body.data
  },
  async restoreBackup(id: string): Promise<Profile> {
    const body = await request<{ data: Profile }>(`${API_PREFIX}/system/backups/${id}/restore`, { method: 'POST', body: JSON.stringify({}) })
    return body.data
  },
}

/** A GPU a generated machine may claim, because a captured host backs it. */
export interface GpuChoice {
  os: string
  /** "<vendor_id>:<device_id>", the browser_settings.gpu value. */
  gpu: string
  /** The WebGL unmasked renderer pages will see. */
  renderer: string
  /** How often it is drawn when no GPU is chosen. */
  weight: number
}

export const fingerprintsAPI = {
  /** Empty until the browser is installed. */
  async gpus(os?: string): Promise<GpuChoice[]> {
    const body = await request<{ data: { gpus: GpuChoice[]; total: number } }>(
      `${API_PREFIX}/fingerprints/gpus${toQuery({ os })}`,
    )
    return body.data.gpus
  },
}

/** The caller's authentication state, from GET /auth/session. */
export interface AuthSession {
  user_auth_enabled: boolean
  authenticated: boolean
  username: string | null
}

// The session lives in an HttpOnly cookie the browser sends by itself on this
// same-origin app; nothing here stores or reads a token.
export const authAPI = {
  session(): Promise<AuthSession> {
    return request<AuthSession>(`${API_PREFIX}/auth/session`)
  },

  login(username: string, password: string): Promise<AuthSession> {
    return request<AuthSession>(`${API_PREFIX}/auth/login`, {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
  },

  logout(): Promise<unknown> {
    return request<unknown>(`${API_PREFIX}/auth/logout`, { method: 'POST' })
  },
}

export const systemAPI = {
  async browser(): Promise<BrowserReadiness> {
    const body = await request<{ data: BrowserReadiness }>(`${API_PREFIX}/system/browser`)
    return body.data
  },
  async installBrowser(): Promise<BrowserReadiness> {
    const body = await request<{ data: BrowserReadiness }>(`${API_PREFIX}/system/browser/install`, { method: 'POST' })
    return body.data
  },
  async updates(): Promise<AppUpdate> {
    const body = await request<{ data: AppUpdate }>(`${API_PREFIX}/system/updates`)
    return body.data
  },
  async prepareUpdate(): Promise<{ release_url: string; backup_count: number; message: string }> {
    const body = await request<{ data: { release_url: string; backup_count: number; message: string } }>(`${API_PREFIX}/system/updates/prepare`, { method: 'POST' })
    return body.data
  },
  status(): Promise<SystemStatus> {
    return request<SystemStatus>(`${API_PREFIX}/system/status`)
  },

  async config(): Promise<SystemConfig> {
    const body = await request<{ data: SystemConfig }>(`${API_PREFIX}/system/config`)
    return body.data
  },
}

// --- Display helpers ---------------------------------------------------------

/** Whether a profile states where it is instead of letting its proxy say. */
export function hasGeography(profile: Profile): boolean {
  return Boolean(profile.browser_settings?.timezone)
}

export function formatProxyString(proxy?: ProxyConfig | null): string {
  if (!proxy || !proxy.server) return ''
  const scheme = proxy.type ? `${proxy.type}://` : ''
  return `${scheme}${proxy.server}`
}

/** How a checked proxy reads: what colour it is, and why.
 *
 * Derived rather than stored, so a row written by an older version cannot
 * disagree with the rules here. An error-level finding is red even when the
 * proxy answered — SOCKS4 credentials the browser cannot send mean the launch
 * is wrong, which is not a milder problem than an unreachable proxy.
 */
export function readProxyCheck(check: ProxyCheckRecord): {
  tone: 'ok' | 'warn' | 'danger'
  label: string
  detail: string
} {
  const notes = check.findings.filter((finding) => finding.level !== 'info')
  const worst = notes.find((finding) => finding.level === 'error') ?? notes[0]
  // Repeats what the row shows on purpose: the line truncates, and an IPv6 exit
  // address is long enough to be cut off mid-address, taking the country and the
  // latency with it. The tooltip is the only place left to read them.
  const lines = [
    check.reachable ? 'Proxy answered' : 'Proxy did not answer',
    check.error,
    check.ip,
    [check.country, check.timezone].filter(Boolean).join(' · ') || null,
    check.latency_ms !== null ? `${check.latency_ms} ms` : null,
    ...notes.map((finding) => finding.message),
    `Checked ${formatLastUsed(check.checked_at).toLowerCase()}`,
  ].filter(Boolean)

  const tone = !check.reachable || worst?.level === 'error' ? 'danger' : worst ? 'warn' : 'ok'

  return {
    tone,
    // Green and amber differ only in hue at six pixels, which is exactly the
    // pair a deuteranope cannot separate. The table's other indicator pairs its
    // dot with a word; this one says the word to screen readers.
    label: tone === 'ok' ? 'Healthy' : tone === 'warn' ? 'Needs attention' : 'Failing',
    detail: lines.join('\n'),
  }
}

export function formatLastUsed(value?: string | null): string {
  if (!value) return 'Never'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Never'

  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (seconds < 60) return 'Just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`
  return date.toLocaleDateString()
}

export const OS_LABELS: Record<string, string> = {
  linux: 'Linux',
  windows: 'Windows',
}

/**
 * Reduce a WebGL renderer string to the card name.
 *
 *   "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)"
 *     -> "NVIDIA GeForce RTX 3060"
 *   "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL 4.5.0)"
 *     -> "NVIDIA GeForce RTX 3060"
 *
 * Unwrapping ANGLE first matters: vendor names contain their own brackets
 * ("Intel(R) UHD Graphics"), so cutting at the first one truncates the name.
 * Anything that does not look like ANGLE comes back as it was.
 */
export function shortGpu(gpu?: string | null): string {
  if (!gpu) return ''
  const angle = gpu.match(/^ANGLE \((.*)\)$/)
  if (!angle) return gpu.trim()
  // vendor, device[, backend]: the device is the second part.
  const parts = angle[1].split(/,\s*/)
  const device = parts.length >= 2 ? parts[1] : parts[0]
  const name = device
    .split(/\s+Direct3D|\s+vs_|\/PCIe|\/SSE2|\s+\(0x[0-9a-f]+\)/i)[0]
    .trim()
  return name || gpu.trim()
}
