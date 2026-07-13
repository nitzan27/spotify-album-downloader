import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  getJobStatus,
  resumeScan,
  startScan,
  submitJobsBatch,
} from '../api'
import type { JobStatusResponse, MissingAlbumResult } from '../api'
import { albumFolderName } from '../downloadFolder'
import { JobStatusView } from './JobStatusView'
import type { TrackedJob } from './DownloadsPanel'

interface Props {
  onJobsCreated: (jobs: TrackedJob[]) => void
  existingAlbumFolders: Set<string> | null
  downloadedAlbums: Set<string>
}

export function ScanPanel({ onJobsCreated, existingAlbumFolders, downloadedAlbums }: Props) {
  const [market, setMarket] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)
  const [status, setStatus] = useState<JobStatusResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [queuing, setQueuing] = useState(false)

  const wasJustDownloaded = (result: MissingAlbumResult) =>
    downloadedAlbums.has(albumFolderName(result.artist, result.album))

  const isAlreadyDownloaded = (result: MissingAlbumResult) =>
    (existingAlbumFolders?.has(albumFolderName(result.artist, result.album)) ?? false) ||
    wasJustDownloaded(result)

  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    let timer: number | undefined

    const poll = async () => {
      try {
        const result = await getJobStatus(jobId)
        if (cancelled) return
        setStatus(result)
        const stillInFlight =
          result.status === 'queued' ||
          result.status === 'running' ||
          (result.status === 'rate_limited' && result.will_auto_retry)
        if (stillInFlight) {
          timer = window.setTimeout(poll, 2000)
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message)
      }
    }
    poll()

    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [jobId])

  const handleStart = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setStatus(null)
    setSelected(new Set())
    try {
      const { job_id } = await startScan({ market: market.trim() })
      setJobId(job_id)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const handleResume = async () => {
    if (!jobId) return
    try {
      const { job_id } = await resumeScan(jobId)
      setJobId(job_id)
      setStatus(null)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const toggle = (index: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  const results: MissingAlbumResult[] | null = status?.results ?? null

  // If a download folder is (re-)chosen, or an album finishes downloading,
  // after some results are already selected, drop any now-known-duplicate
  // albums from the selection.
  useEffect(() => {
    if (!results) return
    setSelected((prev) => {
      const next = new Set(prev)
      for (const i of prev) {
        if (isAlreadyDownloaded(results[i])) next.delete(i)
      }
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingAlbumFolders, downloadedAlbums, results])

  const selectAll = () => {
    if (!results) return
    setSelected(new Set(results.map((_, i) => i).filter((i) => !isAlreadyDownloaded(results[i]))))
  }

  const clearSelection = () => setSelected(new Set())

  const handleDownloadSelected = async () => {
    if (!results) return
    const selectedAlbums = results.filter((_: MissingAlbumResult, i: number) => selected.has(i))
    if (selectedAlbums.length === 0) return
    setQueuing(true)
    setError(null)
    try {
      const { job_ids } = await submitJobsBatch({
        albums: selectedAlbums.map((r) => ({ artist: r.artist, album: r.album })),
      })
      onJobsCreated(
        job_ids.map((id, i) => ({
          id,
          label: `${selectedAlbums[i].artist} - ${selectedAlbums[i].album}`,
          artist: selectedAlbums[i].artist,
          album: selectedAlbums[i].album,
        })),
      )
      clearSelection()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setQueuing(false)
    }
  }

  return (
    <div className="card">
      <h2>Scan for region-locked tracks</h2>
      <p className="muted">
        Finds albums in your saved library and playlists with tracks unavailable in your market that you don't have
        downloaded yet.
      </p>

      {!jobId && (
        <form onSubmit={handleStart}>
          <label htmlFor="market">Market (2-letter country code)</label>
          <input
            id="market"
            value={market}
            onChange={(event) => setMarket(event.target.value)}
            placeholder="e.g. US, IL, GB"
            maxLength={2}
            required
          />
          <button type="submit">Scan my library</button>
        </form>
      )}

      {error && (
        <div className="status error" style={{ marginTop: '0.75rem' }}>
          <div className="status-text">{error}</div>
        </div>
      )}

      {status && (status.status === 'queued' || status.status === 'running') && (
        <div style={{ marginTop: '1rem' }}>
          <JobStatusView status={status.status} progress={status.progress} error={status.error} />
        </div>
      )}

      {status?.status === 'rate_limited' && (
        <div style={{ marginTop: '1rem' }}>
          <JobStatusView status={status.status} progress={status.progress} error={status.error} />
          <p className="muted" style={{ marginTop: '0.5rem' }}>
            Spotify's rate limit was hit partway through - your progress was saved.
            {status.will_auto_retry && ' Retrying automatically...'}
          </p>
          <button type="button" onClick={handleResume} style={{ marginTop: '0.5rem' }}>
            Resume scan
          </button>
        </div>
      )}

      {status?.status === 'error' && (
        <div style={{ marginTop: '1rem' }}>
          <JobStatusView status={status.status} progress={status.progress} error={status.error} />
        </div>
      )}

      {status?.status === 'done' && results && (
        <>
          {results.length === 0 ? (
            <p className="muted" style={{ marginTop: '1rem' }}>
              Nothing found - every region-locked track already has a home.
            </p>
          ) : (
            <>
              <div className="row" style={{ marginTop: '1rem' }}>
                <span className="muted">{results.length} album(s) with missing tracks</span>
                <span>
                  <button type="button" className="secondary" onClick={selectAll}>
                    Select all
                  </button>{' '}
                  <button type="button" className="secondary" onClick={clearSelection}>
                    Clear
                  </button>
                </span>
              </div>
              <div className="result-list">
                {results.map((result: MissingAlbumResult, i: number) => {
                  const already = isAlreadyDownloaded(result)
                  return (
                    <div
                      key={`${result.artist}-${result.album}-${i}`}
                      className={`result-item${already ? ' already-downloaded' : ''}`}
                      onClick={() => {
                        if (!already) toggle(i)
                      }}
                    >
                      {already ? (
                        <span className="result-check" role="img" aria-label="Already downloaded">
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
                      ) : (
                        <span
                          className={`result-check album-toggle${selected.has(i) ? ' checked' : ''}`}
                          role="checkbox"
                          aria-checked={selected.has(i)}
                          tabIndex={0}
                          onKeyDown={(event) => {
                            if (event.key === ' ' || event.key === 'Enter') {
                              event.preventDefault()
                              toggle(i)
                            }
                          }}
                        >
                          {selected.has(i) ? (
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
                          ) : (
                            <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                              <circle cx="8" cy="8" r="7.25" stroke="#B3B3B3" strokeWidth="1.5" />
                              <path d="M8 4.75V11.25" stroke="#B3B3B3" strokeWidth="1.5" strokeLinecap="round" />
                              <path d="M4.75 8H11.25" stroke="#B3B3B3" strokeWidth="1.5" strokeLinecap="round" />
                            </svg>
                          )}
                        </span>
                      )}
                      <span>
                        <div>
                          {result.artist} - {result.album}
                          {already && (
                            <span className="muted"> ({wasJustDownloaded(result) ? 'downloaded' : 'already downloaded'})</span>
                          )}
                        </div>
                        <div className="titles">Missing: {result.missing_titles.join(', ')}</div>
                      </span>
                    </div>
                  )
                })}
              </div>
              <button type="button" disabled={selected.size === 0 || queuing} onClick={handleDownloadSelected}>
                {queuing ? 'Queuing...' : `Download selected (${selected.size})`}
              </button>
            </>
          )}
        </>
      )}
    </div>
  )
}
