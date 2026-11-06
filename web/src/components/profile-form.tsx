'use client'

import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Braces, Check, Copy, Info, LoaderCircle, RefreshCw, TriangleAlert, X } from 'lucide-react'

import { Modal } from '@/components/modal'
import { useToast } from '@/components/toast'
import {
  fingerprintsAPI,
  hasGeography,
  isStaleWrite,
  OS_LABELS,
  profilesAPI,
  shortGpu,
  type FingerprintSummary,
  type GpuChoice,
  type Group,
  type OsFamily,
  type Profile,
  type ProxyCheck,
  type WebRTCPolicy,
} from '@/lib/api'

interface FormState {
  name: string
  group: string
  status: string
  notes: string
  protected: boolean
  os: OsFamily
  /** "" = any captured GPU. */
  gpu: string
  timezone: string
  languages: string
  webrtcPolicy: WebRTCPolicy
  proxyType: string
  proxyServer: string
  proxyUsername: string
  proxyPassword: string
}

const EMPTY: FormState = {
  name: '',
  group: '',
  status: 'active',
  notes: '',
  protected: false,
  os: 'linux',
  gpu: '',
  timezone: '',
  languages: '',
  webrtcPolicy: 'default',
  proxyType: 'http',
  proxyServer: '',
  proxyUsername: '',
  proxyPassword: '',
}

const WEBRTC_POLICIES: { value: WebRTCPolicy; label: string; hint: string }[] = [
  {
    value: 'default',
    label: 'Default (as stock Chrome)',
    hint: 'What an ordinary Chrome does: mDNS already hides local addresses, and the machine reports its proxy exit as its public address.',
  },
  {
    value: 'default_public_interface_only',
    label: 'Public interface only',
    hint: 'WebRTC uses only the default route and never offers local interface addresses.',
  },
  {
    value: 'default_public_and_private_interfaces',
    label: 'Public and private interfaces',
    hint: 'WebRTC uses the default route, including its private address.',
  },
  {
    value: 'disable_non_proxied_udp',
    label: 'Disable non-proxied UDP',
    hint: 'WebRTC goes through the proxy or not at all. Calls may fail on proxies without UDP support.',
  },
]

function fromProfile(profile: Profile): FormState {
  const bs = profile.browser_settings ?? {}
  const proxy = profile.proxy_config ?? {}
  return {
    name: profile.name,
    group: profile.group ?? '',
    status: profile.status,
    notes: profile.notes ?? '',
    protected: profile.protected ?? false,
    os: bs.os === 'windows' ? 'windows' : 'linux',
    gpu: bs.gpu ?? '',
    timezone: bs.timezone ?? '',
    languages: (bs.languages ?? []).join(', '),
    webrtcPolicy: bs.webrtc_policy ?? 'default',
    proxyType: proxy.type ?? 'http',
    proxyServer: proxy.server ?? '',
    proxyUsername: proxy.username ?? '',
    proxyPassword: proxy.password ?? '',
  }
}

/**
 * Which fields differ between the profile this form was opened on and the one
 * now stored — the answer to "changed how?", which is the only part of a
 * conflict a user can act on.
 */
function describeChanges(before: Profile | null, after: Profile): string {
  if (!before) return ''
  const fields: string[] = []
  if (before.name !== after.name) fields.push(`name is now "${after.name}"`)
  if ((before.group ?? null) !== (after.group ?? null))
    fields.push(after.group ? `group is now "${after.group}"` : 'group was cleared')
  if (before.status !== after.status) fields.push(`status is now ${after.status}`)
  if ((before.notes ?? '') !== (after.notes ?? '')) fields.push('notes changed')
  if (Boolean(before.protected) !== Boolean(after.protected))
    fields.push(after.protected ? 'it was protected' : 'it was unprotected')
  if (JSON.stringify(before.proxy_config ?? null) !== JSON.stringify(after.proxy_config ?? null))
    fields.push('the proxy changed')
  if (JSON.stringify(before.browser_settings) !== JSON.stringify(after.browser_settings))
    fields.push('browser settings changed')
  if (!fields.length) return ''
  // Two is enough to recognise what happened; a full list would not fit a toast.
  const shown = fields.slice(0, 2).join(', ')
  return fields.length > 2 ? `${shown}, and ${fields.length - 2} more` : shown
}

interface Props {
  open: boolean
  /** null = create a new profile. */
  profile: Profile | null
  groups: Group[]
  onClose: () => void
  onSaved: () => void
}

export function ProfileForm({ open, profile, groups, onClose, onSaved }: Props) {
  const isEdit = profile !== null
  const [form, setForm] = useState<FormState>(EMPTY)
  const [saving, setSaving] = useState(false)
  const [regenerating, setRegenerating] = useState(false)
  const [checkingProxy, setCheckingProxy] = useState(false)
  const [proxyCheck, setProxyCheck] = useState<ProxyCheck | null>(null)
  // The `profile` prop is a snapshot from the list; regenerating or reconciling
  // returns a new machine, and the panel has to show it without waiting for the
  // dialog to be reopened.
  const [machine, setMachine] = useState<FingerprintSummary | null | undefined>(undefined)
  const [reconciling, setReconciling] = useState(false)
  // Whether the *saved* profile states its timezone. The form fields alone cannot
  // answer that once the user has started typing in them.
  const [storedGeography, setStoredGeography] = useState(false)
  const [clearingGeography, setClearingGeography] = useState(false)
  // GPUs of phasmid-profile-gen's captured devices, per OS.
  const [gpus, setGpus] = useState<Record<string, GpuChoice[]>>({})
  // The version this form was filled from. Sent with the save so that an edit
  // somebody else made in the meantime is refused instead of being overwritten,
  // and moved on after a refused save so a deliberate second Save can proceed.
  const [baseVersion, setBaseVersion] = useState(0)
  const toast = useToast()

  useEffect(() => {
    if (open) {
      // The dialog is kept mounted and refilled when it opens, so the form has
      // to be reset here. A key would avoid the effect, but it would also throw
      // away the loaded GPU list on every open and would not refill when the
      // same profile is reopened after a save changed it.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setForm(profile ? fromProfile(profile) : EMPTY)
      setBaseVersion(profile?.row_version ?? 0)
      setMachine(profile?.fingerprint)
      setStoredGeography(profile ? hasGeography(profile) : false)
    }
  }, [open, profile])

  // Fetched per OS, and again on the next open while still empty: the list only
  // fills in once the browser is installed, which may happen with the app open.
  const gpusLoaded = form.os in gpus && gpus[form.os].length > 0
  useEffect(() => {
    if (!open || gpusLoaded) return
    const os = form.os
    let cancelled = false
    fingerprintsAPI
      .gpus(os)
      .then((list) => {
        if (!cancelled) setGpus((current) => ({ ...current, [os]: list }))
      })
      .catch(() => {
        if (!cancelled) setGpus((current) => ({ ...current, [os]: [] }))
      })
    return () => {
      cancelled = true
    }
  }, [open, form.os, gpusLoaded])

  const gpuOptions = useMemo(() => {
    const list = gpus[form.os] ?? []
    const named = list.map((choice) => ({ ...choice, name: shortGpu(choice.renderer) }))
    const counts = new Map<string, number>()
    for (const choice of named) counts.set(choice.name, (counts.get(choice.name) ?? 0) + 1)
    return named
      .map((choice) => ({
        value: choice.gpu,
        renderer: choice.renderer,
        // Two captures can share a card name; the id is what tells them apart.
        label: (counts.get(choice.name) ?? 0) > 1 ? `${choice.name} (${choice.gpu})` : choice.name,
      }))
      .sort((left, right) => left.label.localeCompare(right.label))
  }, [gpus, form.os])

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }))

  /**
   * Every key is sent: a null means "not chosen" on create (any captured GPU,
   * follow the proxy) and clears a stored override on edit, which is the same
   * thing — so a blank field reads the same in both modes.
   */
  function buildBrowserSettings() {
    const languages = form.languages
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)

    return {
      os: form.os,
      gpu: form.gpu || null,
      languages: languages.length ? languages : null,
      timezone: form.timezone.trim() || null,
      webrtc_policy: form.webrtcPolicy,
    }
  }

  function buildProxy() {
    if (!form.proxyServer.trim()) return null
    return {
      type: form.proxyType,
      server: form.proxyServer.trim(),
      username: form.proxyUsername.trim() || undefined,
      password: form.proxyPassword.trim() || undefined,
    }
  }

  /**
   * Checks what is on screen rather than what is stored, so the answer belongs to
   * the proxy the user is currently typing — the point is to find out before
   * saving whether it works and whether it agrees with the profile.
   */
  async function handleCheckProxy() {
    setCheckingProxy(true)
    setProxyCheck(null)
    try {
      setProxyCheck(
        await profilesAPI.checkUnsavedProxy({
          proxy_config: buildProxy(),
          browser_settings: buildBrowserSettings(),
        }),
      )
    } catch (error) {
      toast('error', 'Could not check the proxy', (error as Error).message)
    } finally {
      setCheckingProxy(false)
    }
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!form.name.trim()) {
      toast('error', 'Name is required')
      return
    }

    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        name: form.name.trim(),
        group: form.group || null,
        notes: form.notes.trim() || null,
        protected: form.protected,
        browser_settings: buildBrowserSettings(),
        proxy_config: buildProxy(),
      }

      if (isEdit) {
        payload.status = form.status
        payload.row_version = baseVersion
        await profilesAPI.updateProfile(profile.id, payload)
        toast('ok', 'Profile updated', form.name.trim())
      } else {
        // Generated now when the browser is installed; otherwise the first
        // launch generates it from the same settings.
        payload.generate_fingerprint = true
        await profilesAPI.createProfile(payload)
        toast('ok', 'Profile created', form.name.trim())
      }
      onSaved()
      onClose()
    } catch (err) {
      if (isStaleWrite(err) && profile) {
        await handleStaleWrite(profile.id)
        return
      }
      toast('error', isEdit ? 'Could not update profile' : 'Could not create profile', String(err))
    } finally {
      setSaving(false)
    }
  }

  /**
   * Somebody else saved this profile while this form was open.
   *
   * Nothing on screen is replaced: the user's typing is theirs, and a form that
   * rewrites itself under them loses work just as surely as the overwrite this
   * refusal prevented. Instead the stored values are fetched to name what
   * actually changed, and the form is moved onto the current version so that a
   * second Save is a deliberate, informed overwrite rather than a blind one.
   */
  async function handleStaleWrite(profileId: string) {
    try {
      const current = await profilesAPI.getProfile(profileId)
      setBaseVersion(current.row_version)
      setStoredGeography(hasGeography(current))
      setMachine(current.fingerprint)
      const changed = describeChanges(profile, current)
      toast(
        'error',
        'Someone else changed this profile',
        changed
          ? `${changed}. Nothing you typed was lost — press Save again to apply your version.`
          : 'Your edit was not saved. Press Save again to apply your version.',
      )
    } catch {
      // The reload is a courtesy; without it the user still needs to know the
      // save did not land, and that is the part that must never be swallowed.
      toast(
        'error',
        'Someone else changed this profile',
        'Your edit was not saved. Reopen the profile to see the current values.',
      )
    }
  }

  /**
   * Keeping the machine only moves the dropdown; keeping the setting replaces the
   * hardware, so the two are reported very differently even though one call does
   * both. The form's own OS field follows, or it would still show the value the
   * user has just resolved away from.
   */
  async function handleReconcileOs(keepMachine: boolean) {
    if (!profile) return
    setReconciling(true)
    try {
      const updated = await profilesAPI.reconcileOs(profile.id, keepMachine)
      setMachine(updated.fingerprint)
      set('os', updated.browser_settings.os === 'windows' ? 'windows' : 'linux')
      toast(
        'ok',
        keepMachine
          ? `Set back to ${OS_LABELS[updated.browser_settings.os] ?? updated.browser_settings.os}`
          : 'New machine pinned',
        keepMachine
          ? 'The machine is untouched.'
          : `${updated.fingerprint?.screen} · ${updated.fingerprint?.hardware_concurrency} cores. The old hardware is gone.`,
      )
      onSaved()
    } catch (err) {
      toast('error', 'Could not reconcile the operating system', String(err))
    } finally {
      setReconciling(false)
    }
  }

  async function handleClearGeography() {
    if (!profile) return
    setClearingGeography(true)
    try {
      await profilesAPI.clearGeography([profile.id])
      setForm((current) => ({ ...current, timezone: '' }))
      setStoredGeography(false)
      toast('ok', 'Timezone cleared', "The next launch moves the machine to the proxy's exit.")
      onSaved()
    } catch (err) {
      toast('error', 'Could not clear the timezone', String(err))
    } finally {
      setClearingGeography(false)
    }
  }

  async function handleRegenerate() {
    if (!profile) return
    setRegenerating(true)
    try {
      const updated = await profilesAPI.resetFingerprint(profile.id)
      setForm(fromProfile(updated))
      setMachine(updated.fingerprint)
      toast('ok', 'Fingerprint regenerated')
      onSaved()
    } catch (err) {
      toast('error', 'Could not regenerate fingerprint', String(err))
    } finally {
      setRegenerating(false)
    }
  }

  return (
    <Modal
      open={open}
      title={isEdit ? 'Edit profile' : 'New profile'}
      subtitle={
        isEdit
          ? `${profile.id} · created ${new Date(profile.created_at).toLocaleDateString()}`
          : 'Choose a name and an optional proxy. A consistent fingerprint is generated for you.'
      }
      onClose={onClose}
      width={640}
      footer={
        <>
          {isEdit && (
            <button
              type="button"
              className="btn btn-default mr-auto"
              onClick={handleRegenerate}
              disabled={regenerating}
            >
              <RefreshCw size={13} className={regenerating ? 'animate-spin' : ''} />
              Regenerate fingerprint
            </button>
          )}
          <button type="button" className="btn btn-default" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form="profile-form" className="btn btn-primary" disabled={saving}>
            {saving && <LoaderCircle size={13} className="animate-spin" />}
            {isEdit ? 'Save changes' : 'Create profile'}
          </button>
        </>
      }
    >
      <form id="profile-form" onSubmit={handleSubmit} className="flex flex-col gap-5">
        <Section title="Identity">
          <div className="col-span-2">
            <label className="field-label" htmlFor="pf-name">
              Name
            </label>
            <input
              id="pf-name"
              className="field"
              value={form.name}
              onChange={(e) => set('name', e.target.value)}
              placeholder="account-1"
              autoFocus
              required
            />
          </div>

          <div>
            <label className="field-label" htmlFor="pf-group">
              Group
            </label>
            <select
              id="pf-group"
              className="field"
              value={form.group}
              onChange={(e) => set('group', e.target.value)}
            >
              <option value="">No group</option>
              {groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="field-label" htmlFor="pf-os">
              Operating system
            </label>
            <select
              id="pf-os"
              className="field"
              value={form.os}
              onChange={(e) =>
                // GPU ids are captured per OS, so a choice for one is not offered by the other.
                setForm((current) => ({ ...current, os: e.target.value as OsFamily, gpu: '' }))
              }
            >
              <option value="linux">Linux</option>
              <option value="windows">Windows</option>
            </select>
            {/* Once a machine is pinned this setting is not what a page sees, so
                the honest warning is stronger than "the rest stays as it was". */}
            {isEdit && form.os !== (profile.browser_settings?.os ?? 'linux') && (
              <p className="mt-1.5 text-ink-faint">
                {machine?.pinned_os
                  ? `Saving this changes nothing a page can see: the pinned machine reports ${
                      OS_LABELS[machine.pinned_os] ?? machine.pinned_os
                    } and keeps doing so. The Machine panel then offers both ways out.`
                  : 'The machine is generated for this OS on the first launch.'}
              </p>
            )}
          </div>

          {isEdit && (
            <div>
              <label className="field-label" htmlFor="pf-status">
                Status
              </label>
              <select
                id="pf-status"
                className="field"
                value={form.status}
                onChange={(e) => set('status', e.target.value)}
              >
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
                <option value="blocked">Blocked</option>
                <option value="maintenance">Maintenance</option>
              </select>
            </div>
          )}

          <div className="col-span-2">
            <label className="field-label" htmlFor="pf-notes">
              Notes
            </label>
            <textarea
              id="pf-notes"
              className="field resize-y"
              rows={2}
              value={form.notes}
              onChange={(e) => set('notes', e.target.value)}
            />
          </div>

          <label className="col-span-2 flex cursor-pointer items-start gap-2.5" htmlFor="pf-protected">
            <input
              id="pf-protected"
              type="checkbox"
              className="mt-0.5"
              checked={form.protected}
              onChange={(e) => set('protected', e.target.checked)}
            />
            <span>
              Protected
              <span className="block text-ink-faint">
                Refuse moving it to Trash or giving it a new machine — by you, a script or an
                agent — until it is unprotected. Editing and running are unaffected.
              </span>
            </span>
          </label>
        </Section>

        <Section
          title="Proxy"
          hint="Leave the server empty for a direct connection."
        >
          <div>
            <label className="field-label" htmlFor="pf-proxy-type">
              Type
            </label>
            <select
              id="pf-proxy-type"
              className="field"
              value={form.proxyType}
              onChange={(e) => set('proxyType', e.target.value)}
            >
              <option value="http">HTTP</option>
              <option value="https">HTTPS</option>
              <option value="socks4">SOCKS4</option>
              <option value="socks5">SOCKS5</option>
            </select>
          </div>

          <div>
            <label className="field-label" htmlFor="pf-proxy-server">
              Server
            </label>
            <input
              id="pf-proxy-server"
              className="field font-mono"
              value={form.proxyServer}
              onChange={(e) => set('proxyServer', e.target.value)}
              placeholder="host:port"
            />
          </div>

          <div>
            <label className="field-label" htmlFor="pf-proxy-user">
              Username
            </label>
            <input
              id="pf-proxy-user"
              className="field"
              value={form.proxyUsername}
              onChange={(e) => set('proxyUsername', e.target.value)}
              autoComplete="off"
            />
          </div>

          <div>
            <label className="field-label" htmlFor="pf-proxy-pass">
              Password
            </label>
            <input
              id="pf-proxy-pass"
              type="password"
              className="field"
              value={form.proxyPassword}
              onChange={(e) => set('proxyPassword', e.target.value)}
              autoComplete="new-password"
            />
          </div>

          {/* SOCKS4 has no password authentication at all, so the launch would
              be refused rather than go out unauthenticated — say so first. */}
          {form.proxyType === 'socks4' &&
            (form.proxyUsername.trim() || form.proxyPassword.trim()) && (
              <p className="col-span-2 text-danger">
                SOCKS4 cannot carry a username or password, so this profile will fail to launch.
                Use SOCKS5, HTTP or HTTPS for credentials.
              </p>
            )}

          <div className="col-span-2">
            <button
              type="button"
              className="btn"
              onClick={handleCheckProxy}
              disabled={checkingProxy}
            >
              {checkingProxy ? (
                <LoaderCircle size={13} className="animate-spin" />
              ) : (
                <RefreshCw size={13} />
              )}
              {checkingProxy ? 'Checking…' : 'Check proxy'}
            </button>
            <ProxyCheckResult result={proxyCheck} />
          </div>
        </Section>

        <details className="rounded-md border border-line p-3" open={isEdit || undefined}>
          <summary className="cursor-pointer font-medium">Advanced browser and fingerprint settings</summary>
          <p className="mb-4 mt-1 text-ink-dim">Defaults are ready to use. Change these only when a profile needs a specific setup.</p>
          <div className="flex flex-col gap-5">
        {isEdit && (
          <PinnedMachine
            profileId={profile.id}
            fingerprint={machine}
            onReconcileOs={handleReconcileOs}
            reconciling={reconciling}
          />
        )}

        <Section
          title="Fingerprint"
          hint={
            isEdit && machine
              ? 'OS and GPU take effect only when the machine is regenerated: save, then Regenerate fingerprint. Languages, timezone and WebRTC apply at the next launch.'
              : 'Phasmid generates one consistent machine from these — screen, GPU, cores, fonts — and the profile keeps it for good.'
          }
        >
          <div className="col-span-2">
            <label className="field-label" htmlFor="pf-gpu">
              GPU
            </label>
            <select
              id="pf-gpu"
              className="field"
              value={form.gpu}
              onChange={(e) => set('gpu', e.target.value)}
            >
              <option value="">Any captured GPU</option>
              {gpuOptions.map((option) => (
                <option key={option.value} value={option.value} title={option.renderer}>
                  {option.label}
                </option>
              ))}
              {/* A stored choice the list does not offer (browser not installed
                  yet, or a capture since dropped) still has to read as itself,
                  or saving would silently clear it. */}
              {form.gpu && !gpuOptions.some((option) => option.value === form.gpu) && (
                <option value={form.gpu}>{form.gpu}</option>
              )}
            </select>
            <p className="mt-1.5 text-ink-faint">
              {gpuOptions.length
                ? `${gpuOptions.length} ${OS_LABELS[form.os] ?? form.os} GPUs backed by a captured machine. Any draws one, weighted by how common it is.`
                : 'The list fills in once the browser is installed. Any draws one from the captured machines.'}
            </p>
            {isEdit && machine?.gpu_id && form.gpu && form.gpu !== machine.gpu_id && (
              <p className="mt-1.5 text-ink-faint">
                The pinned machine keeps its current GPU until it is regenerated.
              </p>
            )}
          </div>

          <div>
            <label className="field-label" htmlFor="pf-langs">
              Languages
            </label>
            <input
              id="pf-langs"
              className="field font-mono"
              value={form.languages}
              onChange={(e) => set('languages', e.target.value)}
              placeholder="From the proxy"
              aria-describedby="pf-langs-hint"
            />
            <p id="pf-langs-hint" className="mt-1.5 text-ink-faint">
              Comma-separated, e.g. en-US, en. Blank follows the proxy.
            </p>
          </div>

          <div>
            <label className="field-label" htmlFor="pf-tz">
              Timezone
            </label>
            <input
              id="pf-tz"
              className="field font-mono"
              value={form.timezone}
              onChange={(e) => set('timezone', e.target.value)}
              placeholder="From the proxy"
              aria-describedby="pf-tz-hint"
            />
            <p id="pf-tz-hint" className="mt-1.5 text-ink-faint">
              An IANA zone, e.g. Europe/Berlin. Blank follows the proxy.
            </p>
          </div>

          <div className="col-span-2">
            <label className="field-label" htmlFor="pf-webrtc">
              WebRTC
            </label>
            <select
              id="pf-webrtc"
              className="field"
              value={form.webrtcPolicy}
              onChange={(e) => set('webrtcPolicy', e.target.value as WebRTCPolicy)}
            >
              {WEBRTC_POLICIES.map((policy) => (
                <option key={policy.value} value={policy.value}>
                  {policy.label}
                </option>
              ))}
            </select>
            <p className="mt-1.5 text-ink-faint">
              {WEBRTC_POLICIES.find((policy) => policy.value === form.webrtcPolicy)?.hint}
            </p>
          </div>

          {/* A stated timezone wins over the proxy at every launch, so a profile
              moved to a proxy elsewhere keeps claiming the old zone. Offered
              whenever one is stored, since nothing records why it was set. */}
          {isEdit && storedGeography && (
            <div className="col-span-2 rounded-md border border-line bg-raised p-2.5">
              <p className="text-ink">
                This profile states its timezone instead of taking it from its proxy.
              </p>
              <p className="mt-0.5 text-ink-faint">
                Cleared, the next launch moves the machine to the proxy&apos;s exit address — its
                zone, position and WebRTC address. Languages and the hardware stay.
              </p>
              <div className="mt-2 flex justify-end">
                <button
                  type="button"
                  className="btn btn-default"
                  onClick={handleClearGeography}
                  disabled={clearingGeography}
                >
                  {clearingGeography && <LoaderCircle size={13} className="animate-spin" />}
                  Clear timezone, follow the proxy
                </button>
              </div>
            </div>
          )}
        </Section>
          </div>
        </details>
      </form>
    </Modal>
  )
}

/**
 * The machine a profile is pinned to. Without this the profile would look like
 * different hardware every session, so it is worth showing that it does not.
 */
function PinnedMachine({
  profileId,
  fingerprint,
  onReconcileOs,
  reconciling,
}: {
  profileId: string
  fingerprint?: FingerprintSummary | null
  onReconcileOs: (keepMachine: boolean) => void
  reconciling: boolean
}) {
  const [json, setJson] = useState<string | null>(null)
  const [loadingJson, setLoadingJson] = useState(false)
  const toast = useToast()

  async function viewJson() {
    setLoadingJson(true)
    try {
      setJson(JSON.stringify(await profilesAPI.getMachine(profileId), null, 2))
    } catch (err) {
      toast('error', 'Could not load the machine', String(err))
    } finally {
      setLoadingJson(false)
    }
  }

  async function copyJson() {
    if (!json) return
    try {
      await navigator.clipboard.writeText(json)
      toast('ok', 'Machine JSON copied')
    } catch {
      toast('error', 'Could not copy', 'Select the text and copy it instead.')
    }
  }

  if (!fingerprint) {
    return (
      <fieldset>
        <legend className="mb-0.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-faint">
          Machine
        </legend>
        <p className="text-ink-faint">
          Generated on the first launch, then reused every time so this profile stays the same
          computer.
        </p>
      </fieldset>
    )
  }

  const screen = fingerprint.screen
    ? fingerprint.device_pixel_ratio
      ? `${fingerprint.screen} @ ${fingerprint.device_pixel_ratio}x`
      : fingerprint.screen
    : null

  const rows: [string, string | number | null | undefined][] = [
    [
      'OS',
      fingerprint.os
        ? [osLabel(fingerprint.os), fingerprint.os_version].filter(Boolean).join(' ')
        : null,
    ],
    ['Browser', fingerprint.browser_version ? `Phasmid ${fingerprint.browser_version}` : null],
    ['Screen', screen],
    ['CPU threads', fingerprint.hardware_concurrency],
    ['Memory', fingerprint.device_memory != null ? `${fingerprint.device_memory} GB` : null],
    ['GPU', fingerprint.gpu],
    ['Fonts', fingerprint.font_count],
    ['Timezone', fingerprint.timezone],
    ['Languages', fingerprint.languages?.length ? fingerprint.languages.join(', ') : null],
    ['Exit / WebRTC', fingerprint.public_ip],
  ]

  return (
    <fieldset>
      <legend className="mb-0.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-faint">
        Machine
      </legend>
      <div className="mb-2.5 flex items-start justify-between gap-3">
        <p className="text-ink-faint">
          Pinned across launches. Each launch moves it to the proxy&apos;s exit address; the
          hardware stays. Regenerate the fingerprint to move this profile to different hardware.
        </p>
        <button
          type="button"
          className="btn btn-default shrink-0"
          onClick={viewJson}
          disabled={loadingJson}
        >
          {loadingJson ? (
            <LoaderCircle size={13} className="animate-spin" />
          ) : (
            <Braces size={13} aria-hidden="true" />
          )}
          View machine JSON
        </button>
      </div>
      <div className="panel divide-y divide-line">
        {rows
          .filter(([, value]) => value !== null && value !== undefined && value !== '')
          .map(([label, value]) => (
            <div key={label} className="flex gap-3 px-3 py-1.5">
              <span className="w-[92px] shrink-0 text-ink-dim">{label}</span>
              <span className="min-w-0 flex-1 break-all font-mono text-ink">{value}</span>
            </div>
          ))}
      </div>

      {/* The OS dropdown can be changed long after the machine was pinned, and
          nothing stops it — the pin is what a page sees, so the setting simply
          stops meaning anything. The two ways out cost very different amounts,
          so neither is preselected. */}
      {fingerprint.os_mismatch && (
        <div className="mt-2 rounded-md border border-line bg-raised p-2.5">
          <div className="flex items-start gap-2.5">
            <TriangleAlert size={14} className="mt-0.5 shrink-0 text-warn" />
            <div>
              <p className="text-ink">
                This profile is set to {osLabel(fingerprint.settings_os)}, but its pinned machine is
                a {osLabel(fingerprint.pinned_os)} one — and the machine is what every page sees.
              </p>
              <p className="mt-0.5 text-ink-faint">
                Keeping the machine puts the setting back to {osLabel(fingerprint.pinned_os)} and
                changes no fingerprint at all. Pinning a{' '}
                {osLabel(fingerprint.settings_os)} machine instead gives this profile different
                hardware — screen, GPU, cores, fonts and canvas — which any account already warmed
                up on the old one will notice.
              </p>
            </div>
          </div>
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              className="btn btn-default"
              onClick={() => onReconcileOs(true)}
              disabled={reconciling}
            >
              {reconciling && <LoaderCircle size={13} className="animate-spin" />}
              Keep this machine
            </button>
            <button
              type="button"
              className="btn btn-default"
              onClick={() => onReconcileOs(false)}
              disabled={reconciling}
            >
              New {osLabel(fingerprint.settings_os)} machine
            </button>
          </div>
        </div>
      )}

      {/* Portalled out of the profile dialog: inside it, the dialog's form and
          scroll container would hold this one too. */}
      {json !== null &&
        createPortal(
          <Modal
            open
            title="Machine JSON"
            subtitle="The Phasmid profile this profile launches with, apart from the location each launch moves it to."
            onClose={() => setJson(null)}
            width={760}
            footer={
              <>
                <button type="button" className="btn btn-default mr-auto" onClick={copyJson}>
                  <Copy size={13} aria-hidden="true" />
                  Copy
                </button>
                <button type="button" className="btn btn-default" onClick={() => setJson(null)}>
                  Close
                </button>
              </>
            }
          >
            <pre className="whitespace-pre-wrap break-all font-mono text-[11.5px] leading-relaxed text-ink">
              {json}
            </pre>
          </Modal>,
          document.body,
        )}
    </fieldset>
  )
}

function osLabel(os?: string | null): string {
  return (os && OS_LABELS[os]) || os || 'unknown'
}

function Section({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <fieldset>
      <legend className="mb-0.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-faint">
        {title}
      </legend>
      {hint && <p className="mb-2.5 text-ink-faint">{hint}</p>}
      <div className="grid grid-cols-2 gap-3">{children}</div>
    </fieldset>
  )
}


/**
 * What the check found: where the proxy actually comes out, and everything a page
 * could notice between that and the profile. A working proxy is not the whole
 * answer — a profile whose timezone contradicts its exit address is detectable
 * however healthy the connection is.
 *
 * Three levels, three treatments: red for something that stops the browser
 * launching, amber for something a page can detect, grey for a note.
 */
function ProxyCheckResult({ result }: { result: ProxyCheck | null }) {
  if (!result) return null

  if (!result.reachable) {
    return (
      <p className="mt-2 flex items-start gap-1.5 text-danger">
        <X size={13} className="mt-0.5 shrink-0" />
        <span>{result.error}</span>
      </p>
    )
  }

  const where = result.location
  const place = [where?.country, where?.timezone].filter(Boolean).join(' · ')

  return (
    <div className="mt-2 space-y-1.5">
      <p className="flex items-start gap-1.5 text-ink-muted">
        <Check size={13} className="mt-0.5 shrink-0 text-ok" />
        <span>
          Exits at <span className="font-mono">{where?.ip}</span>
          {place && <> — {place}</>}
          {result.latency_ms !== null && <> · {result.latency_ms} ms</>}
        </span>
      </p>
      {result.findings.map((finding, index) => (
        <p
          key={index}
          className={`flex items-start gap-1.5 ${
            finding.level === 'error'
              ? 'text-danger'
              : finding.level === 'warning'
                ? 'text-warn'
                : 'text-ink-faint'
          }`}
        >
          {finding.level === 'info' ? (
            <Info size={13} className="mt-0.5 shrink-0" />
          ) : (
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          )}
          <span>{finding.message}</span>
        </p>
      ))}
    </div>
  )
}
