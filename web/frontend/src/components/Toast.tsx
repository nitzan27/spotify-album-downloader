import { useEffect, useState } from 'react'

interface Props {
  message: string
  onClose: () => void
}

const AUTO_DISMISS_MS = 4000
// Must match the CSS transition duration on `.toast-closing` - the fade
// needs to finish playing before the toast actually unmounts.
const FADE_OUT_MS = 300

export function Toast({ message, onClose }: Props) {
  const [closing, setClosing] = useState(false)

  useEffect(() => {
    const timer = window.setTimeout(() => setClosing(true), AUTO_DISMISS_MS)
    return () => window.clearTimeout(timer)
  }, [])

  useEffect(() => {
    if (!closing) return
    const timer = window.setTimeout(onClose, FADE_OUT_MS)
    return () => window.clearTimeout(timer)
  }, [closing, onClose])

  return (
    <div className={`toast${closing ? ' toast-closing' : ''}`} role="status">
      <span className="toast-text">{message}</span>
      <button type="button" className="toast-close" aria-label="Dismiss" onClick={() => setClosing(true)}>
        <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  )
}
