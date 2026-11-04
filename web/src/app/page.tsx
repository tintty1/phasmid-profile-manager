'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AppWindow,
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  Ellipsis,
  EyeOff,
  Globe,
  Lock,
  LockOpen,
  Monitor,
  PackageOpen,
  Pencil,
  Play,
  Plus,
  Search,
  Square,
  Terminal,
  Trash2,
  Upload,
  Users,
} from 'lucide-react'

import { BrowserSetup } from '@/components/browser-setup'
import { BrowserViewer } from '@/components/browser-viewer'
import { EmptyState } from '@/components/empty-state'
import { ConfirmDialog, Modal } from '@/components/modal'
import { ProfileForm } from '@/components/profile-form'
import { useToast } from '@/components/toast'
import {
  browsersAPI,
  formatLastUsed,
  formatProxyString,
  groupsAPI,
  hasGeography,
  OS_LABELS,
  cdpUrl,
  profilesAPI,
  readProxyCheck,
  type ActiveBrowser,
  type BrowserModes,
  type DisplayMode,
  type Group,
  type Profile,
  type ProxyCheckRecord,
} from '@/lib/api'

const MODE_LABELS: Record<DisplayMode, { label: string; hint: string; icon: React.ReactNode }> = {
  headless: { label: 'Headless', hint: 'No window; for automation', icon: <EyeOff size={13} /> },
  virtual: { label: 'Virtual display', hint: 'Watch and use it here', icon: <Monitor size={13} /> },
  window: { label: 'Desktop window', hint: "On the server's own screen", icon: <AppWindow size={13} /> },
}

type SortKey = 'name' | 'id' | 'group' | 'os' | 'status' | 'last_used'

const COLUMNS: { key: SortKey; label: string; className?: string }[] = [
  { key: 'name', label: 'Name' },
  { key: 'id', label: 'ID' },
  { key: 'group', label: 'Group' },
  { key: 'os', label: 'OS' },
  { key: 'status', label: 'Status' },
  { key: 'last_used', label: 'Last used' },
]

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [running, setRunning] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<Set<string>>(new Set())
  // Running profiles the server shows on a VNC display, and the one being viewed.
  const [viewable, setViewable] = useState<Set<string>>(new Set())
  const [viewing, setViewing] = useState<Profile | null>(null)
  // What each running browser is (mode, CDP), and what this server can launch.
  const [active, setActive] = useState<Map<string, ActiveBrowser>>(new Map())
  const [modes, setModes] = useState<BrowserModes | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [sortKey, setSortKey] = useState<SortKey>('name')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [page, setPage] = useState(1)
  const perPage = 25

  const [checkingProxies, setCheckingProxies] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Profile | null>(null)
  const [confirm, setConfirm] = useState<null | {
    title: string
    body: string
    label: string
    run: () => Promise<void>
  }>(null)
  const [exportOpen, setExportOpen] = useState(false)

  const toast = useToast()
  const importRef = useRef<HTMLInputElement>(null)
  const archiveRef = useRef<HTMLInputElement>(null)

  const loadProfiles = useCallback(async () => {
    try {
      setError(null)
      // The API paginates; pull every page so sorting and search stay client-side
      // and instant. Guarded so a bad has_next can never spin forever.
      const collected: Profile[] = []
      for (let pageNumber = 1; pageNumber <= 200; pageNumber += 1) {
        const response = await profilesAPI.getProfiles({ page: pageNumber, per_page: 100 })
        collected.push(...response.profiles)
        if (!response.has_next) break
      }
      setProfiles(collected)
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadRunning = useCallback(async () => {
    try {
      const response = await browsersAPI.active()
      setRunning(new Set(response.active_browsers.map((browser) => browser.profile_id)))
      setViewable(
        new Set(response.active_browsers.filter((browser) => browser.vnc).map((browser) => browser.profile_id)),
      )
      setActive(new Map(response.active_browsers.map((browser) => [browser.profile_id, browser])))
      return response.active_browsers
    } catch {
      // The poll is best-effort; a transient failure must not surface as an error.
      return null
    }
  }, [])

  const loadGroups = useCallback(async () => {
    try {
      const response = await groupsAPI.list()
      setGroups(response.groups)
    } catch {
      // Groups are optional context for the table and the form.
    }
  }, [])

  useEffect(() => {
    // loadProfiles clears the previous error synchronously before awaiting,
    // which is the one render the rule objects to and is what makes a reload
    // stop showing a stale failure.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadProfiles()
    loadGroups()
    loadRunning()
    // Without it the Run menu is hidden and Run uses the server's default.
    browsersAPI.modes().then(setModes, () => {})
    const timer = setInterval(loadRunning, 5000)
    return () => clearInterval(timer)
  }, [loadProfiles, loadGroups, loadRunning])

  useEffect(() => {
    // Close on any click that is not on a trigger or inside an open menu.
    // Relying on stopPropagation in the trigger's React handler is not enough:
    // React delegates to its root container, so the document listener could
    // still fire and shut the menu in the same click that opened it.
    const close = (event: MouseEvent) => {
      const target = event.target as Element | null
      if (target?.closest('[data-menu-trigger], [role="menu"]')) return
      setMenuFor(null)
    }
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [])

  // Go back to the first page when the filters change. Adjusted during render
  // rather than in an effect: an effect would paint the new results clamped to
  // whatever page number survived, then snap to the first one, and React
  // documents this as the way to derive state from a change in props or state.
  const [filterForPage, setFilterForPage] = useState(() => ({ search, statusFilter }))
  if (filterForPage.search !== search || filterForPage.statusFilter !== statusFilter) {
    setFilterForPage({ search, statusFilter })
    setPage(1)
  }

  const groupNames = useMemo(
    () => new Map(groups.map((group) => [group.id, group.name])),
    [groups],
  )

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    const filtered = profiles.filter((profile) => {
      if (statusFilter !== 'all' && profile.status !== statusFilter) return false
      if (!needle) return true
      return (
        profile.name.toLowerCase().includes(needle) || profile.id.toLowerCase().includes(needle)
      )
    })

    const direction = sortDir === 'asc' ? 1 : -1
    return [...filtered].sort((a, b) => {
      switch (sortKey) {
        case 'id':
          return a.id.localeCompare(b.id) * direction
        case 'group':
          return (groupNames.get(a.group ?? '') ?? '').localeCompare(
            groupNames.get(b.group ?? '') ?? '',
          ) * direction
        case 'os':
          return (a.browser_settings?.os ?? '').localeCompare(b.browser_settings?.os ?? '') * direction
        case 'status':
          return a.status.localeCompare(b.status) * direction
        case 'last_used':
          return (
            (new Date(a.last_used ?? 0).getTime() - new Date(b.last_used ?? 0).getTime()) * direction
          )
        default:
          return a.name.localeCompare(b.name) * direction
      }
    })
  }, [profiles, search, statusFilter, sortKey, sortDir, groupNames])

  // Offering the bulk clear when nothing selected would change is just noise.
  const selectedWithGeography = useMemo(
    () => profiles.filter((profile) => selected.has(profile.id) && hasGeography(profile)).length,
    [profiles, selected],
  )

  // Same rule as the clear above: a profile with no proxy has nothing to check.
  const selectedWithProxy = useMemo(
    () => profiles.filter((profile) => selected.has(profile.id) && profile.proxy_config).length,
    [profiles, selected],
  )

  const totalPages = Math.max(1, Math.ceil(visible.length / perPage))
  // Deleting the last rows of a page must not strand the user on an empty one.
  const currentPage = Math.min(page, totalPages)
  const pageRows = visible.slice((currentPage - 1) * perPage, currentPage * perPage)
  const allOnPageSelected = pageRows.length > 0 && pageRows.every((row) => selected.has(row.id))

  /** Close the row menu and hand focus back to the button that opened it.
   *
   * Focus moves first: removing the menu while one of its items still has focus
   * drops focus to <body>, and a refocus queued after the close races that reset.
   */
  function closeMenu(menuKey: string) {
    document.querySelector<HTMLButtonElement>(`[data-menu-trigger="${menuKey}"]`)?.focus()
    setMenuFor(null)
  }

  /** Arrow keys move through a menu's enabled items; Escape closes it. */
  function menuKeyDown(event: React.KeyboardEvent<HTMLDivElement>, menuKey: string) {
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'),
    )
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    if (event.key === 'Escape') {
      event.stopPropagation()
      closeMenu(menuKey)
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      items[(index + 1) % items.length]?.focus()
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      items[(index - 1 + items.length) % items.length]?.focus()
    }
  }

  async function copyCdpUrl(profile: Profile) {
    const url = cdpUrl(profile.id)
    const hint =
      'Connect with chromium.connect_over_cdp(url) in Playwright, or puppeteer.connect({ browserWSEndpoint: url }).'
    try {
      await navigator.clipboard.writeText(url)
      toast('ok', 'CDP URL copied', hint)
    } catch {
      toast('info', 'CDP URL', url)
    }
  }

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    else {
      setSortKey(key)
      setSortDir('asc')
    }
  }

  async function withBusy(id: string, action: () => Promise<void>) {
    setBusy((current) => new Set(current).add(id))
    try {
      await action()
    } finally {
      setBusy((current) => {
        const next = new Set(current)
        next.delete(id)
        return next
      })
    }
  }

  async function launch(profile: Profile, mode?: DisplayMode) {
    await withBusy(profile.id, async () => {
      try {
        await profilesAPI.startProfile(profile.id, mode)
        const [active] = await Promise.all([loadRunning(), loadProfiles()])
        // On a server the browser has no window of its own to look at.
        if (active?.some((browser) => browser.profile_id === profile.id && browser.vnc)) {
          setViewing(profile)
        }
      } catch (err) {
        toast('error', 'Could not launch browser', String(err))
      }
    })
  }

  async function stop(profile: Profile) {
    await withBusy(profile.id, async () => {
      try {
        await profilesAPI.closeProfile(profile.id)
        setViewing((current) => (current?.id === profile.id ? null : current))
        await loadRunning()
      } catch (err) {
        toast('error', 'Could not close browser', String(err))
      }
    })
  }

  function download(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  async function exportArchive(profile: Profile) {
    toast('info', 'Packing the profile…', 'Cookies and history make this take a moment.')
    try {
      const blob = await profilesAPI.exportArchive(profile.id)
      const safe = profile.name.replace(/[^A-Za-z0-9_.-]+/g, '-').replace(/^-|-$/g, '')
      download(blob, `${safe || profile.id}.phasmid.zip`)
      toast(
        'ok',
        'Profile exported',
        'The archive holds session cookies and the proxy password — keep it like a password.',
      )
    } catch (err) {
      toast('error', 'Could not export the profile', String(err))
    }
  }

  async function importArchive(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    try {
      const profile = await profilesAPI.importArchive(file)
      toast('ok', 'Profile imported', profile.name)
      loadProfiles()
    } catch (err) {
      toast('error', 'Could not import the profile', String(err))
    }
  }

  async function clone(profile: Profile) {
    try {
      await profilesAPI.cloneProfile(profile.id, `${profile.name} copy`)
      toast('ok', 'Profile cloned', `${profile.name} copy`)
      loadProfiles()
    } catch (err) {
      toast('error', 'Could not clone profile', String(err))
    }
  }

  /**
   * Check one proxy and leave the answer in its row.
   *
   * The row is the report, not a toast: a toast is gone in four seconds, and the
   * question this answers — is this proxy alive, and does it agree with the
   * profile — is one the list should keep showing. Only a failure to reach *our
   * own API* is worth interrupting for, because then no row can say anything.
   */
  async function checkProxy(profile: Profile): Promise<ProxyCheckRecord | null> {
    setCheckingProxies((current) => new Set(current).add(profile.id))
    try {
      const result = await profilesAPI.checkProxy(profile.id)
      const record: ProxyCheckRecord = {
        checked_at: result.checked_at ?? new Date().toISOString(),
        reachable: result.reachable,
        error: result.error,
        latency_ms: result.latency_ms,
        ip: result.location?.ip ?? null,
        country: result.location?.country ?? null,
        timezone: result.location?.timezone ?? null,
        findings: result.findings,
      }
      setProfiles((current) =>
        current.map((row) => (row.id === profile.id ? { ...row, proxy_check: record } : row)),
      )
      return record
    } catch (err) {
      toast('error', 'Could not check the proxy', String(err))
      return null
    } finally {
      setCheckingProxies((current) => {
        const next = new Set(current)
        next.delete(profile.id)
        return next
      })
    }
  }

  /**
   * Check every selected profile that has a proxy.
   *
   * A few at a time: a hundred proxies opened at once is a hundred sockets and a
   * self-inflicted timeout, and rows landing one by one reads as progress where
   * a single wait reads as a hang.
   */
  async function checkSelectedProxies() {
    const queue = profiles.filter((profile) => selected.has(profile.id) && profile.proxy_config)
    if (!queue.length) return

    // Counted before the workers start draining the queue.
    const total = queue.length
    // Three outcomes, not two: a check that never reached our own API left its
    // row untouched, and reporting that as success is how a summary ends up
    // contradicting the table it summarises.
    let clean = 0
    let flagged = 0
    let unchecked = 0
    const workers = Array.from({ length: Math.min(5, total) }, async () => {
      for (let next = queue.shift(); next; next = queue.shift()) {
        const record = await checkProxy(next)
        if (!record) unchecked += 1
        else if (readProxyCheck(record).tone === 'ok') clean += 1
        else flagged += 1
      }
    })
    await Promise.all(workers)

    // Worded from the same rule the dots use, so a red row is never counted as
    // an answer just because the proxy replied.
    const parts = [
      flagged ? `${flagged} need attention` : null,
      unchecked ? `${unchecked} could not be checked` : null,
    ].filter(Boolean)

    if (parts.length)
      toast('error', parts.join(', '), `of ${total} selected. The rows carry the detail.`)
    else toast('ok', `${clean} ${clean === 1 ? 'proxy is' : 'proxies are'} healthy`)
  }

  function askDelete(profile: Profile) {
    setConfirm({
      title: 'Move profile to Trash',
      body: `"${profile.name}" and its browser data will move to Trash. You can restore them from the sidebar.`,
      label: 'Move to Trash',
      run: async () => {
        await profilesAPI.deleteProfile(profile.id)
        toast('ok', 'Profile moved to Trash', profile.name)
        setSelected((current) => {
          const next = new Set(current)
          next.delete(profile.id)
          return next
        })
        loadProfiles()
      },
    })
  }

  async function toggleProtected(profile: Profile) {
    try {
      await profilesAPI.updateProfile(profile.id, {
        protected: !profile.protected,
        row_version: profile.row_version,
      })
      toast(
        'ok',
        profile.protected ? 'Profile unprotected' : 'Profile protected',
        profile.protected
          ? `${profile.name} can be moved to Trash again`
          : `${profile.name} can't be moved to Trash or given a new machine`,
      )
      loadProfiles()
    } catch (err) {
      toast('error', 'Could not change protection', String(err))
    }
  }

  function askBulkDelete() {
    // Protected profiles are left where they are, and the dialog says so up
    // front rather than failing halfway through the selection.
    const chosen = profiles.filter((p) => selected.has(p.id))
    const deletable = chosen.filter((p) => !p.protected)
    const skipped = chosen.length - deletable.length
    const count = deletable.length
    setConfirm({
      title: `Move ${count} profile${count === 1 ? '' : 's'} to Trash`,
      body:
        'The selected profiles and their browser data will move to Trash. You can restore them from the sidebar.' +
        (skipped
          ? ` ${skipped} protected profile${skipped === 1 ? ' is' : 's are'} skipped.`
          : ''),
      label: 'Move to Trash',
      run: async () => {
        for (const profile of deletable) await profilesAPI.deleteProfile(profile.id)
        toast('ok', `Moved ${count} profile${count === 1 ? '' : 's'} to Trash`)
        setSelected(new Set())
        loadProfiles()
      },
    })
  }

  /**
   * A stated timezone wins over the proxy at every launch, so profiles moved to
   * proxies elsewhere tend to need it cleared together — the useful unit is a
   * selection, not one profile at a time. Only those that actually state one
   * are named, so the confirmation says what will really change.
   */
  function askClearGeography() {
    const ids = profiles.filter((p) => selected.has(p.id) && hasGeography(p)).map((p) => p.id)
    setConfirm({
      title: `Clear the timezone of ${ids.length} profile${ids.length === 1 ? '' : 's'}`,
      body:
        'The stored timezone is unset, so the next launch moves each machine to where its ' +
        'proxy comes out — zone, position and WebRTC address. Languages and the hardware ' +
        'are untouched.',
      label: 'Clear',
      run: async () => {
        const result = await profilesAPI.clearGeography(ids)
        toast('ok', `Cleared ${result.cleared.length} profile${result.cleared.length === 1 ? '' : 's'}`)
        setSelected(new Set())
        loadProfiles()
      },
    })
  }

  function askCloseAll() {
    setConfirm({
      title: 'Close all browsers',
      body: `${running.size} running browser${running.size === 1 ? '' : 's'} will be closed.`,
      label: 'Close all',
      run: async () => {
        const result = await browsersAPI.closeAll()
        toast('ok', result.message)
        loadRunning()
      },
    })
  }

  async function runConfirmed() {
    if (!confirm) return
    const action = confirm
    setConfirm(null)
    try {
      await action.run()
    } catch (err) {
      toast('error', 'Action failed', String(err))
    }
  }

  async function exportExcel() {
    setExportOpen(false)
    try {
      const blob = await profilesAPI.exportExcel()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'phasmid-profiles.xlsx'
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      toast('ok', 'Exported to phasmid-profiles.xlsx', 'The file contains proxy passwords in clear text.')
    } catch (err) {
      toast('error', 'Export failed', String(err))
    }
  }

  async function importExcel(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    try {
      const result = await profilesAPI.importExcel(file)
      if (result.success) {
        toast('ok', `Imported ${result.data?.created_count ?? 0} profiles`)
        loadProfiles()
      } else {
        toast(
          'error',
          'Import finished with errors',
          (result.data?.errors ?? []).slice(0, 3).join('\n') || result.message,
        )
      }
    } catch (err) {
      toast('error', 'Import failed', String(err))
    }
  }

  return (
    <>
      <header className="sticky top-0 z-20 flex h-[52px] items-center gap-3 border-b border-line bg-canvas/85 px-5 backdrop-blur">
        <h1 className="text-[14px] font-semibold">Profiles</h1>
        <span className="font-mono text-ink-faint">{visible.length}</span>

        <div className="relative ml-3 w-[240px]">
          <Search
            size={13}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint"
          />
          <input
            className="field h-[30px] pl-7"
            placeholder="Search name or ID"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Search profiles"
          />
        </div>

        <select
          className="field h-[30px] w-[132px]"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
          aria-label="Filter by status"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
          <option value="blocked">Blocked</option>
          <option value="maintenance">Maintenance</option>
        </select>

        <div className="ml-auto flex items-center gap-2">
          {running.size > 0 && (
            <button className="btn btn-default" onClick={askCloseAll}>
              <Square size={12} fill="currentColor" />
              Close {running.size} running
            </button>
          )}
          <button className="btn btn-ghost" onClick={() => setExportOpen(true)} title="Export to Excel">
            <Download size={14} />
          </button>
          <button
            className="btn btn-ghost"
            onClick={() => importRef.current?.click()}
            title="Import from Excel"
          >
            <Upload size={14} />
          </button>
          <input
            ref={importRef}
            type="file"
            accept=".xlsx,.xls"
            onChange={importExcel}
            className="hidden"
          />
          <button
            className="btn btn-ghost"
            onClick={() => archiveRef.current?.click()}
            title="Import a profile archive"
          >
            <PackageOpen size={14} />
          </button>
          <input
            ref={archiveRef}
            type="file"
            accept=".zip"
            onChange={importArchive}
            className="hidden"
          />
          <button
            className="btn btn-primary"
            onClick={() => {
              setEditing(null)
              setFormOpen(true)
            }}
          >
            <Plus size={14} strokeWidth={2.5} />
            New profile
          </button>
        </div>
      </header>

      {selected.size > 0 && (
        <div className="flex items-center gap-3 border-b border-line bg-raised px-5 py-2">
          <span>{selected.size} selected</span>
          {selectedWithProxy > 0 && (
            <button
              className="btn btn-default h-7"
              disabled={checkingProxies.size > 0}
              onClick={checkSelectedProxies}
            >
              <Globe size={13} />
              {checkingProxies.size > 0
                ? `Checking… (${checkingProxies.size})`
                : `Check proxies (${selectedWithProxy})`}
            </button>
          )}
          {selectedWithGeography > 0 && (
            <button className="btn btn-default h-7" onClick={askClearGeography}>
              <Globe size={13} />
              Clear timezone ({selectedWithGeography})
            </button>
          )}
          <button className="btn btn-danger h-7" onClick={askBulkDelete}>
            <Trash2 size={13} />
            Move to Trash
          </button>
          <button className="btn btn-ghost h-7" onClick={() => setSelected(new Set())}>
            Clear
          </button>
        </div>
      )}

      {!loading && !error && <BrowserSetup firstProfile={profiles.length === 0} onCreate={() => { setEditing(null); setFormOpen(true) }} />}

      {error ? (
        <EmptyState
          icon={<Users size={18} />}
          title="Cannot reach the API"
          body={error}
          action={
            <button className="btn btn-default" onClick={loadProfiles}>
              Retry
            </button>
          }
        />
      ) : loading ? (
        <p className="px-5 py-8 text-ink-faint">Loading…</p>
      ) : profiles.length === 0 ? (
        <EmptyState
          icon={<Users size={18} />}
          title="No profiles yet"
          body="A profile is one isolated browser identity — its own fingerprint, proxy, cookies and storage. Create one to get started."
          action={
            <button
              className="btn btn-primary"
              onClick={() => {
                setEditing(null)
                setFormOpen(true)
              }}
            >
              <Plus size={14} strokeWidth={2.5} />
              Create your first profile
            </button>
          }
        />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<Search size={18} />}
          title="No matches"
          body="No profile matches the current search and filter."
          action={
            <button
              className="btn btn-default"
              onClick={() => {
                setSearch('')
                setStatusFilter('all')
              }}
            >
              Clear filters
            </button>
          }
        />
      ) : (
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-line text-left text-[11px] uppercase tracking-[0.05em] text-ink-faint">
              <th className="w-9 py-2 pl-5">
                <input
                  type="checkbox"
                  className="accent-signal"
                  checked={allOnPageSelected}
                  aria-label="Select all on this page"
                  onChange={() =>
                    setSelected((current) => {
                      const next = new Set(current)
                      pageRows.forEach((row) =>
                        allOnPageSelected ? next.delete(row.id) : next.add(row.id),
                      )
                      return next
                    })
                  }
                />
              </th>
              {COLUMNS.map((column) => (
                <th key={column.key} className="py-2 pr-4 font-medium">
                  <button
                    className="inline-flex items-center gap-1 hover:text-ink"
                    onClick={() => toggleSort(column.key)}
                  >
                    {column.label}
                    {sortKey === column.key &&
                      (sortDir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
                  </button>
                </th>
              ))}
              <th className="py-2 pr-4 font-medium">Proxy</th>
              <th className="w-[160px] py-2 pr-5" />
            </tr>
          </thead>
          <tbody>
            {pageRows.map((profile, index) => {
              const isRunning = running.has(profile.id)
              const isBusy = busy.has(profile.id)
              return (
                <tr
                  key={profile.id}
                  className="row-in group border-b border-line/60 hover:bg-surface"
                  style={{ animationDelay: `${Math.min(index, 12) * 12}ms` }}
                >
                  <td className="relative py-2.5 pl-5">
                    {/* Hairline marker: a running profile is visible at a glance. */}
                    {isRunning && (
                      <span className="absolute left-0 top-0 h-full w-[2px] bg-signal" aria-hidden />
                    )}
                    <input
                      type="checkbox"
                      className="accent-signal"
                      checked={selected.has(profile.id)}
                      aria-label={`Select ${profile.name}`}
                      onChange={() =>
                        setSelected((current) => {
                          const next = new Set(current)
                          if (next.has(profile.id)) next.delete(profile.id)
                          else next.add(profile.id)
                          return next
                        })
                      }
                    />
                  </td>

                  <td className="py-2.5 pr-4 font-medium">
                    <span className="inline-flex items-center gap-1.5">
                      {profile.name}
                      {profile.protected && (
                        <span
                          className="inline-flex text-ink-faint"
                          role="img"
                          aria-label="Protected"
                          title="Protected: it cannot be moved to Trash or given a new machine"
                        >
                          <Lock size={12} />
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 font-mono text-ink-faint">{profile.id}</td>
                  <td className="py-2.5 pr-4 text-ink-dim">
                    {profile.group ? (groupNames.get(profile.group) ?? profile.group) : '—'}
                  </td>
                  <td className="py-2.5 pr-4 text-ink-dim">
                    {OS_LABELS[profile.browser_settings?.os ?? ''] ?? profile.browser_settings?.os}
                  </td>
                  <td className="py-2.5 pr-4">
                    <StatusCell
                      status={profile.status}
                      running={isRunning}
                      mode={active.get(profile.id)?.mode}
                    />
                  </td>
                  <td className="py-2.5 pr-4 text-ink-dim">{formatLastUsed(profile.last_used)}</td>
                  <td className="py-2.5 pr-4">
                    <ProxyCell
                      profile={profile}
                      checking={checkingProxies.has(profile.id)}
                    />
                  </td>

                  <td className="py-2.5 pr-5">
                    <div className="flex items-center justify-end gap-1">
                      {isRunning && viewable.has(profile.id) && (
                        <button
                          className="btn btn-ghost h-7 w-7 p-0"
                          aria-label={`Open the browser for ${profile.name}`}
                          title="Open the browser to watch and use it"
                          onClick={() => setViewing(profile)}
                        >
                          <Monitor size={13} />
                        </button>
                      )}
                      {isRunning ? (
                        <button className="btn btn-default h-7" disabled={isBusy} onClick={() => stop(profile)}>
                          <Square size={11} fill="currentColor" />
                          Stop
                        </button>
                      ) : (
                        <div className="relative flex">
                          <button
                            className={`btn btn-default h-7 ${modes ? 'rounded-r-none' : ''}`}
                            disabled={isBusy}
                            title={modes ? `Run: ${MODE_LABELS[modes.default].label}` : undefined}
                            onClick={() => launch(profile)}
                          >
                            <Play size={11} fill="currentColor" />
                            Run
                          </button>
                          {modes && (
                            <button
                              className="btn btn-default h-7 w-6 rounded-l-none border-l-0 p-0"
                              disabled={isBusy}
                              aria-label={`Run ${profile.name} in a chosen mode`}
                              aria-haspopup="menu"
                              aria-expanded={menuFor === `run:${profile.id}`}
                              data-menu-trigger={`run:${profile.id}`}
                              onClick={(event) => {
                                event.stopPropagation()
                                const key = `run:${profile.id}`
                                setMenuFor(menuFor === key ? null : key)
                              }}
                            >
                              <ChevronDown size={12} />
                            </button>
                          )}
                          {modes && menuFor === `run:${profile.id}` && (
                            <div
                              role="menu"
                              aria-label="Run in"
                              className="dialog-in absolute right-0 top-8 z-30 w-[236px] overflow-hidden rounded-md border border-line bg-raised py-1 shadow-xl shadow-black/50"
                              onClick={(event) => event.stopPropagation()}
                              onKeyDown={(event) => menuKeyDown(event, `run:${profile.id}`)}
                            >
                              {modes.modes.map((option) => (
                                <MenuItem
                                  key={option.mode}
                                  icon={MODE_LABELS[option.mode].icon}
                                  label={
                                    MODE_LABELS[option.mode].label +
                                    (option.mode === modes.default ? ' (default)' : '')
                                  }
                                  hint={option.reason ?? MODE_LABELS[option.mode].hint}
                                  disabled={!option.available}
                                  autoFocus={
                                    option.mode === modes.modes.find((m) => m.available)?.mode
                                  }
                                  onClick={() => {
                                    closeMenu(`run:${profile.id}`)
                                    launch(profile, option.mode)
                                  }}
                                />
                              ))}
                            </div>
                          )}
                        </div>
                      )}

                      <div className="relative">
                        <button
                          className="btn btn-ghost h-7 w-7 p-0"
                          aria-label={`Actions for ${profile.name}`}
                          aria-haspopup="menu"
                          aria-expanded={menuFor === profile.id}
                          data-menu-trigger={profile.id}
                          onClick={(event) => {
                            event.stopPropagation()
                            setMenuFor(menuFor === profile.id ? null : profile.id)
                          }}
                        >
                          <Ellipsis size={15} />
                        </button>

                        {menuFor === profile.id && (
                          <div
                            role="menu"
                            className="dialog-in absolute right-0 top-8 z-30 w-[184px] overflow-hidden rounded-md border border-line bg-raised py-1 shadow-xl shadow-black/50"
                            onClick={(event) => event.stopPropagation()}
                            onKeyDown={(event) => menuKeyDown(event, profile.id)}
                          >
                            <MenuItem
                              icon={<Pencil size={13} />}
                              label="Edit"
                              autoFocus
                              onClick={() => {
                                setEditing(profile)
                                setFormOpen(true)
                                closeMenu(profile.id)
                              }}
                            />
                            <MenuItem
                              icon={<Copy size={13} />}
                              label="Duplicate"
                              onClick={() => {
                                clone(profile)
                                closeMenu(profile.id)
                              }}
                            />
                            <MenuItem
                              icon={<Globe size={13} />}
                              label="Check proxy"
                              onClick={() => {
                                // The set holds ids, not a count, so a second
                                // check of the same row would clear the first
                                // one's "Checking…" when it finished.
                                if (!checkingProxies.has(profile.id)) checkProxy(profile)
                                closeMenu(profile.id)
                              }}
                            />
                            {active.get(profile.id)?.cdp && (
                              <MenuItem
                                icon={<Terminal size={13} />}
                                label="Copy CDP URL"
                                onClick={() => {
                                  copyCdpUrl(profile)
                                  closeMenu(profile.id)
                                }}
                              />
                            )}
                            <MenuItem
                              icon={<PackageOpen size={13} />}
                              label="Export…"
                              onClick={() => {
                                exportArchive(profile)
                                closeMenu(profile.id)
                              }}
                            />
                            <MenuItem
                              icon={profile.protected ? <LockOpen size={13} /> : <Lock size={13} />}
                              label={profile.protected ? 'Unprotect' : 'Protect'}
                              onClick={() => {
                                toggleProtected(profile)
                                closeMenu(profile.id)
                              }}
                            />
                            <MenuItem
                              icon={<Trash2 size={13} />}
                              label="Move to Trash"
                              hint={profile.protected ? 'Unprotect it first' : undefined}
                              disabled={profile.protected}
                              danger
                              onClick={() => {
                                askDelete(profile)
                                closeMenu(profile.id)
                              }}
                            />
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between px-5 py-3 text-ink-dim">
          <span>
            {(currentPage - 1) * perPage + 1}–{Math.min(currentPage * perPage, visible.length)} of{' '}
            {visible.length}
          </span>
          <div className="flex items-center gap-1">
            <button
              className="btn btn-ghost h-7 w-7 p-0"
              disabled={currentPage === 1}
              onClick={() => setPage(currentPage - 1)}
              aria-label="Previous page"
            >
              <ChevronLeft size={15} />
            </button>
            <span className="px-2 font-mono">
              {currentPage} / {totalPages}
            </span>
            <button
              className="btn btn-ghost h-7 w-7 p-0"
              disabled={currentPage === totalPages}
              onClick={() => setPage(currentPage + 1)}
              aria-label="Next page"
            >
              <ChevronRight size={15} />
            </button>
          </div>
        </div>
      )}

      <ProfileForm
        open={formOpen}
        profile={editing}
        groups={groups}
        onClose={() => setFormOpen(false)}
        onSaved={loadProfiles}
      />

      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.title ?? ''}
        body={confirm?.body ?? ''}
        confirmLabel={confirm?.label}
        destructive={confirm?.label === 'Delete'}
        onConfirm={runConfirmed}
        onCancel={() => setConfirm(null)}
      />

      {viewing && (
        <BrowserViewer
          profileId={viewing.id}
          profileName={viewing.name}
          onClose={() => setViewing(null)}
          onStop={() => stop(viewing)}
        />
      )}

      <Modal
        open={exportOpen}
        title="Export to Excel"
        onClose={() => setExportOpen(false)}
        width={440}
        footer={
          <>
            <button className="btn btn-default" onClick={() => setExportOpen(false)}>
              Cancel
            </button>
            <button className="btn btn-primary" onClick={exportExcel}>
              Export
            </button>
          </>
        }
      >
        <p className="text-ink-dim">
          The spreadsheet includes every profile setting so it can be imported back — including
          <span className="text-ink"> proxy passwords in clear text</span>. Store the file somewhere
          you would keep the passwords themselves.
        </p>
      </Modal>
    </>
  )
}

/** The configured proxy, and under it the last answer it gave.
 *
 * Two lines rather than a column of its own: the table is already wide, and the
 * second line only exists once there is something to say, so a list nobody has
 * checked looks exactly as it did before.
 */
function ProxyCell({ profile, checking }: { profile: Profile; checking: boolean }) {
  const configured = formatProxyString(profile.proxy_config) || '—'
  const check = profile.proxy_check

  return (
    // Bounded, because a table cell grows to fit its content: an unreachable
    // proxy reports a whole sentence, and without a cap one dead proxy pushed
    // the row actions off the screen and gave the table a scrollbar.
    <div className="max-w-[280px] leading-tight">
      <div className="truncate font-mono text-ink-faint">{configured}</div>
      {checking ? (
        <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-ink-faint">
          <span className="signal-pulse h-1.5 w-1.5 rounded-full bg-signal" />
          Checking…
        </div>
      ) : check ? (
        <ProxyResult check={check} />
      ) : null}
    </div>
  )
}

function ProxyResult({ check }: { check: ProxyCheckRecord }) {
  const { tone, label, detail } = readProxyCheck(check)
  const dot = tone === 'ok' ? 'bg-ok' : tone === 'warn' ? 'bg-warn' : 'bg-danger'
  // Latency and country only mean something when the proxy answered; when it did
  // not, the reason is the only thing worth the space.
  const parts = check.reachable
    ? [check.ip, check.country, check.latency_ms !== null ? `${check.latency_ms} ms` : null]
    : [check.error]

  return (
    <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-ink-faint" title={detail}>
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      <span className="sr-only">{label}.</span>
      {/* min-w-0 so this is the part that gives way: a flex item will not shrink
          below its content without it, and the truncation never happens. */}
      <span className="min-w-0 truncate font-mono">{parts.filter(Boolean).join(' · ')}</span>
      <span className="shrink-0 text-ink-dim/70">· {formatLastUsed(check.checked_at)}</span>
    </div>
  )
}

function StatusCell({
  status,
  running,
  mode,
}: {
  status: string
  running: boolean
  mode?: DisplayMode
}) {
  if (running) {
    return (
      <span className="inline-flex items-center gap-1.5 text-signal">
        <span className="signal-pulse h-1.5 w-1.5 rounded-full bg-signal" />
        Running
        {mode && <span className="text-ink-faint">· {MODE_LABELS[mode].label.toLowerCase()}</span>}
      </span>
    )
  }
  const tone =
    status === 'blocked' ? 'bg-danger' : status === 'maintenance' ? 'bg-ink-dim' : 'bg-ink-faint'
  return (
    <span className="inline-flex items-center gap-1.5 capitalize text-ink-dim">
      <span className={`h-1.5 w-1.5 rounded-full ${tone}`} />
      {status}
    </span>
  )
}

function MenuItem({
  icon,
  label,
  hint,
  onClick,
  danger,
  disabled,
  autoFocus,
}: {
  icon: React.ReactNode
  label: string
  /** A second line: what the item does, or why it is unavailable. */
  hint?: string
  onClick: () => void
  danger?: boolean
  disabled?: boolean
  autoFocus?: boolean
}) {
  return (
    <button
      role="menuitem"
      autoFocus={autoFocus}
      disabled={disabled}
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left transition-colors enabled:hover:bg-line enabled:focus:bg-line disabled:cursor-not-allowed disabled:opacity-45 ${
        danger ? 'text-danger' : 'text-ink'
      }`}
    >
      <span className="flex shrink-0">{icon}</span>
      {hint ? (
        <span className="flex min-w-0 flex-col">
          <span>{label}</span>
          <span className="text-ink-faint">{hint}</span>
        </span>
      ) : (
        label
      )}
    </button>
  )
}
