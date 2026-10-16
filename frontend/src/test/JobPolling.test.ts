import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { isJobPollable, startJobPolling } from '../services/jobPolling';
import { transcriptService } from '../services/TranscriptService';
import type { TranscriptJobProgressData } from '../types';

const job = (status: string) => ({ job_id: 'abcdef012345', status, videos: [] }) as unknown as TranscriptJobProgressData;

function httpError(status: number) {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

describe('startJobPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('only polls queued, running and cooldown jobs', () => {
    expect(['queued', 'running', 'cooldown'].every(isJobPollable)).toBe(true);
    expect(['paused', 'completed', 'failed', 'cancelled', undefined].some(isJobPollable)).toBe(false);
  });

  it('stops on a paused job and fetches the full job exactly once', async () => {
    const fetchProgress = vi.fn()
      .mockResolvedValueOnce(job('running'))
      .mockResolvedValueOnce(job('paused'));
    const fetchFull = vi.fn().mockResolvedValue(job('paused'));
    const onUpdate = vi.fn();
    const onStop = vi.fn();
    startJobPolling({ jobId: 'abcdef012345', fetchProgress, fetchFull, onUpdate, onStop, intervalMs: 1000 });

    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(fetchFull).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchProgress).toHaveBeenCalledTimes(2); // no polling after the pause
    expect(onUpdate).toHaveBeenLastCalledWith(job('paused'));
  });

  it.each([401, 404])('stops polling after HTTP %i', async status => {
    const fetchProgress = vi.fn().mockRejectedValue(httpError(status));
    const onFatalError = vi.fn();
    startJobPolling({
      jobId: 'abcdef012345', fetchProgress, fetchFull: vi.fn(), onUpdate: vi.fn(), onFatalError, intervalMs: 1000,
    });
    await vi.advanceTimersByTimeAsync(20000);
    expect(fetchProgress).toHaveBeenCalledTimes(1);
    expect(onFatalError).toHaveBeenCalledTimes(1);
  });

  it('never overlaps requests while a slow request is in flight', async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const fetchProgress = vi.fn(async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise(resolve => setTimeout(resolve, 5000)); // slower than the interval
      inFlight -= 1;
      return job('running');
    });
    const stop = startJobPolling({
      jobId: 'abcdef012345', fetchProgress, fetchFull: vi.fn(), onUpdate: vi.fn(), intervalMs: 1000,
    });
    await vi.advanceTimersByTimeAsync(20000);
    stop();
    expect(maxInFlight).toBe(1);
    expect(fetchProgress.mock.calls.length).toBeLessThanOrEqual(4);
  });

  it('stops when the returned cancel function is called', async () => {
    const fetchProgress = vi.fn().mockResolvedValue(job('running'));
    const stop = startJobPolling({
      jobId: 'abcdef012345', fetchProgress, fetchFull: vi.fn(), onUpdate: vi.fn(), intervalMs: 1000,
    });
    await vi.advanceTimersByTimeAsync(1000);
    stop();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchProgress).toHaveBeenCalledTimes(1);
  });
});

describe('getJobProgress', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests the lightweight progress view and exposes the HTTP status on errors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ success: false, message: 'gone' }), { status: 404 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await expect(transcriptService.getJobProgress('abcdef012345', 'progress')).rejects.toMatchObject({ status: 404 });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/transcript/jobs/abcdef012345?view=progress');
  });
});
