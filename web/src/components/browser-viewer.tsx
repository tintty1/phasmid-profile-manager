'use client'

import { useEffect, useRef, useState } from 'react'
import { Maximize2, Minimize2, Square, X } from 'lucide-react'

import { getApiKey, vncUrl } from '@/lib/api'

type Status = 'connecting' | 'connected' | 'disconnected'

interface BrowserViewerProps {
  profileId: string
  profileName: string
  onClose: () => void
  onStop: () => void
}

/** The API key as a WebSocket subprotocol; see KEY_PROTOCOL_PREFIX in api/routes/viewer.py. */
function keyProtocol(key: string): string {
  const bytes = new TextEncoder().encode(key)
  const base64 = btoa(String.fromCharCode(...bytes))
  return `cpm-key.${base64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')}`
}

/**
 * A running profile's browser, shown and driven over VNC.
 *
 * Deliberately not a Modal: Modal closes on Escape, and Escape here belongs to
 * the page inside the browser.
 */
export function BrowserViewer({ profileId, profileName, onClose, onStop }: BrowserViewerProps) {
  const screenRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<Status>('connecting')
  const [reason, setReason] = useState<string | null>(null)
  const [fullscreen, setFullscreen] = useState(false)

  useEffect(() => {
    let cancelled = false
    // Set once noVNC has disconnected by itself; disconnecting it again logs an error.
    let ended = false
    let rfb: import('@novnc/novnc').default | null = null

    async function connect() {
      // Loaded on demand: noVNC touches browser-only APIs at import time.
      const { default: RFB } = await import('@novnc/novnc')
      if (cancelled || !screenRef.current) return

      const key = getApiKey()
      rfb = new RFB(screenRef.current, vncUrl(profileId), {
        wsProtocols: key ? ['binary', keyProtocol(key)] : ['binary'],
      })
      rfb.scaleViewport = true
      rfb.resizeSession = false
      rfb.background = '#000'
      rfb.addEventListener('connect', () => {
        if (!cancelled) setStatus('connected')
      })
      rfb.addEventListener('disconnect', (event) => {
        ended = true
        if (cancelled) return
        setStatus('disconnected')
        setReason(event.detail.clean ? 'The browser was closed.' : 'Lost the connection to the browser.')
      })
      rfb.focus()
    }

    connect().catch((err: unknown) => {
      if (cancelled) return
      setStatus('disconnected')
      setReason(err instanceof Error ? err.message : String(err))
    })

    return () => {
      cancelled = true
      try {
        if (!ended) rfb?.disconnect()
      } catch {
        // Already gone; nothing to clean up.
      }
    }
  }, [profileId])

  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === panelRef.current)
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  function toggleFullscreen() {
    if (document.fullscreenElement) void document.exitFullscreen()
    else void panelRef.current?.requestFullscreen()
  }

  return (
    <div
      ref={panelRef}
      className="fixed inset-0 z-50 flex flex-col bg-canvas"
      role="dialog"
      aria-modal="true"
      aria-label={`Browser for ${profileName}`}
    >
      <div className="flex h-11 shrink-0 items-center gap-3 border-b border-line bg-surface px-4">
        <span className="font-medium">{profileName}</span>
        <span className="font-mono text-ink-faint">{profileId}</span>
        <StatusDot status={status} />
        <div className="ml-auto flex items-center gap-1">
          <button
            className="btn btn-ghost h-7 w-7 p-0"
            onClick={toggleFullscreen}
            aria-label={fullscreen ? 'Exit full screen' : 'Full screen'}
            title={fullscreen ? 'Exit full screen' : 'Full screen'}
          >
            {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
          <button className="btn btn-default h-7" onClick={onStop}>
            <Square size={11} fill="currentColor" />
            Stop browser
          </button>
          <button
            className="btn btn-ghost h-7 w-7 p-0"
            onClick={onClose}
            aria-label="Close viewer"
            title="Close viewer (the browser keeps running)"
          >
            <X size={15} />
          </button>
        </div>
      </div>

      <div className="relative min-h-0 flex-1 bg-black">
        <div ref={screenRef} className="h-full w-full" />
        {status !== 'connected' && (
          <div className="absolute inset-0 flex items-center justify-center text-ink-dim">
            {status === 'connecting' ? 'Connecting…' : (reason ?? 'Disconnected.')}
          </div>
        )}
      </div>
    </div>
  )
}

function StatusDot({ status }: { status: Status }) {
  const color = status === 'connected' ? 'bg-ok' : status === 'connecting' ? 'bg-warn' : 'bg-danger'
  const label = status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting' : 'Disconnected'
  return (
    <span className="flex items-center gap-1.5 text-ink-dim">
      <span className={`h-1.5 w-1.5 rounded-full ${color}`} aria-hidden />
      {label}
    </span>
  )
}
