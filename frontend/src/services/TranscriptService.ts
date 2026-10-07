import type {
  TranscriptResult,
  AllTranscriptsResponse,
  TranscriptSimpleResponse,
  ChannelVideoTranscriptSimple,
  TranscriptJobProgressData,
  OutputLanguage,
  UnifiedTranscriptResponse,
} from '../types';

const API_BASE = '/api';

class TranscriptService {
  private transcriptCache = new Map<string, AllTranscriptsResponse>();
  private translationCache = new Map<string, TranscriptResult>();

  async fetchAllTranscripts(videoId: string): Promise<AllTranscriptsResponse> {
    const cached = this.transcriptCache.get(videoId);
    if (cached) {
      console.log(`[TranscriptService] Cache HIT for ${videoId}`);
      return cached;
    }

    console.log(`[TranscriptService] Fetching ALL transcripts for ${videoId}`);
    const resp = await fetch(`${API_BASE}/transcript/${videoId}/all`);
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.error || `Server returned ${resp.status}`);
    }

    const data: AllTranscriptsResponse = await resp.json();
    console.log(`[TranscriptService] Response: success=${data.success}, manual=${data.manual ? 'YES' : 'NULL'}, auto=${data.auto ? 'YES' : 'NULL'}, whisper=${data.whisper ? 'YES' : 'NULL'}`);

    if (data.success) {
      this.transcriptCache.set(videoId, data);
    }
    return data;
  }

  async fetchTranscript(videoId: string): Promise<TranscriptResult> {
    const cached = this.transcriptCache.get(videoId);
    if (cached) {
      const best = cached.manual || cached.auto || cached.whisper;
      if (best) return best;
    }

    console.log(`[TranscriptService] Fetching single transcript for ${videoId}`);
    const resp = await fetch(`${API_BASE}/transcript/${videoId}`);
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.error || `Server returned ${resp.status}`);
    }

    const data: TranscriptResult = await resp.json();
    return data;
  }

  async translateTranscript(
    videoId: string,
    targetLang: string,
  ): Promise<TranscriptResult> {
    const cacheKey = `${videoId}:${targetLang}`;
    const cached = this.translationCache.get(cacheKey);
    if (cached) {
      console.log(`[TranscriptService] Translation cache HIT for ${targetLang}`);
      return cached;
    }

    console.log(`[TranscriptService] Fetching translation for ${targetLang}`);
    const resp = await fetch(
      `${API_BASE}/transcript/${videoId}/translate/${targetLang}`,
    );
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.error || `Server returned ${resp.status}`);
    }
    const data: TranscriptResult = await resp.json();
    console.log(`[TranscriptService] Translation response: success=${data.success}, lang=${data.language}`);
    if (data.success) {
      this.translationCache.set(cacheKey, data);
    }
    return data;
  }

  getCachedTranslation(
    videoId: string,
    targetLang: string,
  ): TranscriptResult | undefined {
    return this.translationCache.get(`${videoId}:${targetLang}`);
  }

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

  // Simplified v2 — returns ONLY title, duration, transcript
  async fetchTranscriptSimple(videoId: string): Promise<TranscriptSimpleResponse> {
    console.log(`[TranscriptService] Fetching simplified transcript for ${videoId}`);
    const resp = await fetch(`${API_BASE}/transcriptv2/${videoId}`);
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new Error(body?.message || `Server returned ${resp.status}`);
    }
    return resp.json();
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

  clearCache(): void {
    this.transcriptCache.clear();
    this.translationCache.clear();
  }
}

export const transcriptService = new TranscriptService();

