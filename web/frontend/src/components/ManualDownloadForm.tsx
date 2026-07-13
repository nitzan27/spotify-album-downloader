import { useState } from 'react'
import type { FormEvent } from 'react'
import { submitJob } from '../api'
import { albumFolderName } from '../downloadFolder'
import type { TrackedJob } from './DownloadsPanel'

interface Props {
  onJobCreated: (job: TrackedJob) => void
  existingAlbumFolders: Set<string> | null
  canDownload: boolean
  onShowToast: (message: string) => void
}

export function ManualDownloadForm({ onJobCreated, existingAlbumFolders, canDownload, onShowToast }: Props) {
  const [artist, setArtist] = useState('')
  const [album, setAlbum] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [skippedMessage, setSkippedMessage] = useState<string | null>(null)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setSkippedMessage(null)
    const trimmedArtist = artist.trim()
    const trimmedAlbum = album.trim()

    // Replaces the browser's own native validation bubble (disabled via the
    // form's `noValidate`) with the same popup styling used everywhere else.
    if (!trimmedArtist || !trimmedAlbum) {
      onShowToast('Enter both an artist and an album.')
      return
    }

    if (!canDownload) {
      onShowToast('Choose a download folder above to enable downloads.')
      return
    }

    if (existingAlbumFolders?.has(albumFolderName(trimmedArtist, trimmedAlbum))) {
      setSkippedMessage(`You already have "${trimmedArtist} - ${trimmedAlbum}" in your download folder - skipping.`)
      return
    }

    setSubmitting(true)
    try {
      const { job_id } = await submitJob({ artist: trimmedArtist, album: trimmedAlbum })
      onJobCreated({
        id: job_id,
        label: `${trimmedArtist} - ${trimmedAlbum}`,
        artist: trimmedArtist,
        album: trimmedAlbum,
      })
      setArtist('')
      setAlbum('')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="card">
      <h2>Download an album</h2>
      <form onSubmit={handleSubmit} noValidate>
        <label htmlFor="artist">Artist</label>
        <input
          id="artist"
          value={artist}
          onChange={(event) => setArtist(event.target.value)}
          placeholder="e.g. Daft Punk"
          required
        />
        <label htmlFor="album">Album</label>
        <input
          id="album"
          value={album}
          onChange={(event) => setAlbum(event.target.value)}
          placeholder="e.g. Discovery"
          required
        />
        <button type="submit" className={!canDownload ? 'button-disabled-look' : undefined} disabled={submitting}>
          {submitting ? 'Starting...' : 'Download'}
        </button>
      </form>
      {error && (
        <div className="status error" style={{ marginTop: '0.75rem' }}>
          <div className="status-text">{error}</div>
        </div>
      )}
      {skippedMessage && (
        <div className="status" style={{ marginTop: '0.75rem' }}>
          <div className="status-text">{skippedMessage}</div>
        </div>
      )}
    </div>
  )
}
