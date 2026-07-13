import { useState } from 'react'
import type { MeResponse } from '../api'
import { logout } from '../api'

interface Props {
  me: MeResponse
  onLoggedOut: () => void
}

function Avatar({ avatarUrl, displayName }: { avatarUrl: string | null; displayName: string | null }) {
  const [imageFailed, setImageFailed] = useState(false)

  if (avatarUrl && !imageFailed) {
    return <img className="avatar" src={avatarUrl} alt="" onError={() => setImageFailed(true)} />
  }

  return (
    <span className="avatar avatar-placeholder" aria-hidden="true">
      {displayName ? displayName.charAt(0).toUpperCase() : '?'}
    </span>
  )
}

export function ConnectAccount({ me, onLoggedOut }: Props) {
  const handleLogout = async () => {
    await logout()
    onLoggedOut()
  }

  return (
    <div className="card">
      <h2>Your Spotify account</h2>
      {me.logged_in ? (
        <div className="row">
          <span className="account-identity">
            <Avatar avatarUrl={me.avatar_url} displayName={me.display_name} />
            <span className="muted">Connected as {me.display_name ?? 'Spotify user'}</span>
          </span>
          <button type="button" className="secondary" onClick={handleLogout}>
            Log out
          </button>
        </div>
      ) : (
        <div>
          <p className="muted">Log in to scan your library for region-locked tracks.</p>
          <button type="button" onClick={() => { window.location.href = '/login' }}>
            Connect Spotify
          </button>
        </div>
      )}
    </div>
  )
}
