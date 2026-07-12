import type { ReactNode } from 'react'
import type { JobStatusResponse } from '../api'

interface Props {
  status: JobStatusResponse['status']
  progress: string
  error: string | null
  children?: ReactNode
}

export function JobStatusView({ status, progress, error, children }: Props) {
  const showSpinner = status === 'queued' || status === 'running'
  const text = status === 'error' ? `Error: ${error}` : progress

  return (
    <div className={`status ${status}`}>
      {showSpinner && <div className="spinner" />}
      <div className="status-text">
        {text}
        {children}
      </div>
    </div>
  )
}
