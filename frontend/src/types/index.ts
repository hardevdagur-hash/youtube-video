export interface Step {
  name: string;
  status: 'pending' | 'running' | 'ok' | 'error';
  detail: string;
}

export interface ProgressData {
  steps: Step[];
  complete: boolean;
}

export interface ResultData {
  success: boolean;
  run_id?: string;
  channel_title?: string;
  channel_id?: string;
  total_videos?: number;
  total_discovered?: number;
  total_api_calls?: number;
  file_size_bytes?: number;
  elapsed_seconds?: number;
  error?: string;
  steps?: Step[];
}

export interface RunResponse {
  run_id: string;
}

export type ExportFormat = 'csv' | 'excel' | 'json';

// --- New v3 Export Types ---

export interface StageProgress {
  stage: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  label: string;
  detail: string;
  progress_pct: number;
  current_page: number;
  total_pages: number;
  processed: number;
  total: number;
  remaining: number;
  eta_seconds: number;
  api_calls: number;
  rows_written: number;
  elapsed: number;
  error: string;
}

export interface V3ExportProgress {
  success: boolean;
  job_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  current_stage: string;
  overall_progress_pct: number;
  elapsed_seconds: number;
  eta_seconds: number;
  stages: Record<string, StageProgress>;
  error: string;
  error_type: string;
  error_action: string;
}

export interface V3ExportResult {
  success: boolean;
  job_id: string;
  status: string;
  error?: string;
  result?: {
    success: boolean;
    job_id: string;
    channel_title: string;
    channel_id: string;
    total_videos: number;
    total_discovered: number;
    total_api_calls: number;
    file_size_bytes: number;
    elapsed_seconds: number;
    csv_path: string;
    cache_hits: number;
    cache_misses: number;
    total_retries: number;
    error: string;
    error_type: string;
  };
}

export interface V3ExportResponse {
  success: boolean;
  job_id: string;
  status: string;
}

export interface DurationInfo {
  iso: string | null;
  readable: string | null;
  compact: string | null;
  seconds: number | null;
}

export interface VideoStatistics {
  views: number;
  likes: number;
  comments: number;
  views_formatted: string;
  likes_formatted: string;
  comments_formatted: string;
}

export interface Thumbnails {
  default: string | null;
  medium: string | null;
  high: string | null;
  standard: string | null;
  maxres: string | null;
}

export interface DateInfo {
  iso: string | null;
  localized: string | null;
  relative: string | null;
}

export interface ChannelInfo {
  name: string | null;
  id: string | null;
  url: string | null;
  verified: boolean | null;
}

export interface DescriptionInfo {
  full: string | null;
  urls: string[];
  hashtags: string[];
  mentions: string[];
}

export interface VideoMetadata {
  video_id: string;
  title: string | null;
  description: DescriptionInfo;
  channel: ChannelInfo;
  published_at: DateInfo;
  duration: DurationInfo;
  statistics: VideoStatistics;
  thumbnails: Thumbnails;
  tags: string[];
  category_id: string | null;
  language: string | null;
  license: string | null;
  embeddable: boolean | null;
  caption: boolean | null;
  privacy: string | null;
  live_status: string | null;
  default_audio_language: string | null;
}

export interface VideoMetadataResponse {
  success: boolean;
  video: VideoMetadata | null;
  error: string | null;
}

// Transcript types
export type TranscriptSource = 'manual' | 'auto' | 'whisper';
export type TranscriptProvider = 'youtube_manual' | 'youtube_auto' | 'faster_whisper';
export type PipelineStepStatus = 'pending' | 'running' | 'ok' | 'error' | 'skipped';

export interface TranscriptSegment {
  start: number;
  end: number;
  duration: number;
  text: string;
}

export interface AvailableLanguage {
  language: string;
  language_code: string;
  is_generated: boolean;
  is_translatable: boolean;
}

export interface WhisperProcessingInfo {
  model_name: string;
  transcription_duration_seconds: number | null;
  audio_duration_seconds: number | null;
  processing_time_seconds: number | null;
  language_detected: string | null;
  language_confidence: number | null;
  word_timestamps: boolean;
  audio_download_time_seconds: number | null;
}

export interface PipelineStep {
  name: string;
  status: PipelineStepStatus;
  detail: string;
  duration_seconds: number | null;
}

export interface AllTranscriptsResponse {
  success: boolean;
  video_id: string;
  manual: TranscriptResult | null;
  auto: TranscriptResult | null;
  whisper: TranscriptResult | null;
  pipeline_steps: PipelineStep[];
  available_languages: AvailableLanguage[];
}

export interface TranscriptResult {
  success: boolean;
  video_id: string;
  source: TranscriptSource;
  provider: TranscriptProvider;
  language: string;
  language_confidence: number | null;
  segments: TranscriptSegment[];
  plain_text: string;
  raw_transcript?: string;
  paragraph_text: string;
  word_count: number;
  character_count: number;
  estimated_read_time: string;
  generated_at: string;
  duration_seconds: number | null;
  whisper_info: WhisperProcessingInfo | null;
  pipeline_steps: PipelineStep[];
  available_languages: AvailableLanguage[];
  translation_source: string | null;
  error: string | null;
}

// Channel Transcript types
export interface ChannelStatistics {
  videos_found: number;
  eligible_videos: number;
  shorts_skipped: number;
  processed: number;
  failed: number;
  success_rate: number;
  processing_time_ms: number;
  average_duration_seconds: number;
  languages_found: string[];
}

export interface ChannelResultInfo {
  id: string;
  title: string;
  handle: string;
}

export interface ChannelVideoTranscript {
  video_id: string;
  title: string;
  duration_seconds: number;
  duration_readable: string;
  published_at: string;
  thumbnail: string | null;
  transcript_available: boolean;
  language: string | null;
  provider: string | null;
  transcript: TranscriptResult | null;
  error: string | null;
}

export interface ChannelTranscriptResponse {
  channel: ChannelResultInfo;
  statistics: ChannelStatistics;
  videos: ChannelVideoTranscript[];
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

export interface ChannelTranscriptSimpleResponse {
  success: boolean;
  data: {
    channel_id?: string;
    channel_title?: string;
    total_discovered?: number;
    eligible_count?: number;
    skipped_count?: number;
    successful_count?: number;
    caption_count?: number;
    whisper_count?: number;
    failed_count?: number;
    videos: ChannelVideoTranscriptSimple[];
  };
  message?: string;
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

export type TranscriptTabSource = 'manual' | 'auto' | 'whisper' | 'translated';

export interface TranscriptTab {
  key: string;
  label: string;
  source: TranscriptTabSource;
  available: boolean;
  language: string;
  languageCode: string;
}

// Workflow types
export type WorkflowStep = 'url' | 'metadata' | 'transcript';

export const WORKFLOW_STEPS: { key: WorkflowStep; label: string; number: number }[] = [
  { key: 'url', label: 'Video URL', number: 1 },
  { key: 'metadata', label: 'Metadata', number: 2 },
  { key: 'transcript', label: 'Transcript', number: 3 },
];

// Phase 5 — Processing types
export interface ProcessingStep {
  name: string;
  status: 'pending' | 'running' | 'ok' | 'error' | 'skipped';
  detail: string;
  duration_ms: number | null;
}

export interface LanguageDistribution {
  primary: string;
  secondary: string | null;
  primary_confidence: number;
  secondary_confidence: number | null;
  mixed_ratio: number | null;
}

export interface ProcessingStatistics {
  word_count: number;
  character_count: number;
  paragraph_count: number;
  sentence_count: number;
  estimated_read_time: string;
  avg_sentence_length_words: number;
  avg_paragraph_length_words: number;
  longest_sentence_words: number;
  filler_word_count: number;
}

export interface ProcessingFlags {
  timestamps_removed: boolean;
  captions_merged: boolean;
  punctuation_restored: boolean;
  capitalization_fixed: boolean;
  duplicates_removed: boolean;
  language_detected: boolean;
  fillers_removed: boolean;
  quality_passed: boolean;
}

export interface ProcessedTimestamp {
  original_start: number;
  original_end: number;
  original_text: string;
  cleaned_text: string;
}

export interface ProcessingResult {
  success: boolean;
  video_id: string;
  language: LanguageDistribution | null;
  statistics: ProcessingStatistics;
  clean_transcript: string;
  paragraphs: string[];
  sentences: string[];
  processing_steps: ProcessingStep[];
  timestamps: ProcessedTimestamp[];
  flags: ProcessingFlags;
  processing_time_ms: number;
  error: string | null;
}

export interface WorkflowState {
  currentStep: WorkflowStep;
  videoId: string | null;
  normalizedUrl: string | null;
  metadata: VideoMetadata | null;
  metadataResponse: VideoMetadataResponse | null;
  transcript: TranscriptResult | null;
  translatedTranscripts: Record<string, TranscriptResult>;
  selectedLanguage: string;
  processedTranscript: ProcessingResult | null;
  processingStatus: 'idle' | 'processing' | 'ok' | 'error';
  stepStatus: Record<WorkflowStep, 'pending' | 'running' | 'ok' | 'error' | 'skipped'>;
  error: string | null;
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


