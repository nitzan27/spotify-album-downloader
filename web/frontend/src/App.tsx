import { useEffect, useState } from 'react'
import { getMe, submitJob } from './api'
import type { MeResponse } from './api'
import { albumFolderName, isDirectoryPickerSupported } from './downloadFolder'
import { ConnectAccount } from './components/ConnectAccount'
import { DownloadFolderPicker } from './components/DownloadFolderPicker'
import { ScanPanel } from './components/ScanPanel'
import { ManualDownloadForm } from './components/ManualDownloadForm'
import { DownloadsPanel } from './components/DownloadsPanel'
import type { TrackedJob } from './components/DownloadsPanel'
import { Toast } from './components/Toast'
import logoIcon from './assets/logo-icon.png'

// Tracked jobs survive a page reload (accidental refresh, browser/tab crash,
// or the dev server restarting mid-batch) by round-tripping through
// localStorage, so a big batch of selected albums never has to be re-picked
// from scratch - each row just resumes polling its own job id on mount.
// Mirrors web/jobs.py's JOB_TTL_SECONDS: entries older than that are
// guaranteed gone server-side, so there's no point restoring (or polling)
// them.
const JOBS_STORAGE_KEY = 'spotify-album-downloader:jobs'
const JOB_TTL_MS = 60 * 60 * 1000

function loadPersistedJobs(): TrackedJob[] {
  try {
    const raw = localStorage.getItem(JOBS_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as TrackedJob[]
    const cutoff = Date.now() - JOB_TTL_MS
    return parsed.filter((job) => job.createdAt > cutoff)
  } catch {
    return []
  }
}

function App() {
  const [me, setMe] = useState<MeResponse | null>(null)
  const [jobs, setJobs] = useState<TrackedJob[]>(loadPersistedJobs)
  // Null until a folder is chosen (File System Access API, Chromium-only) -
  // every consumer treats null as "no duplicate-skipping/save-to-folder
  // available", which is also what happens by default on unsupported browsers.
  const [existingAlbumFolders, setExistingAlbumFolders] = useState<Set<string> | null>(null)
  const [downloadDirHandle, setDownloadDirHandle] = useState<FileSystemDirectoryHandle | null>(null)
  // Albums downloaded during this page load (regardless of whether a folder
  // was chosen), keyed the same way as existingAlbumFolders so ScanPanel can
  // cross them off its results list without waiting for a folder re-scan.
  const [downloadedAlbums, setDownloadedAlbums] = useState<Set<string>>(new Set())
  // Albums with a download job currently queued/running - added the instant a
  // job is created (no need to wait for the first poll) and removed once that
  // job finishes or fails, so ScanPanel can block re-queuing a duplicate job
  // for an album that's already in flight.
  // Seeded from any restored jobs too - each row's first poll (on mount)
  // corrects this the moment it learns the job actually finished or failed,
  // same as a freshly-created job assumes in-flight until its first poll.
  const [downloadingAlbums, setDownloadingAlbums] = useState<Set<string>>(
    () => new Set(loadPersistedJobs().map((job) => albumFolderName(job.artist, job.album))),
  )

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe({ logged_in: false, display_name: null, avatar_url: null }))
  }, [])

  useEffect(() => {
    localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(jobs))
  }, [jobs])

  const addJob = (job: TrackedJob) => {
    setJobs((prev) => [...prev, job])
    setDownloadingAlbums((prev) => new Set(prev).add(albumFolderName(job.artist, job.album)))
  }
  const addJobs = (newJobs: TrackedJob[]) => {
    setJobs((prev) => [...prev, ...newJobs])
    setDownloadingAlbums((prev) => {
      const next = new Set(prev)
      for (const job of newJobs) next.add(albumFolderName(job.artist, job.album))
      return next
    })
  }
  const markAlbumDownloaded = (artist: string, album: string) => {
    const key = albumFolderName(artist, album)
    setDownloadedAlbums((prev) => new Set(prev).add(key))
    setDownloadingAlbums((prev) => {
      const next = new Set(prev)
      next.delete(key)
      return next
    })
  }
  const markAlbumDownloadFailed = (artist: string, album: string) => {
    const key = albumFolderName(artist, album)
    setDownloadingAlbums((prev) => {
      const next = new Set(prev)
      next.delete(key)
      return next
    })
  }

  // Re-submits a single failed job (a real terminal failure, or a job id
  // that no longer exists after too long a gap) without making the friend
  // re-scan or re-select anything - swaps the old row for a freshly created
  // one tracking the same artist/album.
  const handleRetryJob = async (oldJob: TrackedJob) => {
    setJobs((prev) => prev.filter((job) => job.id !== oldJob.id))
    try {
      const { job_id } = await submitJob({ artist: oldJob.artist, album: oldJob.album })
      addJob({ id: job_id, label: oldJob.label, artist: oldJob.artist, album: oldJob.album, createdAt: Date.now() })
    } catch (err) {
      setToastMessage(`Retry failed: ${(err as Error).message}`)
    }
  }

  // On browsers that support it, a download folder must be chosen before
  // scanning/downloading is unlocked - there's no more "download a zip
  // instead" fallback there. Browsers without the API (Firefox/Safari) never
  // gain a dirHandle at all, so gating on it would lock them out entirely;
  // they keep working exactly as before (zip download, no folder needed).
  const canDownload = !isDirectoryPickerSupported() || downloadDirHandle !== null

  // Download buttons stay clickable even while `!canDownload` (styled as
  // greyed-out, not natively `disabled`) specifically so clicking one can
  // trigger this popup - see ScanPanel's onBlockedDownload and
  // ManualDownloadForm's onShowToast (which also covers its own empty-field
  // validation, replacing the browser's native validation bubble).
  const [toastMessage, setToastMessage] = useState<string | null>(null)

  return (
    <div className="app">
      {toastMessage && <Toast message={toastMessage} onClose={() => setToastMessage(null)} />}
      <div className="brand">
        <img src={logoIcon} alt="" className="brand-icon" />
        <div className="brand-text">
          <h1>Fuck Clairo</h1>
          <p className="tagline">album downloader</p>
        </div>
      </div>

      <div className="layout-sidebar">
        <div className="layout-account">
          {me && <ConnectAccount me={me} onLoggedOut={() => setMe({ logged_in: false, display_name: null, avatar_url: null })} />}
        </div>

        <div className="layout-folder">
          <DownloadFolderPicker
            onFolderChosen={(folders, _name, dirHandle) => {
              setExistingAlbumFolders(folders)
              setDownloadDirHandle(dirHandle)
            }}
          />
        </div>

        <div className="layout-manual">
          <ManualDownloadForm
            onJobCreated={addJob}
            existingAlbumFolders={existingAlbumFolders}
            canDownload={canDownload}
            onShowToast={setToastMessage}
          />
        </div>
      </div>

      <div className="layout-main">
        {me?.logged_in && (
          <ScanPanel
            onJobsCreated={addJobs}
            existingAlbumFolders={existingAlbumFolders}
            downloadedAlbums={downloadedAlbums}
            downloadingAlbums={downloadingAlbums}
            canDownload={canDownload}
            onBlockedDownload={() => setToastMessage('Choose a download folder above to enable downloads.')}
          />
        )}
      </div>

      <div className="layout-downloads">
        <DownloadsPanel
          jobs={jobs}
          downloadDirHandle={downloadDirHandle}
          onJobDone={markAlbumDownloaded}
          onJobFailed={markAlbumDownloadFailed}
          onRetry={handleRetryJob}
        />
      </div>
    </div>
  )
}

export default App
