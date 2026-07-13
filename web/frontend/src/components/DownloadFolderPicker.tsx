import { useState } from 'react'
import {
  isDirectoryPickerSupported,
  listExistingAlbumFolders,
  pickDownloadFolder,
} from '../downloadFolder'

interface Props {
  onFolderChosen: (folders: Set<string>, folderName: string, dirHandle: FileSystemDirectoryHandle) => void
}

// Renders nothing on browsers without the File System Access API (Firefox,
// Safari) - this feature is simply unavailable there, not broken, so scans
// and downloads proceed exactly as before (zip download, no duplicate-skipping).
export function DownloadFolderPicker({ onFolderChosen }: Props) {
  const [folderName, setFolderName] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  if (!isDirectoryPickerSupported()) return null

  const handlePick = async () => {
    setError(null)
    try {
      const dirHandle = await pickDownloadFolder()
      const folders = await listExistingAlbumFolders(dirHandle)
      setFolderName(dirHandle.name)
      onFolderChosen(folders, dirHandle.name, dirHandle)
    } catch (err) {
      // The user closing the picker without choosing anything is not an error.
      if ((err as Error).name !== 'AbortError') {
        setError((err as Error).message)
      }
    }
  }

  return (
    <div className="card">
      <h2>Download folder</h2>
      <p className="muted">Skips albums you already have, and saves downloads straight to disk.</p>
      <button type="button" className="secondary" onClick={handlePick}>
        {folderName ? `Change folder (${folderName})` : 'Choose download folder'}
      </button>
      {error && (
        <div className="status error" style={{ marginTop: '0.75rem' }}>
          <div className="status-text">{error}</div>
        </div>
      )}
    </div>
  )
}
