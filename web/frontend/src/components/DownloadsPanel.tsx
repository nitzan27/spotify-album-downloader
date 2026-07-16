import { useEffect, useState } from 'react'
import { getDownloadUrl, getJobStatus, isNetworkError } from '../api'
import type { JobStatusResponse } from '../api'
import { saveZipToFolder } from '../downloadFolder'

export interface TrackedJob {
  id: string
  label: string
  artist: string
  album: string
  createdAt: number
}

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

const POLL_INTERVAL_MS = 2000
// A DNS/network blip (e.g. the machine's connection dropping mid-batch) or a
// dev server restart mid-poll used to surface as an instant, permanent
// "Error: Failed to fetch" on the job row even though the job itself was
// still fine server-side. Retry a few times with backoff before giving up -
// only for a raw fetch()-level failure (isNetworkError), never for a real
// HTTP error response like a 404 (job genuinely gone), which fails as before.
const MAX_POLL_RETRIES = 5
const MAX_POLL_RETRY_DELAY_MS = 15000

function TrackedJobRow({
  job,
  downloadDirHandle,
  onJobDone,
  onJobFailed,
  onRetry,
}: {
  job: TrackedJob
  downloadDirHandle: FileSystemDirectoryHandle | null
  onJobDone: (artist: string, album: string) => void
  onJobFailed: (artist: string, album: string) => void
  onRetry: (job: TrackedJob) => void
}) {
  const [status, setStatus] = useState<JobStatusResponse | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)

  // Each row polls itself, keyed only on job.id - appending a new job to the
  // parent's list never disturbs already-running pollers for earlier jobs.
  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const poll = async (retriesLeft = MAX_POLL_RETRIES) => {
      try {
        const result = await getJobStatus(job.id)
        if (cancelled) return
        setStatus(result)
        // A rate_limited download auto-requeues itself server-side (see
        // web/jobs.py's MAX_AUTO_RETRIES) and, once exhausted, self-resolves
        // to "error" rather than needing a manual click like a rate_limited
        // scan does - so it's treated the same as queued/running here: just
        // keep polling and showing whatever progress text the backend sends.
        if (result.status === 'queued' || result.status === 'running' || result.status === 'rate_limited') {
          timer = window.setTimeout(() => poll(MAX_POLL_RETRIES), POLL_INTERVAL_MS)
        } else if (result.status === 'done') {
          onJobDone(job.artist, job.album)
        } else if (result.status === 'error') {
          onJobFailed(job.artist, job.album)
        }
      } catch (err) {
        if (cancelled) return
        if (isNetworkError(err) && retriesLeft > 0) {
          const attempt = MAX_POLL_RETRIES - retriesLeft
          const delay = Math.min(POLL_INTERVAL_MS * 2 ** attempt, MAX_POLL_RETRY_DELAY_MS)
          timer = window.setTimeout(() => poll(retriesLeft - 1), delay)
          return
        }
        setStatus({ status: 'error', progress: '', error: (err as Error).message })
        onJobFailed(job.artist, job.album)
      }
    }
    poll()

    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id])

  const saveToFolder = async () => {
    if (!downloadDirHandle) return
    setSaveState('saving')
    setSaveError(null)
    try {
      const res = await fetch(getDownloadUrl(job.id))
      if (!res.ok) throw new Error(`Download failed with status ${res.status}`)
      const zipBytes = await res.arrayBuffer()
      await saveZipToFolder(downloadDirHandle, job.artist, job.album, zipBytes)
      setSaveState('saved')
    } catch (err) {
      setSaveState('error')
      setSaveError((err as Error).message)
    }
  }

  // Once a folder is chosen, save automatically as soon as the download
  // finishes - no extra click needed. If the folder is picked *after* the
  // job already finished, this still fires (dependency on downloadDirHandle).
  useEffect(() => {
    if (status?.status === 'done' && downloadDirHandle && saveState === 'idle') {
      saveToFolder()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.status, downloadDirHandle])

  const isInFlight =
    !status || status.status === 'queued' || status.status === 'running' || status.status === 'rate_limited'
  // Treat the brief 'idle' render (before the auto-save effect above has had
  // a chance to fire) the same as 'saving' so the row never flashes empty.
  const isSaving = saveState === 'saving' || saveState === 'idle'

  return (
    <div className="job-row">
      <span className="job-label">{job.label}</span>
      {isInFlight && (
        <span className="job-status-text muted">
          {status?.progress || 'Queuing...'}
          <span className="spinner" />
        </span>
      )}
      {status?.status === 'error' && (
        <>
          <span className="job-status-error" title={status.error ?? undefined}>
            Error: {status.error}
          </span>
          <button type="button" className="secondary" onClick={() => onRetry(job)}>
            Retry
          </button>
        </>
      )}
      {status?.status === 'done' && !downloadDirHandle && (
        <a className="download" href={getDownloadUrl(job.id)}>
          Download zip
        </a>
      )}
      {status?.status === 'done' && downloadDirHandle && (
        <>
          {isSaving && (
            <span className="job-status-text muted">
              Saving...
              <span className="spinner" />
            </span>
          )}
          {saveState === 'saved' && (
            <span className="result-check" role="img" aria-label="Saved to folder">
              <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                <circle cx="8" cy="8" r="8" fill="#1DB954" />
                <path
                  d="M4.5 8.3L6.8 10.6L11.5 5.6"
                  stroke="#000000"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>
          )}
          {saveState === 'error' && (
            <>
              <span className="job-status-error" title={saveError ?? undefined}>
                {saveError}
              </span>
              <button type="button" className="secondary" onClick={saveToFolder}>
                Retry
              </button>
            </>
          )}
        </>
      )}
    </div>
  )
}

export function DownloadsPanel({
  jobs,
  downloadDirHandle,
  onJobDone,
  onJobFailed,
  onRetry,
}: {
  jobs: TrackedJob[]
  downloadDirHandle: FileSystemDirectoryHandle | null
  onJobDone: (artist: string, album: string) => void
  onJobFailed: (artist: string, album: string) => void
  onRetry: (job: TrackedJob) => void
}) {
  if (jobs.length === 0) return null
  return (
    <div className="card">
      <h2>Downloads</h2>
      <div className="job-list">
        {jobs.map((job) => (
          <TrackedJobRow
            key={job.id}
            job={job}
            downloadDirHandle={downloadDirHandle}
            onJobDone={onJobDone}
            onJobFailed={onJobFailed}
            onRetry={onRetry}
          />
        ))}
      </div>
    </div>
  )
}
