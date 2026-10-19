import type { TranscriptJobProgressData } from '../types';

// Statuses in which a job is still changing on the server. Paused jobs only change when
// the user resumes them, and completed/failed/cancelled jobs never change again.
const POLLABLE_STATUSES = new Set(['queued', 'running', 'cooldown']);

// HTTP statuses after which polling can never succeed (signed out, job gone or not ours).
const FATAL_HTTP_STATUSES = new Set([401, 403, 404]);

export function isJobPollable(status: string | undefined | null): boolean {
  return !!status && POLLABLE_STATUSES.has(status);
}

export interface JobPollingOptions {
  jobId: string;
  // Cheap progress view (no transcript bodies), fetched on every tick.
  fetchProgress: (jobId: string) => Promise<TranscriptJobProgressData>;
  // Full job with transcripts, fetched once when the job stops changing.
  fetchFull: (jobId: string) => Promise<TranscriptJobProgressData>;
  onUpdate: (job: TranscriptJobProgressData) => void;
  // The job reached a paused or terminal status; polling has stopped.
  onStop?: (job: TranscriptJobProgressData) => void;
  // A fatal error (401/403/404); polling has stopped.
  onFatalError?: (err: unknown) => void;
  intervalMs?: number;
  maxBackoffMs?: number;
}

/**
 * Poll a background job with a chained setTimeout: the next request is scheduled only
 * after the previous one settles, so requests never overlap. Stops on paused/terminal
 * statuses and on 401/403/404; transient errors back off up to ``maxBackoffMs``.
 * Returns a function that stops polling.
 */
export function startJobPolling(options: JobPollingOptions): () => void {
  const interval = options.intervalMs ?? 1500;
  const maxBackoff = options.maxBackoffMs ?? 15000;
  let stopped = false;
  let delay = interval;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const schedule = () => {
    if (!stopped) timer = setTimeout(tick, delay);
  };

  async function tick() {
    if (stopped) return;
    try {
      const job = await options.fetchProgress(options.jobId);
      if (stopped) return;
      delay = interval;
      if (isJobPollable(job.status)) {
        options.onUpdate(job);
        schedule();
        return;
      }
      let finalJob = job;
      try {
        finalJob = await options.fetchFull(options.jobId);
      } catch {
        // Keep the progress view; the CSV download still has every transcript.
      }
      if (stopped) return;
      stopped = true;
      options.onUpdate(finalJob);
      options.onStop?.(finalJob);
    } catch (err) {
      if (stopped) return;
      const status = (err as { status?: number } | null)?.status;
      if (status !== undefined && FATAL_HTTP_STATUSES.has(status)) {
        stopped = true;
        options.onFatalError?.(err);
        return;
      }
      delay = Math.min(delay * 2, maxBackoff);
      schedule();
    }
  }

  schedule();
  return () => {
    stopped = true;
    if (timer !== undefined) clearTimeout(timer);
  };
}
