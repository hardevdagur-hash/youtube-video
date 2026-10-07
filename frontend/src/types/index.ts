export interface Step {
  name: string;
  status: 'pending' | 'running' | 'ok' | 'error';
  detail: string;
}

// Simplified v2 types (Workflow 2 — title, duration, transcript + complete metadata)
export interface TranscriptSimpleResponse {
  title: string;
  duration: string;
  transcript: string;
  raw_transcript?: string;
  video_id?: string;
  video_url?: string;
  duration_seconds?: number;
  language?: string;
  script?: string;
  status?: string;
  source?: string | null;
  method?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

export interface ChannelVideoTranscriptSimple {
  video_id?: string;
  video_url?: string;
  channel_id?: string;
  channel_title?: string;
  title: string;
  published_at?: string;
  duration_seconds?: number;
  duration: string;
  language?: string;
  script?: string;
  status?: 'success' | 'failed' | 'processing' | 'pending' | 'rate_limited' | 'no_captions' | 'temporary_error';
  transcript: string;
  raw_transcript?: string;
  source?: string | null;
  method?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  retryable?: boolean;
  attempt_count?: number;
  next_retry_at?: string | null;
}

export interface TranscriptJobProgressData {
  job_id: string;
  channel_handle: string;
  channel_id: string;
  channel_title: string;
  status: 'queued' | 'running' | 'cooldown' | 'paused' | 'completed' | 'failed' | 'cancelled';
  total_discovered: number;
  eligible_videos: number;
  skipped_videos: number;
  processed: number;
  successful: number;
  caption_count: number;
  whisper_count: number;
  no_captions: number;
  rate_limited: number;
  failed: number;
  remaining: number;
  progress_percent: number;
  cooldown_seconds_remaining?: number;
  checkpoint_file?: string | null;
  published_after?: string | null;
  published_before?: string | null;
  output_language?: string;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  error?: string | null;
  videos: ChannelVideoTranscriptSimple[];
}

export const LANGUAGE_LABELS: Record<string, string> = {
  en: 'English',
  hi: 'Hindi',
  es: 'Spanish',
  fr: 'French',
  de: 'German',
  pt: 'Portuguese',
  ja: 'Japanese',
  ko: 'Korean',
  zh: 'Chinese',
  ru: 'Russian',
  ar: 'Arabic',
  it: 'Italian',
  nl: 'Dutch',
  tr: 'Turkish',
  vi: 'Vietnamese',
  th: 'Thai',
};

export function getLanguageLabel(code: string): string {
  return LANGUAGE_LABELS[code] || code.toUpperCase();
}

// --- Production Plan STT & Translation Types ---

export type OutputLanguage = 'original' | 'en' | 'hi';

export type PipelineStage =
  | 'IDLE'
  | 'VALIDATING'
  | 'CHECKING_CACHE'
  | 'FETCHING_CAPTIONS'
  | 'CAPTIONS_UNAVAILABLE'
  | 'EXTRACTING_AUDIO'
  | 'TRANSCRIBING'
  | 'CLEANING'
  | 'VALIDATING_QUALITY'
  | 'COMPLETED'
  | 'FAILED';

export interface UnifiedTranscriptSegment {
  start: number;
  end: number;
  duration: number;
  text: string;
}

export interface UnifiedTranscriptResponse {
  success: boolean;
  video_id: string;
  title?: string | null;
  source_language: string;
  output_language: OutputLanguage;
  provider: string;
  transcript: string;
  raw_transcript?: string;
  raw_segments?: UnifiedTranscriptSegment[];
  fallback_to_original?: boolean;
  segments: UnifiedTranscriptSegment[];
  word_count: number;
  duration_seconds?: number | null;
  confidence?: number;
  from_cache?: boolean;
  error_code?: string;
  message?: string;
  retryable?: boolean;
}

