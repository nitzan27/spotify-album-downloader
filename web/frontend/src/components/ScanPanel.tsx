import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  getJobStatus,
  isNetworkError,
  resumeScan,
  startScan,
  submitJobsBatch,
} from '../api'
import type { JobStatusResponse, MissingAlbumResult } from '../api'
import { albumFolderName } from '../downloadFolder'
import { COUNTRY_CODES } from '../countryCodes'
import { JobStatusView } from './JobStatusView'
import type { TrackedJob } from './DownloadsPanel'

const POLL_INTERVAL_MS = 2000
const MAX_POLL_RETRIES = 5
const MAX_POLL_RETRY_DELAY_MS = 15000

interface Props {
  onJobsCreated: (jobs: TrackedJob[]) => void
  existingAlbumFolders: Set<string> | null
  downloadedAlbums: Set<string>
  downloadingAlbums: Set<string>
  canDownload: boolean
  onBlockedDownload: () => void
}

export function ScanPanel({
  onJobsCreated,
  existingAlbumFolders,
  downloadedAlbums,
  downloadingAlbums,
  canDownload,
  onBlockedDownload,
}: Props) {
  const [market, setMarket] = useState('IL')
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

  const isDownloading = (result: MissingAlbumResult) =>
    downloadingAlbums.has(albumFolderName(result.artist, result.album))

  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    let timer: number | undefined

    const poll = async (retriesLeft = MAX_POLL_RETRIES) => {
      try {
        const result = await getJobStatus(jobId)
        if (cancelled) return
        setStatus(result)
        const stillInFlight =
          result.status === 'queued' ||
          result.status === 'running' ||
          (result.status === 'rate_limited' && result.will_auto_retry)
        if (stillInFlight) {
          timer = window.setTimeout(() => poll(MAX_POLL_RETRIES), POLL_INTERVAL_MS)
        }
      } catch (err) {
        if (cancelled) return
        // A raw network blip (DNS drop, dev server restart) shouldn't throw
        // away a long scan's progress - retry with backoff before surfacing
        // an error, same reasoning as DownloadsPanel's job polling.
        if (isNetworkError(err) && retriesLeft > 0) {
          const attempt = MAX_POLL_RETRIES - retriesLeft
          const delay = Math.min(POLL_INTERVAL_MS * 2 ** attempt, MAX_POLL_RETRY_DELAY_MS)
          timer = window.setTimeout(() => poll(retriesLeft - 1), delay)
          return
        }
        setError((err as Error).message)
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

  // If a download folder is (re-)chosen, an album finishes downloading, or an
  // album starts downloading (e.g. queued from a previous scan), drop any
  // now-unselectable albums from the selection.
  useEffect(() => {
    if (!results) return
    setSelected((prev) => {
      const next = new Set(prev)
      for (const i of prev) {
        if (isAlreadyDownloaded(results[i]) || isDownloading(results[i])) next.delete(i)
      }
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingAlbumFolders, downloadedAlbums, downloadingAlbums, results])

  const selectAll = () => {
    if (!results) return
    setSelected(
      new Set(results.map((_, i) => i).filter((i) => !isAlreadyDownloaded(results[i]) && !isDownloading(results[i]))),
    )
  }

  const clearSelection = () => setSelected(new Set())

  const handleDownloadSelected = async () => {
    if (!canDownload) {
      onBlockedDownload()
      return
    }
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
          createdAt: Date.now(),
        })),
      )
      clearSelection()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setQueuing(false)
    }
  }

  // Only stretch to match the sidebar's height (see styles.css) once there
  // are results to fill that space with - an empty/pre-scan card staying
  // small looks intentional, whereas stretching it before there's anything
  // to show would just be dead space.
  const hasResults = results !== null && results.length > 0

  return (
    <div className={`card${hasResults ? ' card-filled' : ''}`}>
      <h2>Scan for region-locked tracks</h2>
      <p className="muted">
        Finds albums in your saved library and playlists with tracks unavailable in your market that you don't have
        downloaded yet.
      </p>

      {!jobId && (
        <form onSubmit={handleStart}>
          <label htmlFor="market">Market</label>
          <select id="market" value={market} onChange={(event) => setMarket(event.target.value)} required>
            {COUNTRY_CODES.map(({ code, name }) => (
              <option key={code} value={code}>
                {code} - {name}
              </option>
            ))}
          </select>
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
                <span className="result-count">
                  {results.length} album{results.length === 1 ? '' : 's'} with missing tracks
                </span>
                <span>
                  <button type="button" className="secondary-inverted" onClick={selectAll}>
                    Select all
                  </button>{' '}
                  <button type="button" className="secondary-inverted" onClick={clearSelection}>
                    Clear
                  </button>
                </span>
              </div>
              <div className="result-list">
                {results.map((result: MissingAlbumResult, i: number) => {
                  const already = isAlreadyDownloaded(result)
                  const downloading = isDownloading(result)
                  const locked = already || downloading
                  return (
                    <div
                      key={`${result.artist}-${result.album}-${i}`}
                      className={`result-item${locked ? ' already-downloaded' : ''}`}
                      onClick={() => {
                        if (!locked) toggle(i)
                      }}
                    >
                      {downloading ? (
                        <span className="result-check" role="img" aria-label="Downloading">
                          <span className="spinner" />
                        </span>
                      ) : already ? (
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
                      <span className="result-text">
                        <div className="result-title">
                          {result.artist} - {result.album}
                          {downloading && <span className="muted"> (downloading...)</span>}
                          {!downloading && already && (
                            <span className="muted"> ({wasJustDownloaded(result) ? 'downloaded' : 'already downloaded'})</span>
                          )}
                        </div>
                        <div className="titles">Missing: {result.missing_titles.join(', ')}</div>
                      </span>
                    </div>
                  )
                })}
              </div>
              <button
                type="button"
                className={!canDownload ? 'button-disabled-look' : undefined}
                // When a folder isn't chosen yet, stay clickable regardless
                // of selection so the popup always fires - the "nothing
                // selected" disable only applies once a folder is set and
                // that's the only remaining reason to block the click.
                disabled={queuing || (canDownload && selected.size === 0)}
                onClick={handleDownloadSelected}
              >
                {queuing ? 'Queuing...' : `Download selected (${selected.size})`}
              </button>
            </>
          )}
        </>
      )}
    </div>
  )
}
