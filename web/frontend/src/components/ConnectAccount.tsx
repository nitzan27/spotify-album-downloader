import type { MeResponse } from '../api'
import { logout } from '../api'

interface Props {
  me: MeResponse
  onLoggedOut: () => void
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
          <span className="muted">Connected as {me.display_name ?? 'Spotify user'}</span>
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
