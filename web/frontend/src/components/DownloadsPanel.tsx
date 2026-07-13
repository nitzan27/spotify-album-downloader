import { useEffect, useState } from 'react'
import { getDownloadUrl, getJobStatus } from '../api'
import type { JobStatusResponse } from '../api'
import { saveZipToFolder } from '../downloadFolder'
import { JobStatusView } from './JobStatusView'

export interface TrackedJob {
  id: string
  label: string
  artist: string
  album: string
}

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

function TrackedJobRow({
  job,
  downloadDirHandle,
  onJobDone,
  onJobFailed,
}: {
  job: TrackedJob
  downloadDirHandle: FileSystemDirectoryHandle | null
  onJobDone: (artist: string, album: string) => void
  onJobFailed: (artist: string, album: string) => void
}) {
  const [status, setStatus] = useState<JobStatusResponse | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)

  // Each row polls itself, keyed only on job.id - appending a new job to the
  // parent's list never disturbs already-running pollers for earlier jobs.
  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const poll = async () => {
      try {
        const result = await getJobStatus(job.id)
        if (cancelled) return
        setStatus(result)
        if (result.status === 'queued' || result.status === 'running') {
          timer = window.setTimeout(poll, 2000)
        } else if (result.status === 'done') {
          onJobDone(job.artist, job.album)
        } else if (result.status === 'error') {
          onJobFailed(job.artist, job.album)
        }
      } catch (err) {
        if (!cancelled) {
          setStatus({ status: 'error', progress: '', error: (err as Error).message })
          onJobFailed(job.artist, job.album)
        }
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

  return (
    <div>
      <div className="muted">{job.label}</div>
      {status ? (
        <JobStatusView status={status.status} progress={status.progress} error={status.error}>
          {status.status === 'done' && !downloadDirHandle && (
            <>
              {' '}
              <a className="download" href={getDownloadUrl(job.id)}>
                Download zip
              </a>
            </>
          )}
          {status.status === 'done' && downloadDirHandle && (
            <>
              {' '}
              {saveState === 'saving' && <span className="muted">Saving to folder...</span>}
              {saveState === 'saved' && <span className="muted">Saved to folder.</span>}
              {saveState === 'error' && (
                <span className="status error" style={{ display: 'inline' }}>
                  {saveError} -{' '}
                </span>
              )}
              <button type="button" className="secondary" onClick={saveToFolder}>
                {saveState === 'error' ? 'Retry save to folder' : 'Save to folder'}
              </button>
            </>
          )}
        </JobStatusView>
      ) : (
        <div className="status progress">
          <div className="spinner" />
          <div className="status-text">Queuing...</div>
        </div>
      )}
    </div>
  )
}

export function DownloadsPanel({
  jobs,
  downloadDirHandle,
  onJobDone,
  onJobFailed,
}: {
  jobs: TrackedJob[]
  downloadDirHandle: FileSystemDirectoryHandle | null
  onJobDone: (artist: string, album: string) => void
  onJobFailed: (artist: string, album: string) => void
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
          />
        ))}
      </div>
    </div>
  )
}
