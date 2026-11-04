'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Download, FileDown, LoaderCircle, RefreshCw, Trash2 } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { Modal } from '@/components/modal'
import { useToast } from '@/components/toast'
import { formatBytes, profilesAPI, type DownloadFile, type DownloadList } from '@/lib/api'

interface Props {
  profileId: string
  profileName: string
  /** Whether the profile's browser runs: then files can still be arriving. */
  running: boolean
  onClose: () => void
}

// Often enough to watch a download finish, rarely enough to be free.
const POLL_MS = 3000

/**
 * The files a profile's browser downloaded, in its own folder on the server.
 *
 * Files are fetched through the API (with the same credentials as every other
 * call), so this works when the browser runs in Docker or on another machine.
 */
export function DownloadsDialog({ profileId, profileName, running, onClose }: Props) {
  const toast = useToast()
  const [list, setList] = useState<DownloadList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  // Deleting takes two clicks on the same row: the first arms it.
  const [armed, setArmed] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const mounted = useRef(true)

  const load = useCallback(async () => {
    try {
      const next = await profilesAPI.listDownloads(profileId)
      if (!mounted.current) return
      setList(next)
      setError(null)
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : String(err))
    }
  }, [profileId])

  useEffect(() => {
    mounted.current = true
    // The request completes asynchronously; this effect starts the initial read.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => {
      mounted.current = false
    }
  }, [load])

  // Keep watching while something can still change: a running browser may
  // start a download, and a partial file finishes on its own.
  const busy = running || (list?.files.some((file) => !file.complete) ?? false)
  useEffect(() => {
    if (!busy) return
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [busy, load])

  async function save(file: DownloadFile) {
    setSaving(file.name)
    try {
      const blob = await profilesAPI.fetchDownload(profileId, file.name)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = file.name
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      toast('error', `Could not save ${file.name}`, err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(null)
    }
  }

  async function remove(file: DownloadFile) {
    setDeleting(file.name)
    try {
      await profilesAPI.deleteDownload(profileId, file.name)
      toast('ok', 'Download deleted', file.name)
      setArmed(null)
      await load()
    } catch (err) {
      toast('error', `Could not delete ${file.name}`, err instanceof Error ? err.message : String(err))
    } finally {
      setDeleting(null)
    }
  }

  const files = list?.files ?? []
  const total = files.reduce((sum, file) => sum + file.size, 0)

  return (
    <Modal
      open
      title={`Downloads — ${profileName}`}
      subtitle={
        list
          ? `${files.length} ${files.length === 1 ? 'file' : 'files'}${files.length ? ` · ${formatBytes(total)}` : ''}`
          : undefined
      }
      onClose={onClose}
      width={680}
      footer={
        <>
          <button className="btn btn-default mr-auto" onClick={() => void load()} aria-label="Refresh downloads">
            <RefreshCw size={13} aria-hidden="true" />
            Refresh
          </button>
          <button className="btn btn-default" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      {error ? (
        <EmptyState
          icon={<FileDown size={18} />}
          title="Cannot load the downloads"
          body={error}
          action={
            <button className="btn btn-default" onClick={() => void load()}>
              Retry
            </button>
          }
        />
      ) : !list ? (
        <p className="py-8 text-center text-ink-dim" role="status">
          Loading…
        </p>
      ) : files.length === 0 ? (
        <EmptyState
          icon={<FileDown size={18} />}
          title="Nothing downloaded yet"
          body="Files this profile's browser downloads appear here, whether a person or an agent started them."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full min-w-[520px] text-left">
            <thead className="bg-surface text-ink-dim">
              <tr>
                <th className="px-4 py-2.5 font-medium">File</th>
                <th className="px-4 py-2.5 text-right font-medium">Size</th>
                <th className="px-4 py-2.5 font-medium">Downloaded</th>
                <th className="px-4 py-2.5 text-right font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {files.map((file) => (
                <tr key={file.name}>
                  <td className="max-w-[260px] px-4 py-2.5">
                    <p className="break-all font-medium">{file.name}</p>
                    {!file.complete && (
                      <p className="flex items-center gap-1 text-[11px] text-ink-dim" role="status">
                        <LoaderCircle
                          size={11}
                          aria-hidden="true"
                          className="animate-spin motion-reduce:animate-none"
                        />
                        Still downloading
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-ink-dim">
                    {formatBytes(file.size)}
                  </td>
                  <td className="px-4 py-2.5 text-ink-dim">
                    {new Date(file.modified).toLocaleString()}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex justify-end gap-2">
                      {armed === file.name ? (
                        <>
                          <button
                            className="btn btn-default"
                            onClick={() => setArmed(null)}
                            disabled={deleting === file.name}
                          >
                            Cancel
                          </button>
                          <button
                            className="btn btn-danger"
                            onClick={() => void remove(file)}
                            disabled={deleting === file.name}
                            aria-label={`Confirm deleting ${file.name}`}
                          >
                            {deleting === file.name ? 'Deleting…' : 'Delete'}
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            className="btn btn-default"
                            onClick={() => void save(file)}
                            disabled={!file.complete || saving === file.name}
                            aria-label={`Save ${file.name}`}
                          >
                            {saving === file.name ? (
                              <LoaderCircle
                                size={13}
                                aria-hidden="true"
                                className="animate-spin motion-reduce:animate-none"
                              />
                            ) : (
                              <Download size={13} aria-hidden="true" />
                            )}
                            Save
                          </button>
                          <button
                            className="btn btn-default"
                            onClick={() => setArmed(file.name)}
                            disabled={!file.complete}
                            aria-label={`Delete ${file.name}`}
                            title="Delete from the server"
                          >
                            <Trash2 size={13} aria-hidden="true" />
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {list && (
        <p className="mt-3 break-all text-[11px] text-ink-faint">
          Stored on the server in <span className="font-mono">{list.downloads_dir}</span>. Not included
          in exports, backups or copies of this profile.
        </p>
      )}
    </Modal>
  )
}
