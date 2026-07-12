import { useEffect, useState } from 'react'
import { getMe } from './api'
import type { MeResponse } from './api'
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

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe({ logged_in: false, display_name: null }))
  }, [])

  const addJob = (job: TrackedJob) => setJobs((prev) => [...prev, job])
  const addJobs = (newJobs: TrackedJob[]) => setJobs((prev) => [...prev, ...newJobs])

  return (
    <div className="app">
      <div className="brand">
        <svg width="28" height="28" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="14" cy="14" r="14" fill="#1DB954" />
          <path
            d="M8 18.5V9.5L20 8V17"
            stroke="#121212"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <circle cx="8" cy="19.5" r="2" fill="#121212" />
          <circle cx="20" cy="18" r="2" fill="#121212" />
        </svg>
        <h1>Album Downloader</h1>
      </div>

      {me && <ConnectAccount me={me} onLoggedOut={() => setMe({ logged_in: false, display_name: null })} />}

      <DownloadFolderPicker
        onFolderChosen={(folders, _name, dirHandle) => {
          setExistingAlbumFolders(folders)
          setDownloadDirHandle(dirHandle)
        }}
      />

      {me?.logged_in && <ScanPanel onJobsCreated={addJobs} existingAlbumFolders={existingAlbumFolders} />}

      <ManualDownloadForm onJobCreated={addJob} existingAlbumFolders={existingAlbumFolders} />

      <DownloadsPanel jobs={jobs} downloadDirHandle={downloadDirHandle} />
    </div>
  )
}

export default App
