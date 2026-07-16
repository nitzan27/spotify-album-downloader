export interface MeResponse {
  logged_in: boolean;
  display_name: string | null;
  avatar_url: string | null;
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
  // True while the backend may still auto-requeue this same job id (see
  // jobs.MAX_AUTO_RETRIES) - for a scan, false means a rate_limited job
  // needs an explicit "Resume scan" click; a download has no such button and
  // self-resolves to "error" once retries are exhausted, so this is
  // informational only there (the frontend just keeps polling regardless).
  will_auto_retry?: boolean;
}

/**
 * True for a raw browser fetch failure (DNS blip, dropped connection, dev
 * server restart mid-request) - fetch() rejects with a TypeError in that
 * case, distinct from the plain Error thrown below for a real HTTP error
 * response (e.g. 404 job-not-found). Callers use this to decide whether a
 * failed poll is worth retrying or is a genuine terminal state.
 */
export function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError
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
