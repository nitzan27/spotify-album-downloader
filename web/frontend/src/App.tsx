import { useEffect, useState } from 'react'
import { getMe } from './api'
import type { MeResponse } from './api'
import { albumFolderName, isDirectoryPickerSupported } from './downloadFolder'
import { ConnectAccount } from './components/ConnectAccount'
import { DownloadFolderPicker } from './components/DownloadFolderPicker'
import { ScanPanel } from './components/ScanPanel'
import { ManualDownloadForm } from './components/ManualDownloadForm'
import { DownloadsPanel } from './components/DownloadsPanel'
import type { TrackedJob } from './components/DownloadsPanel'

function App() {
  const [me, setMe] = useState<MeResponse | null>(null)
  const [jobs, setJobs] = useState<TrackedJob[]>([])
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
  const [downloadingAlbums, setDownloadingAlbums] = useState<Set<string>>(new Set())

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe({ logged_in: false, display_name: null, avatar_url: null }))
  }, [])

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

  // On browsers that support it, a download folder must be chosen before
  // scanning/downloading is unlocked - there's no more "download a zip
  // instead" fallback there. Browsers without the API (Firefox/Safari) never
  // gain a dirHandle at all, so gating on it would lock them out entirely;
  // they keep working exactly as before (zip download, no folder needed).
  const canDownload = !isDirectoryPickerSupported() || downloadDirHandle !== null

  return (
    <div className="app">
      <div className="brand">
        <svg width="28" height="28" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="14" cy="14" r="14" fill="#1DB954" />
          <circle cx="14" cy="14" r="8.5" fill="#121212" />
          <circle cx="14" cy="14" r="8.5" stroke="rgba(255,255,255,0.1)" strokeWidth="1" />
          <circle cx="14" cy="14" r="5.5" fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth="1" />
          <circle cx="14" cy="14" r="2.4" fill="#1DB954" />
          <circle cx="14" cy="14" r="0.9" fill="#121212" />
        </svg>
        <h1>Album Downloader</h1>
      </div>

      {me && <ConnectAccount me={me} onLoggedOut={() => setMe({ logged_in: false, display_name: null, avatar_url: null })} />}

      <DownloadFolderPicker
        onFolderChosen={(folders, _name, dirHandle) => {
          setExistingAlbumFolders(folders)
          setDownloadDirHandle(dirHandle)
        }}
      />

      {!canDownload && (
        <p className="muted">Choose a download folder above to start scanning or downloading.</p>
      )}

      {canDownload && me?.logged_in && (
        <ScanPanel
          onJobsCreated={addJobs}
          existingAlbumFolders={existingAlbumFolders}
          downloadedAlbums={downloadedAlbums}
          downloadingAlbums={downloadingAlbums}
        />
      )}

      {canDownload && <ManualDownloadForm onJobCreated={addJob} existingAlbumFolders={existingAlbumFolders} />}

      <DownloadsPanel
        jobs={jobs}
        downloadDirHandle={downloadDirHandle}
        onJobDone={markAlbumDownloaded}
        onJobFailed={markAlbumDownloadFailed}
      />
    </div>
  )
}

export default App
