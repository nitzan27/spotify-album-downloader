export interface MeResponse {
  logged_in: boolean;
  display_name: string | null;
}

export interface ScanRequest {
  market: string;
}

export interface ScanJobResponse {
  job_id: string;
}

export interface NewJobRequest {
  artist: string;
  album: string;
}

export interface BatchJobRequest {
  albums: NewJobRequest[];
}

export interface BatchJobResponse {
  job_ids: string[];
}

export interface MissingAlbumResult {
  artist: string;
  album: string;
  missing_titles: string[];
}

export interface JobStatusResponse {
  status: "queued" | "running" | "done" | "error" | "rate_limited";
  progress: string;
  error: string | null;
  results?: MissingAlbumResult[]; // Populated only for completed scan jobs
  // Scan jobs only: true while the backend may still auto-requeue this same
  // job id (see jobs.MAX_SCAN_AUTO_RETRIES) - if false, a rate_limited job
  // needs an explicit "Resume scan" click to make further progress.
  will_auto_retry?: boolean;
}

/**
 * Helper to ensure we process API errors uniformly
 */
async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      errorData.detail || `Request failed with status ${response.status}`,
    );
  }
  return response.json() as Promise<T>;
}

/**
 * GET /me - Check current Spotify login status
 */
export async function getMe(): Promise<MeResponse> {
  const res = await fetch("/me");
  return handleResponse<MeResponse>(res);
}

/**
 * POST /logout - Clear session and Spotify tokens
 */
export async function logout(): Promise<void> {
  const res = await fetch("/logout", {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error("Logout failed");
  }
}

/**
 * POST /scan - Trigger a library region-lock scan for a given country market
 */
export async function startScan(
  payload: ScanRequest,
): Promise<ScanJobResponse> {
  const res = await fetch("/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return handleResponse<ScanJobResponse>(res);
}

/**
 * POST /scan/{job_id}/resume - Resume a rate-limited scan job
 */
export async function resumeScan(jobId: string): Promise<ScanJobResponse> {
  const res = await fetch(`/scan/${jobId}/resume`, {
    method: "POST",
  });
  return handleResponse<ScanJobResponse>(res);
}

/**
 * POST /jobs - Submit a single manual download request
 */
export async function submitJob(
  payload: NewJobRequest,
): Promise<ScanJobResponse> {
  const res = await fetch("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return handleResponse<ScanJobResponse>(res);
}

/**
 * POST /jobs/batch - Submit multiple download jobs at once (e.g. from scan results)
 */
export async function submitJobsBatch(
  payload: BatchJobRequest,
): Promise<BatchJobResponse> {
  const res = await fetch("/jobs/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return handleResponse<BatchJobResponse>(res);
}

/**
 * GET /status/{job_id} - Track progress/results of a download or scan job
 */
export async function getJobStatus(jobId: string): Promise<JobStatusResponse> {
  const res = await fetch(`/status/${jobId}`);
  return handleResponse<JobStatusResponse>(res);
}

/**
 * Returns the download URL for a completed download job.
 * The UI can simply assign this to a window.location or an <a> download link.
 */
export function getDownloadUrl(jobId: string): string {
  return `/download/${jobId}`;
}
