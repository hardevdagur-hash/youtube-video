import type {
  ChannelVideoTranscriptSimple,
  TranscriptJobProgressData,
  OutputLanguage,
  UnifiedTranscriptResponse,
} from '../types';

const API_BASE = '/api';

class TranscriptService {
  // Unified Production Transcript Endpoint — Captions first -> Groq Whisper Large V3 fallback -> Simple English Default
  async fetchUnifiedTranscript(
    videoUrl: string,
    outputLanguage: OutputLanguage = 'en'
  ): Promise<UnifiedTranscriptResponse> {
    console.log(`[TranscriptService] Fetching unified transcript for ${videoUrl} (lang=${outputLanguage})`);
    const resp = await fetch(`${API_BASE}/transcript`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_url: videoUrl, output_language: outputLanguage }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data?.success) {
      const err = new Error(data?.message || `Server returned ${resp.status}`);
      (err as any).error_code = data?.error_code || 'TRANSCRIPTION_FAILED';
      (err as any).retryable = data?.retryable ?? true;
      throw err;
    }
    return data as UnifiedTranscriptResponse;
  }

  // Channel transcripts — returns complete 15-column metadata per video
  async fetchChannelTranscriptsSimple(
    handle: string,
    limit: number = 100,
    concurrency: number = 5,
    allowWhisper: boolean = true,
    outputLanguage: OutputLanguage = 'original',
  ): Promise<ChannelVideoTranscriptSimple[]> {
    console.log(`[TranscriptService] Fetching channel transcripts: ${handle} (lang=${outputLanguage})`);
    const resp = await fetch(
      `${API_BASE}/channel/${encodeURIComponent(handle)}/transcripts?limit=${limit}&concurrency=${concurrency}&allow_whisper=${allowWhisper}&output_language=${encodeURIComponent(outputLanguage)}`
    );
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.message || `Server returned ${resp.status}`);
    }
    const body = await resp.json();
    if (body.success && body.data?.videos) {
      return body.data.videos;
    }
    throw new Error(body?.message || 'Unexpected response format');
  }

  // Background Job execution for large channels
  async startTranscriptJob(
    handle: string,
    maxVideos: number = 0,
    forceRefresh: boolean = false,
    publishedAfter?: string | null,
    publishedBefore?: string | null,
    outputLanguage: string = 'en',
  ): Promise<TranscriptJobProgressData> {
    console.log(`[TranscriptService] Starting background transcript job: ${handle}`);
    const resp = await fetch(`${API_BASE}/channel/${encodeURIComponent(handle)}/transcript-job`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        max_videos: maxVideos,
        force_refresh: forceRefresh,
        published_after: publishedAfter || null,
        published_before: publishedBefore || null,
        output_language: outputLanguage || 'en',
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.message || `Failed to start transcript job (${resp.status})`);
    }
    const body = await resp.json();
    return body.data;
  }

  async getJobProgress(jobId: string): Promise<TranscriptJobProgressData> {
    const resp = await fetch(`${API_BASE}/transcript/jobs/${encodeURIComponent(jobId)}`);
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.message || `Failed to get job progress (${resp.status})`);
    }
    const body = await resp.json();
    return body.data;
  }

  async cancelJob(jobId: string): Promise<void> {
    const resp = await fetch(`${API_BASE}/transcript/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.message || `Failed to cancel job (${resp.status})`);
    }
  }

  async resumeJob(jobId: string): Promise<TranscriptJobProgressData> {
    const resp = await fetch(`${API_BASE}/transcript/jobs/${encodeURIComponent(jobId)}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.message || `Failed to resume job (${resp.status})`);
    }
    const body = await resp.json();
    return body.data;
  }

  getJobDownloadUrl(jobId: string): string {
    return `${API_BASE}/transcript/jobs/${encodeURIComponent(jobId)}/download`;
  }
}

export const transcriptService = new TranscriptService();

