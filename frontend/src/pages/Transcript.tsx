import { motion, AnimatePresence } from 'framer-motion';
import { useState, useCallback, useRef, useEffect, useMemo } from 'react';
import {
  Youtube, Loader2, ChevronDown, ChevronRight,
  Users, Hash, XCircle, Download, RotateCcw,
  CheckCircle2, AlertCircle, ExternalLink, StopCircle, RefreshCw,
  Copy, Check, Sparkles, Globe, FileText, Languages, Cpu, Clock, Database, Calendar,
} from 'lucide-react';
import { Container, Badge, Card } from '../components/ui';
import VideoUrlInput from '../components/transcript/VideoUrlInput';
import { transcriptService } from '../services/TranscriptService';
import type {
  ChannelVideoTranscriptSimple,
  TranscriptJobProgressData,
  TranscriptSimpleResponse,
  OutputLanguage,
  PipelineStage,
  UnifiedTranscriptResponse,
  UnifiedTranscriptSegment,
} from '../types';

function cleanHandle(input: string): string {
  let h = input.trim();
  if (h.includes('youtube.com/channel/')) h = h.split('/channel/')[1]?.split('/')[0] || h;
  if (h.includes('youtube.com/@')) h = h.split('/@')[1]?.split('/')[0] || h;
  if (h.startsWith('@')) h = h.slice(1);
  return h;
}

export default function Transcript() {
  const [validatedVideoId, setValidatedVideoId] = useState<string | null>(null);
  const [processingVideoId, setProcessingVideoId] = useState<string | null>(null);
  const [currentVideoUrl, setCurrentVideoUrl] = useState<string>('');
  const [videoResult, setVideoResult] = useState<TranscriptSimpleResponse | null>(null);
  const [unifiedResult, setUnifiedResult] = useState<UnifiedTranscriptResponse | null>(null);

  // Single source of truth for selected transcript display mode
  const [selectedMode, setSelectedMode] = useState<OutputLanguage>('en');
  const activeLang = selectedMode;

  // Canonical Original Spoken State (IMMUTABLE once fetched for current video)
  const [canonicalData, setCanonicalData] = useState<{
    transcript: string;
    segments: UnifiedTranscriptSegment[];
    sourceLanguage: string;
    provider: string;
    duration_seconds?: number | null;
    confidence?: number;
    title?: string | null;
  } | null>(null);

  // Multi-Slot Derived Transformation Cache (Keyed by target language: 'en' | 'hi')
  const [transformations, setTransformations] = useState<{
    en?: { transcript: string; segments: UnifiedTranscriptSegment[] };
    hi?: { transcript: string; segments: UnifiedTranscriptSegment[] };
  }>({});

  // Sequence ref for in-flight translation race condition protection
  const requestSeqRef = useRef<number>(0);

  const [isTranslating, setIsTranslating] = useState(false);
  const [viewMode, setViewMode] = useState<'text' | 'segments'>('text');

  // Single Source of Truth for displayed transcript text
  const displayedTranscript = useMemo(() => {
    if (selectedMode === 'original') {
      return canonicalData?.transcript || '';
    }
    return transformations[selectedMode]?.transcript || '';
  }, [selectedMode, canonicalData, transformations]);

  // Single Source of Truth for displayed timestamped segments
  const displayedSegments = useMemo(() => {
    if (selectedMode === 'original') {
      return canonicalData?.segments || [];
    }
    return transformations[selectedMode]?.segments || [];
  }, [selectedMode, canonicalData, transformations]);

  // Pure dynamically derived word count for active display mode
  const displayedWordCount = useMemo(() => {
    if (!displayedTranscript) return 0;
    return displayedTranscript.trim().split(/\s+/).filter(Boolean).length;
  }, [displayedTranscript]);
  const [pipelineStage, setPipelineStage] = useState<PipelineStage>('IDLE');
  const [stageMessage, setStageMessage] = useState<string>('');
  const [structuredError, setStructuredError] = useState<{
    error_code: string;
    message: string;
    retryable: boolean;
  } | null>(null);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [maxVideos, setMaxVideos] = useState(100);
  const [channelVideos, setChannelVideos] = useState<ChannelVideoTranscriptSimple[] | null>(null);
  const [channelLoading, setChannelLoading] = useState(false);
  const [channelError, setChannelError] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<TranscriptJobProgressData | null>(null);
  const [useBackgroundMode, setUseBackgroundMode] = useState(true);
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const channelInputRef = useRef<HTMLInputElement>(null);

  // Channel scraping date filter & Simple English output options
  const [channelDatePreset, setChannelDatePreset] = useState<'all' | '30d' | '3m' | '6m' | '1y' | 'custom'>('all');
  const [publishedAfter, setPublishedAfter] = useState<string>('');
  const [publishedBefore, setPublishedBefore] = useState<string>('');
  const [channelOutputLang, setChannelOutputLang] = useState<'en' | 'original' | 'hi'>('en');
  const [showRawForIdx, setShowRawForIdx] = useState<Record<number, boolean>>({});

  const [csvExporting, setCsvExporting] = useState(false);
  const [csvExportStatus, setCsvExportStatus] = useState<string | null>(null);

  const handleDatePresetChange = (preset: 'all' | '30d' | '3m' | '6m' | '1y' | 'custom') => {
    setChannelDatePreset(preset);
    const now = new Date();
    const fmt = (d: Date) => d.toISOString().split('T')[0];

    if (preset === 'all') {
      setPublishedAfter('');
      setPublishedBefore('');
    } else if (preset === '30d') {
      const past = new Date();
      past.setDate(now.getDate() - 30);
      setPublishedAfter(fmt(past));
      setPublishedBefore(fmt(now));
    } else if (preset === '3m') {
      const past = new Date();
      past.setMonth(now.getMonth() - 3);
      setPublishedAfter(fmt(past));
      setPublishedBefore(fmt(now));
    } else if (preset === '6m') {
      const past = new Date();
      past.setMonth(now.getMonth() - 6);
      setPublishedAfter(fmt(past));
      setPublishedBefore(fmt(now));
    } else if (preset === '1y') {
      const past = new Date();
      past.setFullYear(now.getFullYear() - 1);
      setPublishedAfter(fmt(past));
      setPublishedBefore(fmt(now));
    }
  };

  const handleValidUrl = (videoId: string, normalizedUrl: string) => {
    setValidatedVideoId(videoId);
    if (normalizedUrl) setCurrentVideoUrl(normalizedUrl);
  };

  const fetchSingleTranscript = useCallback(
    async (videoId: string, rawUrl?: string, targetLang: OutputLanguage = 'en') => {
      const url = rawUrl || (currentVideoUrl ? currentVideoUrl : `https://www.youtube.com/watch?v=${videoId}`);
      setCurrentVideoUrl(url);
      setProcessingVideoId(videoId);
      setSelectedMode(targetLang);
      setChannelVideos(null);
      setActiveJob(null);
      setVideoResult(null);
      setUnifiedResult(null);
      setCanonicalData(null);
      setTransformations({});
      setStructuredError(null);
      setError(null);
      setLoading(true);

      const currentSeq = ++requestSeqRef.current;

      setPipelineStage('VALIDATING');
      setStageMessage('Validating YouTube video URL & identifier...');

      const t1 = setTimeout(() => {
        setPipelineStage('CHECKING_CACHE');
        setStageMessage('Checking multi-tier transcript cache...');
      }, 300);

      const t2 = setTimeout(() => {
        setPipelineStage('FETCHING_CAPTIONS');
        setStageMessage('Extracting YouTube transcript & closed captions...');
      }, 700);

      const t3 = setTimeout(() => {
        setPipelineStage('EXTRACTING_AUDIO');
        setStageMessage('Captions unavailable. Transcribing with Groq Whisper Large V3...');
      }, 2000);

      const t4 = setTimeout(() => {
        setPipelineStage('TRANSCRIBING');
        setStageMessage('Polishing and simplifying into clean Simple English...');
      }, 3500);

      try {
        const data = await transcriptService.fetchUnifiedTranscript(url, targetLang);
        clearTimeout(t1);
        clearTimeout(t2);
        clearTimeout(t3);
        clearTimeout(t4);
        setPipelineStage('COMPLETED');
        setStageMessage('Transcript acquisition completed in Simple English!');

        if (requestSeqRef.current !== currentSeq) {
          return;
        }

        // Establish canonical verbatim source (IMMUTABLE)
        const rawText = data.raw_transcript || data.transcript;
        const rawSegs = data.raw_segments && data.raw_segments.length > 0 ? data.raw_segments : data.segments;
        setCanonicalData({
          transcript: rawText,
          segments: rawSegs || [],
          sourceLanguage: data.source_language || 'en',
          provider: data.provider,
          duration_seconds: data.duration_seconds,
          confidence: data.confidence,
          title: data.title,
        });

        // Store transformation cache slot
        if (data.output_language === 'en' || data.output_language === 'hi') {
          setTransformations(prev => ({
            ...prev,
            [data.output_language]: {
              transcript: data.transcript,
              segments: data.segments || [],
            },
          }));
        }

        setUnifiedResult(data);

        // Map to legacy videoResult for compatibility with export tools
        setVideoResult({
          video_id: data.video_id,
          video_url: url,
          title: data.title || 'YouTube Video',
          duration: data.duration_seconds
            ? `${Math.floor(data.duration_seconds / 60)}:${String(Math.floor(data.duration_seconds % 60)).padStart(2, '0')}`
            : '0:00',
          language: data.output_language === 'en' ? 'Simple English' : data.source_language,
          script: 'Standard',
          status: 'success',
          transcript: data.transcript,
          raw_transcript: rawText,
          source: data.provider,
          method: data.provider.includes('whisper') ? 'speech_to_text' : 'caption',
          error_code: null,
          error_message: null,
        });
      } catch (err: any) {
        clearTimeout(t1);
        clearTimeout(t2);
        clearTimeout(t3);
        clearTimeout(t4);
        if (requestSeqRef.current !== currentSeq) {
          return;
        }
        setPipelineStage('FAILED');
        const errCode = err?.error_code || 'TRANSCRIPTION_FAILED';
        const errMsg = err instanceof Error ? err.message : String(err);
        const isRetryable = err?.retryable ?? true;
        setStructuredError({
          error_code: errCode,
          message: errMsg,
          retryable: isRetryable,
        });
        setError(errMsg);
      } finally {
        if (requestSeqRef.current === currentSeq) {
          setLoading(false);
        }
      }
    },
    [currentVideoUrl]
  );

  const handleLanguageChange = async (newLang: OutputLanguage) => {
    if (newLang === selectedMode || !currentVideoUrl) return;

    // 1. Immediately update selectedMode (single source of truth for display & buttons)
    setSelectedMode(newLang);

    // 2. Switching to Original Spoken: canonical verbatim source is already in memory!
    // Instant 0ms switch, zero network calls, zero mutations.
    if (newLang === 'original') {
      return;
    }

    // 3. Switching to Simple English or Simple Hindi: if already cached, instant 0ms switch!
    if (transformations[newLang]?.transcript) {
      return;
    }

    // 4. In-flight fetch needed for new transformation language with race protection
    const currentSeq = ++requestSeqRef.current;
    setIsTranslating(true);
    try {
      const data = await transcriptService.fetchUnifiedTranscript(currentVideoUrl, newLang);

      // Always save to transformation cache
      setTransformations(prev => ({
        ...prev,
        [newLang]: {
          transcript: data.transcript,
          segments: data.segments || [],
        },
      }));

      // Update unifiedResult & legacy videoResult only if request is still current
      if (requestSeqRef.current === currentSeq) {
        setUnifiedResult(data);
        if (videoResult) {
          setVideoResult({
            ...videoResult,
            transcript: data.transcript,
            language: newLang === 'en' ? 'Simple English' : newLang === 'hi' ? 'Simple Hindi' : data.source_language,
          });
        }
      }
    } catch (err: any) {
      if (requestSeqRef.current === currentSeq) {
        console.error('Translation switch failed:', err);
        const errCode = err?.error_code || 'TRANSLATION_FAILED';
        const errMsg = err instanceof Error ? err.message : String(err);
        setStructuredError({
          error_code: errCode,
          message: errMsg,
          retryable: true,
        });
      }
    } finally {
      if (requestSeqRef.current === currentSeq) {
        setIsTranslating(false);
      }
    }
  };

  const handleSingleVideoSubmit = useCallback(
    (videoId: string, normalizedUrl?: string) => {
      console.log('[Transcript] handleSingleVideoSubmit (Simple English default):', videoId, normalizedUrl);
      fetchSingleTranscript(videoId, normalizedUrl, 'en');
    },
    [fetchSingleTranscript]
  );

  const handleRetry = () => {
    const vid = processingVideoId || validatedVideoId;
    if (vid) {
      fetchSingleTranscript(vid, currentVideoUrl, selectedMode);
    }
  };

  const handleCopyTranscript = async () => {
    if (!displayedTranscript) return;
    try {
      await navigator.clipboard.writeText(displayedTranscript);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy transcript:', err);
    }
  };

  // Background Job polling
  useEffect(() => {
    if (!activeJob || activeJob.status === 'completed' || activeJob.status === 'cancelled' || activeJob.status === 'failed') {
      return;
    }

    const interval = setInterval(async () => {
      try {
        const updated = await transcriptService.getJobProgress(activeJob.job_id);
        setActiveJob(updated);
        if (updated.videos && updated.videos.length > 0) {
          setChannelVideos(updated.videos);
        }
        if (updated.status === 'completed' || updated.status === 'cancelled' || updated.status === 'failed' || updated.status === 'paused') {
          setChannelLoading(false);
          if (updated.status !== 'paused') {
            clearInterval(interval);
          }
        }
      } catch (err) {
        console.error('Job polling error:', err);
      }
    }, 1500);

    return () => clearInterval(interval);
  }, [activeJob]);

  const handleChannelSubmit = useCallback(async (handleOverride?: string) => {
    const input = handleOverride || channelInputRef.current?.value || '';
    if (!input.trim()) return;
    const handle = cleanHandle(input);
    setChannelLoading(true);
    setChannelError(null);
    setChannelVideos(null);
    setActiveJob(null);
    setValidatedVideoId(null);
    setVideoResult(null);

    try {
      if (useBackgroundMode) {
        const job = await transcriptService.startTranscriptJob(
          handle,
          maxVideos,
          false,
          publishedAfter || null,
          publishedBefore || null,
          channelOutputLang,
        );
        setActiveJob(job);
        if (job.videos) {
          setChannelVideos(job.videos);
        }
      } else {
        const videos = await transcriptService.fetchChannelTranscriptsSimple(handle, maxVideos);
        setChannelVideos(videos);
        setChannelLoading(false);
      }
    } catch (err) {
      setChannelLoading(false);
      if (err instanceof TypeError) {
        setChannelError('Backend unavailable. Ensure the API server is running on port 8000.');
      } else {
        setChannelError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [maxVideos, useBackgroundMode, publishedAfter, publishedBefore, channelOutputLang]);

  const handleCancelJob = async () => {
    if (!activeJob) return;
    try {
      await transcriptService.cancelJob(activeJob.job_id);
      setActiveJob(prev => prev ? { ...prev, status: 'cancelled' } : null);
      setChannelLoading(false);
    } catch (err) {
      console.error('Cancel job error:', err);
    }
  };

  const handleResumeJob = async () => {
    if (!activeJob) return;
    try {
      setChannelLoading(true);
      const resumed = await transcriptService.resumeJob(activeJob.job_id);
      setActiveJob(resumed);
      if (resumed.videos) {
        setChannelVideos(resumed.videos);
      }
    } catch (err) {
      console.error('Resume job error:', err);
      setChannelLoading(false);
    }
  };

  const handleCsvExport = useCallback(async (channelHandle?: string) => {
    setCsvExporting(true);
    setCsvExportStatus(null);
    try {
      // If we have an active job, download directly from job download endpoint
      if (activeJob) {
        const dlUrl = transcriptService.getJobDownloadUrl(activeJob.job_id);
        const a = document.createElement('a');
        a.href = dlUrl;
        a.download = `${cleanHandle(activeJob.channel_handle)}_transcripts.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setCsvExportStatus('Download started');
        setTimeout(() => setCsvExportStatus(null), 3000);
        return;
      }

      const body: Record<string, unknown> = {
        format: 'csv',
        output_language: selectedMode,
      };
      if (channelHandle) {
        body.channel_handle = channelHandle;
        body.max_videos = maxVideos;
      } else if (processingVideoId || validatedVideoId) {
        const vid = processingVideoId || validatedVideoId;
        body.video_url = `https://youtube.com/watch?v=${vid}`;
      }
      const resp = await fetch('/api/transcript/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const bodyData = await resp.json().catch(() => null);
        throw new Error(bodyData?.message || `Server returned ${resp.status}`);
      }
      const blob = await resp.blob();
      const filename = (resp.headers.get('Content-Disposition') || 'transcripts.csv')
        .split('filename=')[1]
        ?.replace(/"/g, '') || 'transcripts.csv';
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setCsvExportStatus('Download started');
      setTimeout(() => setCsvExportStatus(null), 3000);
    } catch (err) {
      setCsvExportStatus(err instanceof Error ? err.message : 'Export failed');
      setTimeout(() => setCsvExportStatus(null), 5000);
    } finally {
      setCsvExporting(false);
    }
  }, [processingVideoId, validatedVideoId, activeJob, maxVideos]);

  // Statistics calculation
  const totalEligible = channelVideos?.length || 0;
  const captionCount = channelVideos?.filter(v => v.method === 'caption').length || 0;
  const whisperCount = channelVideos?.filter(v => v.method === 'speech_to_text').length || 0;
  const successCount = captionCount + whisperCount;
  const noCaptionsCount = channelVideos?.filter(v => !v.transcript && (v.error_code === 'NO_CAPTIONS' || v.error_code === 'CAPTIONS_DISABLED')).length || 0;
  const failedCount = totalEligible - successCount - noCaptionsCount;

  return (
    <>
      <section className="relative overflow-hidden bg-gradient-to-br from-emerald-600 via-emerald-500 to-teal-600 dark:from-emerald-800 dark:via-emerald-700 dark:to-teal-900">
        <div className="absolute inset-0">
          <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top_left,rgba(255,255,255,0.12),transparent_60%)]" />
          <div className="absolute bottom-0 right-1/4 w-[400px] h-[400px] bg-white/5 rounded-full blur-3xl" />
        </div>
        <div className="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-28 lg:pt-32 pb-12 lg:pb-16">
          <motion.div
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
            className="max-w-2xl"
          >
            <Badge variant="info">Transcript Extraction & STT</Badge>
            <h1 className="text-[36px] sm:text-[44px] lg:text-[52px] font-extrabold leading-[1.08] tracking-[-0.03em] text-white mt-4 mb-3">
              Video or Channel — Transcript Pipeline
            </h1>
            <p className="text-[17px] leading-relaxed text-white/75 max-w-lg">
              Extract official closed captions or transcribe speech using GPU-accelerated Whisper
              with exact duration filtering (3:00 – 30:00).
            </p>
          </motion.div>
        </div>
      </section>

      <section className="relative z-10 -mt-6 pb-20">
        <Container>
          <div className="max-w-4xl mx-auto">
            {/* --- INPUT SECTION --- */}
            <div className={videoResult || channelVideos || validatedVideoId ? 'mb-8' : ''}>
              <Card padding="lg" className="mb-6">
                <div className="flex items-center gap-3 mb-6">
                  <div className="w-10 h-10 rounded-xl bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center flex-shrink-0">
                    <Youtube size={18} className="text-emerald-600 dark:text-emerald-400" />
                  </div>
                  <div>
                    <h3 className="text-base font-semibold text-gray-900 dark:text-white">
                      Single Video Transcript
                    </h3>
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      Enter a YouTube video URL to extract captions or run speech-to-text
                    </p>
                  </div>
                </div>
                <VideoUrlInput
                  onValidUrl={handleValidUrl}
                  onChannelDetected={handleChannelSubmit}
                  onSubmit={handleSingleVideoSubmit}
                  isProcessing={loading}
                />
              </Card>

              <div className="relative mb-6">
                <div className="absolute inset-0 flex items-center">
                  <div className="w-full border-t border-gray-200 dark:border-gray-700" />
                </div>
                <div className="relative flex justify-center text-xs uppercase">
                  <span className="bg-white dark:bg-gray-900 px-3 text-gray-400 dark:text-gray-500 font-medium">
                    or
                  </span>
                </div>
              </div>

              <Card padding="lg">
                <div className="flex items-center gap-3 mb-6">
                  <div className="w-10 h-10 rounded-xl bg-violet-50 dark:bg-violet-900/30 flex items-center justify-center flex-shrink-0">
                    <Users size={18} className="text-violet-600 dark:text-violet-400" />
                  </div>
                  <div>
                    <h3 className="text-base font-semibold text-gray-900 dark:text-white">
                      Channel Transcripts Pipeline
                    </h3>
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      Enter a channel handle (@handle) to process eligible videos (3–30 min)
                    </p>
                  </div>
                </div>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (channelInputRef.current?.value) {
                      handleChannelSubmit();
                    }
                  }}
                >
                  <div className="mb-4">
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
                      Channel Handle or URL
                    </label>
                    <div className="relative">
                      <Users size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-400" />
                      <input
                        ref={channelInputRef}
                        type="text"
                        placeholder="@physicsgalaxyworld or UC... or channel URL"
                        defaultValue=""
                        className="w-full pl-10 pr-4 py-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm transition-all-200 outline-none focus:border-violet-400 focus:ring-4 focus:ring-violet-100 dark:focus:ring-violet-900/30"
                        autoComplete="off"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
                        Max Videos to Discover
                      </label>
                      <div className="relative">
                        <input
                          type="number"
                          value={maxVideos}
                          onChange={(e) => setMaxVideos(Math.max(1, Math.min(1000, parseInt(e.target.value) || 100)))}
                          min={1}
                          max={1000}
                          placeholder="Enter number of videos (1–1000)"
                          className="w-full px-4 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-sm transition-all-200 outline-none focus:border-violet-400 focus:ring-4 focus:ring-violet-100 dark:focus:ring-violet-900/30"
                        />
                        <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-gray-400">1–1000</span>
                      </div>
                    </div>

                    <div className="flex items-center pt-6">
                      <label className="flex items-center gap-2.5 cursor-pointer select-none">
                        <input
                          type="checkbox"
                          checked={useBackgroundMode}
                          onChange={(e) => setUseBackgroundMode(e.target.checked)}
                          className="w-4 h-4 rounded text-violet-600 focus:ring-violet-500 border-gray-300 dark:border-gray-600"
                        />
                        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                          Background Job (Live Progress)
                        </span>
                      </label>
                    </div>
                  </div>

                  {/* --- DATE RANGE FILTER & PRESETS --- */}
                  <div className="mb-4 p-4 rounded-xl bg-gray-50/70 dark:bg-gray-800/40 border border-gray-200/80 dark:border-gray-700/60">
                    <div className="flex items-center justify-between mb-2.5">
                      <label className="flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
                        <Calendar size={15} className="text-violet-600 dark:text-violet-400" />
                        <span>Date Range Filter</span>
                        <span className="text-[11px] font-normal text-gray-500 dark:text-gray-400">
                          (Stops scanning older videos early to save API quota)
                        </span>
                      </label>
                      {channelDatePreset !== 'all' && (
                        <button
                          type="button"
                          onClick={() => handleDatePresetChange('all')}
                          className="text-xs text-violet-600 dark:text-violet-400 hover:underline cursor-pointer"
                        >
                          Clear Date Filter
                        </button>
                      )}
                    </div>

                    {/* Quick Presets */}
                    <div className="flex flex-wrap gap-2 mb-3">
                      {[
                        { id: 'all', label: 'All Time' },
                        { id: '30d', label: 'Last 30 Days' },
                        { id: '3m', label: 'Last 3 Months' },
                        { id: '6m', label: 'Last 6 Months' },
                        { id: '1y', label: 'Last 1 Year' },
                        { id: 'custom', label: 'Custom' },
                      ].map((preset) => (
                        <button
                          key={preset.id}
                          type="button"
                          onClick={() => handleDatePresetChange(preset.id as any)}
                          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer ${
                            channelDatePreset === preset.id
                              ? 'bg-violet-600 text-white shadow-sm'
                              : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700'
                          }`}
                        >
                          {preset.label}
                        </button>
                      ))}
                    </div>

                    {/* Date Pickers */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
                      <div>
                        <span className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                          Published After (From)
                        </span>
                        <input
                          type="date"
                          value={publishedAfter}
                          onChange={(e) => {
                            setPublishedAfter(e.target.value);
                            setChannelDatePreset('custom');
                          }}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs text-gray-800 dark:text-gray-200 outline-none focus:border-violet-500 focus:ring-2 focus:ring-violet-200 dark:focus:ring-violet-900/40"
                        />
                      </div>
                      <div>
                        <span className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                          Published Before (To)
                        </span>
                        <input
                          type="date"
                          value={publishedBefore}
                          onChange={(e) => {
                            setPublishedBefore(e.target.value);
                            setChannelDatePreset('custom');
                          }}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs text-gray-800 dark:text-gray-200 outline-none focus:border-violet-500 focus:ring-2 focus:ring-violet-200 dark:focus:ring-violet-900/40"
                        />
                      </div>
                    </div>
                  </div>

                  {/* --- OUTPUT LANGUAGE SELECTOR --- */}
                  <div className="mb-4 p-4 rounded-xl bg-gray-50/70 dark:bg-gray-800/40 border border-gray-200/80 dark:border-gray-700/60">
                    <label className="flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2.5">
                      <Languages size={15} className="text-violet-600 dark:text-violet-400" />
                      <span>Output Transcript Format</span>
                    </label>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                      <button
                        type="button"
                        onClick={() => setChannelOutputLang('en')}
                        className={`flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-all cursor-pointer ${
                          channelOutputLang === 'en'
                            ? 'bg-violet-600 text-white shadow-sm'
                            : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700'
                        }`}
                      >
                        <Sparkles size={13} className={channelOutputLang === 'en' ? 'text-amber-200' : 'text-amber-500'} />
                        <span className="font-semibold">Simple English</span>
                        <span className="text-[10px] opacity-80">(Default)</span>
                      </button>

                      <button
                        type="button"
                        onClick={() => setChannelOutputLang('original')}
                        className={`flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-all cursor-pointer ${
                          channelOutputLang === 'original'
                            ? 'bg-violet-600 text-white shadow-sm'
                            : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700'
                        }`}
                      >
                        <Globe size={13} />
                        <span>Original Spoken</span>
                      </button>

                      <button
                        type="button"
                        onClick={() => setChannelOutputLang('hi')}
                        className={`flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-all cursor-pointer ${
                          channelOutputLang === 'hi'
                            ? 'bg-violet-600 text-white shadow-sm'
                            : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700'
                        }`}
                      >
                        <FileText size={13} />
                        <span>Simple Hindi</span>
                      </button>
                    </div>
                  </div>

                  {/* --- PRODUCTION BULK SCRAPING SAFEGUARD NOTE --- */}
                  <div className="mb-4 px-3.5 py-2.5 rounded-xl bg-violet-50/70 dark:bg-violet-950/20 border border-violet-200 dark:border-violet-800/40 text-xs text-violet-800 dark:text-violet-300 flex items-center gap-2.5">
                    <Cpu size={16} className="text-violet-600 dark:text-violet-400 flex-shrink-0" />
                    <span>
                      <strong>Bulk Scraping Protected:</strong> 2.5s jittered pacing & automatic circuit breaker cooldown protect your IP. Progress is saved after every video and can be resumed anytime.
                    </span>
                  </div>

                  <button
                    type="submit"
                    disabled={channelLoading}
                    className="w-full inline-flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl bg-gradient-to-r from-violet-600 to-violet-500 text-white font-semibold text-sm shadow-lg shadow-violet-200 dark:shadow-violet-900/30 hover:shadow-xl hover:scale-[1.01] active:scale-[0.99] transition-all-200 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
                  >
                    {channelLoading ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Hash size={16} />
                    )}
                    {channelLoading ? 'Processing Pipeline...' : 'Start Channel Extraction'}
                  </button>
                </form>
              </Card>
            </div>

            {/* --- MULTI-STAGE PRODUCTION STATUS STEPPER --- */}
            {loading && (
              <Card padding="lg" className="mb-6 border-violet-200 dark:border-violet-900/60 bg-gradient-to-br from-violet-50/40 via-white to-purple-50/30 dark:from-gray-900 dark:via-gray-900/80 dark:to-violet-950/20 shadow-lg">
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-violet-600/10 dark:bg-violet-500/20 flex items-center justify-center flex-shrink-0">
                      <Loader2 size={20} className="animate-spin text-violet-600 dark:text-violet-400" />
                    </div>
                    <div>
                      <h4 className="text-sm font-bold text-gray-900 dark:text-white flex items-center gap-2">
                        Acquiring Production Transcript
                        <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
                          {pipelineStage}
                        </span>
                      </h4>
                      <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5 font-medium">
                        {stageMessage || 'Processing YouTube video stream...'}
                      </p>
                    </div>
                  </div>
                </div>

                {/* Visual Stepper */}
                <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 pt-2 border-t border-gray-100 dark:border-gray-800 text-xs">
                  <div className={`p-2 rounded-lg border text-center transition-all ${
                    ['VALIDATING', 'CHECKING_CACHE', 'FETCHING_CAPTIONS', 'EXTRACTING_AUDIO', 'TRANSCRIBING', 'CLEANING', 'COMPLETED'].includes(pipelineStage)
                      ? 'bg-violet-50 dark:bg-violet-950/40 border-violet-300 dark:border-violet-800 text-violet-700 dark:text-violet-300 font-semibold'
                      : 'bg-gray-50 dark:bg-gray-800/40 border-gray-200 dark:border-gray-800 text-gray-400'
                  }`}>
                    <span className="block text-[10px] uppercase font-mono">Step 1</span>
                    <span>Validation</span>
                  </div>

                  <div className={`p-2 rounded-lg border text-center transition-all ${
                    ['CHECKING_CACHE', 'FETCHING_CAPTIONS', 'EXTRACTING_AUDIO', 'TRANSCRIBING', 'CLEANING', 'COMPLETED'].includes(pipelineStage)
                      ? 'bg-violet-50 dark:bg-violet-950/40 border-violet-300 dark:border-violet-800 text-violet-700 dark:text-violet-300 font-semibold'
                      : 'bg-gray-50 dark:bg-gray-800/40 border-gray-200 dark:border-gray-800 text-gray-400'
                  }`}>
                    <span className="block text-[10px] uppercase font-mono">Step 2</span>
                    <span>Cache Check</span>
                  </div>

                  <div className={`p-2 rounded-lg border text-center transition-all ${
                    ['FETCHING_CAPTIONS', 'EXTRACTING_AUDIO', 'TRANSCRIBING', 'CLEANING', 'COMPLETED'].includes(pipelineStage)
                      ? 'bg-emerald-50 dark:bg-emerald-950/40 border-emerald-300 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300 font-semibold'
                      : 'bg-gray-50 dark:bg-gray-800/40 border-gray-200 dark:border-gray-800 text-gray-400'
                  }`}>
                    <span className="block text-[10px] uppercase font-mono">Step 3</span>
                    <span>Captions ($0)</span>
                  </div>

                  <div className={`p-2 rounded-lg border text-center transition-all ${
                    ['EXTRACTING_AUDIO', 'TRANSCRIBING', 'CLEANING', 'COMPLETED'].includes(pipelineStage)
                      ? 'bg-purple-50 dark:bg-purple-950/40 border-purple-300 dark:border-purple-800 text-purple-700 dark:text-purple-300 font-semibold'
                      : 'bg-gray-50 dark:bg-gray-800/40 border-gray-200 dark:border-gray-800 text-gray-400'
                  }`}>
                    <span className="block text-[10px] uppercase font-mono">Step 4</span>
                    <span>Groq STT</span>
                  </div>

                  <div className={`p-2 rounded-lg border text-center transition-all ${
                    ['CLEANING', 'COMPLETED'].includes(pipelineStage)
                      ? 'bg-emerald-50 dark:bg-emerald-950/40 border-emerald-300 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300 font-semibold'
                      : 'bg-gray-50 dark:bg-gray-800/40 border-gray-200 dark:border-gray-800 text-gray-400'
                  }`}>
                    <span className="block text-[10px] uppercase font-mono">Step 5</span>
                    <span>NLP Clean</span>
                  </div>
                </div>
              </Card>
            )}

            {/* --- SINGLE VIDEO ERROR --- */}
            {structuredError && !loading && (
              <Card padding="lg" className="mb-6 border-rose-200 dark:border-rose-900/40 bg-rose-50/30 dark:bg-rose-950/10">
                <div className="flex items-start gap-4">
                  <div className="w-10 h-10 rounded-xl bg-rose-100 dark:bg-rose-900/40 flex items-center justify-center flex-shrink-0">
                    <AlertCircle size={20} className="text-rose-600 dark:text-rose-400" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <h3 className="text-base font-bold text-gray-900 dark:text-white">
                        Transcript Acquisition Failed
                      </h3>
                      <span className="px-2.5 py-0.5 rounded-full text-xs font-bold font-mono bg-rose-100 text-rose-800 dark:bg-rose-950/60 dark:text-rose-300 border border-rose-300 dark:border-rose-800">
                        {structuredError.error_code}
                      </span>
                    </div>
                    <p className="text-sm text-gray-700 dark:text-gray-300 mt-1">
                      {structuredError.message}
                    </p>
                    <div className="mt-4 flex items-center gap-4 text-xs">
                      {currentVideoUrl && (
                        <a
                          href={currentVideoUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 font-semibold text-violet-600 dark:text-violet-400 hover:underline"
                        >
                          Open on YouTube <ExternalLink size={12} />
                        </a>
                      )}
                      {structuredError.retryable && (
                        <button
                          onClick={handleRetry}
                          className="inline-flex items-center gap-1 font-semibold text-violet-600 dark:text-violet-400 hover:underline cursor-pointer"
                        >
                          <RotateCcw size={12} /> Retry Pipeline
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </Card>
            )}

            {/* --- CHANNEL ERROR --- */}
            {channelError && !channelLoading && (
              <Card padding="lg" className="mb-6">
                <div className="flex items-start gap-3">
                  <XCircle size={18} className="text-red-500 flex-shrink-0 mt-0.5" />
                  <div>
                    <Badge variant="error">{channelError}</Badge>
                    <button
                      onClick={() => { setChannelError(null); handleChannelSubmit(); }}
                      className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-violet-600 dark:text-violet-400 hover:underline"
                    >
                      <RotateCcw size={12} /> Retry
                    </button>
                  </div>
                </div>
              </Card>
            )}

            {/* --- ACTIVE BACKGROUND JOB PROGRESS CARD --- */}
            {activeJob && (
              <Card padding="lg" className="mb-6 border-violet-200 dark:border-violet-900/60 shadow-lg">
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <span className="text-xs font-bold uppercase tracking-wider text-violet-600 dark:text-violet-400">
                      Live Job Progress
                    </span>
                    <h3 className="text-lg font-bold text-gray-900 dark:text-white">
                      {activeJob.channel_title} (@{activeJob.channel_handle})
                    </h3>
                  </div>
                  <div className="flex items-center gap-2">
                    {activeJob.status === 'running' && (
                      <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-violet-100 text-violet-800 dark:bg-violet-950/60 dark:text-violet-300 border border-violet-300 dark:border-violet-800">
                        <Loader2 size={12} className="animate-spin" /> Running
                      </span>
                    )}
                    {activeJob.status === 'cooldown' && (
                      <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-orange-100 text-orange-800 dark:bg-orange-950/60 dark:text-orange-300 border border-orange-300 dark:border-orange-800 animate-pulse">
                        <Clock size={12} /> Cooldown ({Math.ceil(activeJob.cooldown_seconds_remaining || 0)}s)
                      </span>
                    )}
                    {activeJob.status === 'paused' && (
                      <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300 border border-amber-300 dark:border-amber-800">
                        <AlertCircle size={12} /> Paused (Rate Limit)
                      </span>
                    )}
                    {activeJob.status === 'completed' && (
                      <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800">
                        <CheckCircle2 size={12} /> Completed
                      </span>
                    )}
                    {activeJob.status === 'cancelled' && (
                      <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                        <StopCircle size={12} /> Cancelled
                      </span>
                    )}
                    {(activeJob.status === 'running' || activeJob.status === 'cooldown') && (
                      <button
                        onClick={handleCancelJob}
                        className="px-3 py-1 rounded-lg text-xs font-semibold bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300 border border-red-200 dark:border-red-800 hover:bg-red-100 cursor-pointer"
                      >
                        Cancel
                      </button>
                    )}
                    {(activeJob.status === 'paused' || activeJob.status === 'cancelled' || (activeJob.status === 'completed' && ((activeJob.rate_limited || 0) > 0 || activeJob.remaining > 0))) && (
                      <button
                        onClick={handleResumeJob}
                        disabled={channelLoading}
                        className="inline-flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-semibold bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm cursor-pointer disabled:opacity-50"
                      >
                        <RefreshCw size={12} className={channelLoading ? "animate-spin" : ""} /> Resume Job
                      </button>
                    )}
                  </div>
                </div>

                {/* Progress bar */}
                <div className="mb-4">
                  <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                    <span>Processed {activeJob.processed} of {activeJob.eligible_videos} eligible videos</span>
                    <span className="font-bold text-gray-900 dark:text-white">{activeJob.progress_percent}%</span>
                  </div>
                  <div className="w-full bg-gray-100 dark:bg-gray-800 h-2.5 rounded-full overflow-hidden">
                    <div
                      className="bg-gradient-to-r from-violet-600 to-emerald-500 h-full transition-all duration-300"
                      style={{ width: `${activeJob.progress_percent}%` }}
                    />
                  </div>
                </div>

                {/* Metric counters */}
                <div className="grid grid-cols-2 sm:grid-cols-7 gap-2 text-center text-xs">
                  <div className="bg-gray-50 dark:bg-gray-800/60 p-2 rounded-lg">
                    <p className="text-gray-400 text-[10px] uppercase">Eligible</p>
                    <p className="text-sm font-bold text-gray-800 dark:text-gray-200">{activeJob.eligible_videos}</p>
                  </div>
                  <div className="bg-emerald-50 dark:bg-emerald-950/40 p-2 rounded-lg border border-emerald-200 dark:border-emerald-800">
                    <p className="text-emerald-600 dark:text-emerald-400 text-[10px] uppercase">Captions</p>
                    <p className="text-sm font-bold text-emerald-700 dark:text-emerald-300">{activeJob.caption_count}</p>
                  </div>
                  <div className="bg-blue-50 dark:bg-blue-950/40 p-2 rounded-lg border border-blue-200 dark:border-blue-800">
                    <p className="text-blue-600 dark:text-blue-400 text-[10px] uppercase">Whisper</p>
                    <p className="text-sm font-bold text-blue-700 dark:text-blue-300">{activeJob.whisper_count}</p>
                  </div>
                  <div className="bg-amber-50 dark:bg-amber-950/40 p-2 rounded-lg border border-amber-200 dark:border-amber-800">
                    <p className="text-amber-600 dark:text-amber-400 text-[10px] uppercase">No Captions</p>
                    <p className="text-sm font-bold text-amber-700 dark:text-amber-300">{activeJob.no_captions}</p>
                  </div>
                  <div className="bg-orange-50 dark:bg-orange-950/40 p-2 rounded-lg border border-orange-200 dark:border-orange-800">
                    <p className="text-orange-600 dark:text-orange-400 text-[10px] uppercase">Rate Limited</p>
                    <p className="text-sm font-bold text-orange-700 dark:text-orange-300">{activeJob.rate_limited || 0}</p>
                  </div>
                  <div className="bg-rose-50 dark:bg-rose-950/40 p-2 rounded-lg border border-rose-200 dark:border-rose-800">
                    <p className="text-rose-600 dark:text-rose-400 text-[10px] uppercase">Failed</p>
                    <p className="text-sm font-bold text-rose-700 dark:text-rose-300">{activeJob.failed}</p>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-800/60 p-2 rounded-lg">
                    <p className="text-gray-400 text-[10px] uppercase">Remaining</p>
                    <p className="text-sm font-bold text-gray-800 dark:text-gray-200">{activeJob.remaining}</p>
                  </div>
                </div>
              </Card>
            )}

            {/* --- SINGLE VIDEO RESULT --- */}
            {(canonicalData || unifiedResult || videoResult) && !channelVideos && (
              <div className="mb-6 space-y-4">
                <Card padding="lg" className="border-gray-200 dark:border-gray-800 shadow-xl">
                  {/* Header */}
                  <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4 mb-4">
                    <div>
                      <div className="flex items-center gap-2 text-xs text-gray-400 uppercase tracking-wider font-semibold">
                        <span>Video Transcript</span>
                        {unifiedResult?.from_cache && (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-cyan-50 dark:bg-cyan-950/50 text-cyan-700 dark:text-cyan-300 border border-cyan-200 dark:border-cyan-800 font-mono text-[10px]">
                            <Database size={10} /> Cache Hit
                          </span>
                        )}
                      </div>
                      <h2 className="text-lg sm:text-xl font-bold text-gray-900 dark:text-white mt-1">
                        {canonicalData?.title || unifiedResult?.title || videoResult?.title || 'YouTube Video'}
                      </h2>
                    </div>

                    {/* Provider Badge */}
                    <div className="flex items-center gap-2 flex-wrap">
                      {(unifiedResult?.provider === 'youtube_captions' || videoResult?.method === 'caption') && (
                        <span className="px-3 py-1 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800 flex items-center gap-1.5">
                          <CheckCircle2 size={12} /> YouTube Captions (Free)
                        </span>
                      )}
                      {(unifiedResult?.provider.includes('whisper') || videoResult?.method === 'speech_to_text') && (
                        <span className="px-3 py-1 rounded-full text-xs font-semibold bg-violet-100 text-violet-800 dark:bg-violet-950/60 dark:text-violet-300 border border-violet-300 dark:border-violet-800 flex items-center gap-1.5">
                          <Cpu size={12} /> Groq Whisper Large V3
                        </span>
                      )}
                      {unifiedResult?.provider === 'cache' && (
                        <span className="px-3 py-1 rounded-full text-xs font-semibold bg-sky-100 text-sky-800 dark:bg-sky-950/60 dark:text-sky-300 border border-sky-300 dark:border-sky-800 flex items-center gap-1.5">
                          <Database size={12} /> Cached Transcript
                        </span>
                      )}
                      {(canonicalData || unifiedResult) && (
                        <span className="px-3 py-1 rounded-full text-xs font-semibold bg-indigo-100 text-indigo-800 dark:bg-indigo-950/60 dark:text-indigo-300 border border-indigo-300 dark:border-indigo-800 flex items-center gap-1.5">
                          <Sparkles size={12} /> {selectedMode === 'en' ? 'Simple English' : selectedMode === 'hi' ? 'Simple Hindi' : 'Original Spoken'}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* 3-Way Language Toggle Toolbar — Simple English is Default */}
                  <div className="bg-gray-100 dark:bg-gray-800/80 p-1.5 rounded-xl flex items-center gap-1 mb-4">
                    <button
                      onClick={() => handleLanguageChange('en')}
                      disabled={isTranslating && selectedMode === 'en'}
                      className={`flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all flex items-center justify-center gap-2 cursor-pointer ${
                        selectedMode === 'en'
                          ? 'bg-white dark:bg-gray-900 text-indigo-600 dark:text-indigo-400 shadow-sm font-bold border border-indigo-200/60 dark:border-indigo-800/60'
                          : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                      }`}
                    >
                      {isTranslating && selectedMode === 'en' ? (
                        <Loader2 size={13} className="animate-spin" />
                      ) : (
                        <Sparkles size={13} className="text-indigo-500" />
                      )}
                      <span>Simple English</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-50 text-indigo-700 dark:bg-indigo-950/80 dark:text-indigo-300 font-semibold border border-indigo-200/50 dark:border-indigo-800/50">
                        Default
                      </span>
                    </button>

                    <button
                      onClick={() => handleLanguageChange('original')}
                      className={`flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all flex items-center justify-center gap-2 cursor-pointer ${
                        selectedMode === 'original'
                          ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-bold'
                          : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                      }`}
                    >
                      <FileText size={13} />
                      <span>Original Spoken ({canonicalData?.sourceLanguage ? canonicalData.sourceLanguage.toUpperCase() : (unifiedResult?.source_language ? unifiedResult.source_language.toUpperCase() : 'RAW')})</span>
                    </button>

                    <button
                      onClick={() => handleLanguageChange('hi')}
                      disabled={isTranslating && selectedMode === 'hi'}
                      className={`flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all flex items-center justify-center gap-2 cursor-pointer ${
                        selectedMode === 'hi'
                          ? 'bg-white dark:bg-gray-900 text-amber-600 dark:text-amber-400 shadow-sm font-bold'
                          : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                      }`}
                    >
                      {isTranslating && selectedMode === 'hi' ? (
                        <Loader2 size={13} className="animate-spin" />
                      ) : (
                        <Languages size={13} />
                      )}
                      <span>Simple Hindi (सरल हिंदी)</span>
                    </button>
                  </div>

                  {/* Metadata Metrics Row */}
                  <div className="flex flex-wrap items-center gap-y-2 gap-x-5 mb-5 text-xs text-gray-600 dark:text-gray-400 border-y border-gray-100 dark:border-gray-800 py-3">
                    <div>
                      <span className="font-semibold text-gray-700 dark:text-gray-300">Words: </span>
                      <span className="font-bold text-gray-900 dark:text-white">
                        {displayedWordCount}
                      </span>
                    </div>
                    {(canonicalData?.duration_seconds || unifiedResult?.duration_seconds) ? (
                      <div>
                        <span className="font-semibold text-gray-700 dark:text-gray-300">Duration: </span>
                        <span className="font-medium text-gray-900 dark:text-white">
                          {Math.floor((canonicalData?.duration_seconds || unifiedResult?.duration_seconds || 0) / 60)}:{String(Math.floor((canonicalData?.duration_seconds || unifiedResult?.duration_seconds || 0) % 60)).padStart(2, '0')}
                        </span>
                      </div>
                    ) : videoResult?.duration ? (
                      <div>
                        <span className="font-semibold text-gray-700 dark:text-gray-300">Duration: </span>
                        <span className="font-medium text-gray-900 dark:text-white">{videoResult.duration}</span>
                      </div>
                    ) : null}
                    <div>
                      <span className="font-semibold text-gray-700 dark:text-gray-300">Confidence: </span>
                      <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                        {Math.round((canonicalData?.confidence || unifiedResult?.confidence || 0.95) * 100)}%
                      </span>
                    </div>
                    {displayedSegments && displayedSegments.length > 0 && (
                      <div className="flex items-center gap-1.5 ml-auto">
                        <span className="font-semibold text-gray-700 dark:text-gray-300">View:</span>
                        <div className="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 p-0.5 bg-gray-50 dark:bg-gray-800">
                          <button
                            onClick={() => setViewMode('text')}
                            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-all cursor-pointer ${
                              viewMode === 'text' ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-xs' : 'text-gray-500'
                            }`}
                          >
                            Full Text
                          </button>
                          <button
                            onClick={() => setViewMode('segments')}
                            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-all cursor-pointer ${
                              viewMode === 'segments' ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-xs' : 'text-gray-500'
                            }`}
                          >
                            Timestamps ({displayedSegments.length})
                          </button>
                        </div>
                      </div>
                    )}
                    {currentVideoUrl && (
                      <a
                        href={currentVideoUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-violet-600 dark:text-violet-400 hover:underline font-medium ml-auto"
                      >
                        Watch on YouTube <ExternalLink size={12} />
                      </a>
                    )}
                  </div>

                  {/* Transcript Body */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs text-gray-400 dark:text-gray-500 uppercase tracking-wider font-semibold">
                        {selectedMode === 'original'
                          ? 'Canonical Original Spoken'
                          : selectedMode === 'en'
                          ? 'Simple English Output'
                          : 'Simple Hindi Output'}
                      </p>
                      <button
                        onClick={handleCopyTranscript}
                        className="inline-flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 transition-all cursor-pointer"
                      >
                        {copied ? (
                          <>
                            <Check size={12} className="text-emerald-500" />
                            <span className="text-emerald-600 dark:text-emerald-400 font-semibold">Copied!</span>
                          </>
                        ) : (
                          <>
                            <Copy size={12} />
                            Copy Transcript
                          </>
                        )}
                      </button>
                    </div>

                    {viewMode === 'segments' && displayedSegments && displayedSegments.length > 0 ? (
                      <div className="max-h-96 overflow-y-auto space-y-2 bg-gray-50 dark:bg-gray-800/50 rounded-xl p-4 border border-gray-100 dark:border-gray-800">
                        {displayedSegments.map((seg: UnifiedTranscriptSegment, idx: number) => (
                          <div key={idx} className="flex items-start gap-3 text-xs">
                            <span className="font-mono text-violet-600 dark:text-violet-400 font-semibold bg-violet-50 dark:bg-violet-950/60 px-2 py-0.5 rounded flex-shrink-0">
                              {Math.floor(seg.start / 60)}:{String(Math.floor(seg.start % 60)).padStart(2, '0')}
                            </span>
                            <p className="text-gray-800 dark:text-gray-200 leading-relaxed font-sans">{seg.text}</p>
                          </div>
                        ))}
                      </div>
                    ) : isTranslating && !displayedTranscript ? (
                      <div className="max-h-96 min-h-48 flex flex-col items-center justify-center bg-gray-50 dark:bg-gray-800/50 rounded-xl p-8 border border-gray-100 dark:border-gray-800 text-center">
                        <Loader2 size={24} className="animate-spin text-indigo-600 dark:text-indigo-400 mb-2" />
                        <p className="text-xs text-gray-500 dark:text-gray-400 font-medium">
                          Simplifying transcript with Groq LLM...
                        </p>
                      </div>
                    ) : (
                      <div className="max-h-96 overflow-y-auto bg-gray-50 dark:bg-gray-800/50 rounded-xl p-4 border border-gray-100 dark:border-gray-800">
                        <p className="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap leading-relaxed font-sans">
                          {displayedTranscript}
                        </p>
                      </div>
                    )}
                  </div>
                </Card>

                {/* Single Video CSV Export */}
                {(processingVideoId || validatedVideoId) && (
                  <Card padding="lg">
                    <div className="flex items-center justify-between">
                      <div>
                        <h4 className="text-sm font-semibold text-gray-900 dark:text-white">
                          Download 15-Column CSV
                        </h4>
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                          Complete row with metadata, status, language, and transcript
                        </p>
                      </div>
                      <button
                        onClick={() => handleCsvExport()}
                        disabled={csvExporting}
                        className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-emerald-50 dark:bg-emerald-900/30 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300 font-semibold text-sm hover:bg-emerald-100 dark:hover:bg-emerald-900/50 transition-all-200 disabled:opacity-50 cursor-pointer"
                      >
                        {csvExporting ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          <Download size={14} />
                        )}
                        {csvExporting ? 'Generating...' : 'Download CSV'}
                      </button>
                    </div>
                    {csvExportStatus && (
                      <p className={`mt-2 text-xs ${csvExportStatus === 'Download started' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500 dark:text-red-400'}`}>
                        {csvExportStatus}
                      </p>
                    )}
                  </Card>
                )}
              </div>
            )}

            {/* --- CHANNEL RESULTS --- */}
            {channelVideos && (
              <div className="space-y-4 mb-6">
                {/* CSV Export Card with accurate counters */}
                <Card padding="lg">
                  <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                    <div>
                      <h4 className="text-sm font-semibold text-gray-900 dark:text-white">
                        Export All Transcripts (15-Column CSV)
                      </h4>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Download CSV for <span className="font-semibold text-gray-900 dark:text-white">{totalEligible}</span> eligible videos{' '}
                        (<span className="text-emerald-600 dark:text-emerald-400 font-medium">{successCount} available</span>
                        {noCaptionsCount > 0 && <span className="text-amber-600 dark:text-amber-400">, {noCaptionsCount} without captions</span>}
                        {failedCount > 0 && <span className="text-rose-600 dark:text-rose-400">, {failedCount} errors</span>})
                      </p>
                    </div>
                    <button
                      onClick={() => handleCsvExport(channelInputRef.current?.value ? cleanHandle(channelInputRef.current.value) : undefined)}
                      disabled={csvExporting}
                      className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-gradient-to-r from-emerald-600 to-emerald-500 text-white font-semibold text-sm shadow-lg shadow-emerald-200 dark:shadow-emerald-900/30 hover:shadow-xl hover:scale-[1.01] active:scale-[0.99] transition-all-200 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
                    >
                      {csvExporting ? (
                        <Loader2 size={15} className="animate-spin" />
                      ) : (
                        <Download size={15} />
                      )}
                      {csvExporting ? 'Generating...' : `Download CSV (${totalEligible} Videos)`}
                    </button>
                  </div>
                  {csvExportStatus && (
                    <p className={`mt-3 text-xs ${csvExportStatus === 'Download started' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500 dark:text-red-400'}`}>
                      {csvExportStatus}
                    </p>
                  )}
                </Card>

                {/* Video List */}
                {channelVideos.length === 0 && (
                  <Card padding="lg">
                    <p className="text-sm text-gray-400 dark:text-gray-500 text-center py-4">
                      No eligible videos (3–30 min) found for this channel.
                    </p>
                  </Card>
                )}

                {channelVideos.map((video, idx) => (
                  <div key={idx} className="rounded-xl overflow-hidden border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
                    <button
                      onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
                      className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/30 transition-all-200"
                    >
                      <div className="flex-1 min-w-0 text-left">
                        <p className="text-sm font-medium text-gray-900 dark:text-white truncate">
                          {video.title || `Video ${idx + 1}`}
                        </p>
                        <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                          Duration: {video.duration}{video.published_at ? ` • Published: ${video.published_at.split('T')[0]}` : ''}
                        </p>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        {video.language === 'Simple English' && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800 flex items-center gap-1">
                            <Sparkles size={10} className="text-emerald-600 dark:text-emerald-400" /> Simple English
                          </span>
                        )}
                        {video.method === 'caption' && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800">
                            Captions
                          </span>
                        )}
                        {video.method === 'speech_to_text' && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-100 text-blue-800 dark:bg-blue-950/60 dark:text-blue-300 border border-blue-300 dark:border-blue-800">
                            Whisper (STT)
                          </span>
                        )}
                        {(!video.transcript && (video.error_code === 'NO_CAPTIONS' || video.error_code === 'CAPTIONS_DISABLED')) && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300 border border-amber-300 dark:border-amber-800">
                            No Captions
                          </span>
                        )}
                        {(!video.transcript && (video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited')) && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-orange-100 text-orange-800 dark:bg-orange-950/60 dark:text-orange-300 border border-orange-300 dark:border-orange-800">
                            {activeJob?.status === 'cooldown' ? 'Rate Limited (Cooldown)' : activeJob?.status === 'paused' ? 'Rate Limited (Paused)' : 'Rate Limited'}
                          </span>
                        )}
                        {video.status === 'processing' && (
                          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-purple-100 text-purple-800 dark:bg-purple-950/60 dark:text-purple-300 border border-purple-300 dark:border-purple-800">
                            <Loader2 size={10} className="animate-spin" /> Processing
                          </span>
                        )}
                        {video.status === 'pending' && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                            Pending
                          </span>
                        )}
                        {(!video.transcript && video.error_code && video.error_code !== 'NO_CAPTIONS' && video.error_code !== 'CAPTIONS_DISABLED' && video.error_code !== 'RATE_LIMITED' && video.status !== 'rate_limited') && (
                          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-rose-100 text-rose-800 dark:bg-rose-950/60 dark:text-rose-300 border border-rose-300 dark:border-rose-800">
                            Failed: {video.error_code}
                          </span>
                        )}
                      </div>
                      {expandedIdx === idx ? (
                        <ChevronDown size={14} className="text-gray-400 flex-shrink-0" />
                      ) : (
                        <ChevronRight size={14} className="text-gray-400 flex-shrink-0" />
                      )}
                    </button>

                    {expandedIdx === idx && (
                      <div className="border-t border-gray-100 dark:border-gray-700 p-4">
                        {video.transcript ? (
                          <>
                            <div className="flex items-center gap-4 mb-3 text-xs text-gray-500 dark:text-gray-400 flex-wrap">
                              <span>Method: <strong className="text-gray-700 dark:text-gray-300">{video.method}</strong></span>
                              <span>Source: <strong className="text-gray-700 dark:text-gray-300">{video.source}</strong></span>
                              {video.language && (
                                <span>Language: {' '}
                                  {video.language === 'Simple English' ? (
                                    <strong className="text-emerald-600 dark:text-emerald-400 font-semibold">Simple English</strong>
                                  ) : video.language.toLowerCase().includes('english') ? (
                                    <strong className="text-blue-600 dark:text-blue-400">English (India)</strong>
                                  ) : video.language.toLowerCase() === 'hinglish' ? (
                                    <strong className="text-purple-600 dark:text-purple-400">Hinglish (Roman)</strong>
                                  ) : (
                                    <strong className="text-gray-700 dark:text-gray-300">{video.language.toUpperCase()}</strong>
                                  )}
                                </span>
                              )}
                              {video.video_url && (
                                <a
                                  href={video.video_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="inline-flex items-center gap-1 text-violet-600 dark:text-violet-400 hover:underline ml-auto"
                                >
                                  Watch on YouTube <ExternalLink size={12} />
                                </a>
                              )}
                            </div>

                            {/* Simple English vs Original Spoken Toggle */}
                            {Boolean(video.raw_transcript) && (() => {
                              const isRawActive = showRawForIdx[idx] !== undefined
                                ? showRawForIdx[idx]
                                : (activeJob?.output_language === 'original');
                              return (
                                <div className="flex items-center gap-2 mb-3">
                                  <button
                                    type="button"
                                    onClick={() => setShowRawForIdx(prev => ({ ...prev, [idx]: false }))}
                                    className={`px-2.5 py-1 rounded-md text-xs font-semibold cursor-pointer transition-all flex items-center gap-1 ${
                                      !isRawActive
                                        ? 'bg-emerald-600 text-white shadow-sm'
                                        : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
                                    }`}
                                  >
                                    <Sparkles size={11} /> Simple English
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => setShowRawForIdx(prev => ({ ...prev, [idx]: true }))}
                                    className={`px-2.5 py-1 rounded-md text-xs font-medium cursor-pointer transition-all flex items-center gap-1 ${
                                      isRawActive
                                        ? 'bg-violet-600 text-white shadow-sm font-semibold'
                                        : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
                                    }`}
                                  >
                                    <FileText size={11} /> Original Spoken
                                  </button>
                                </div>
                              );
                            })()}

                            <div className="max-h-80 overflow-y-auto bg-gray-50 dark:bg-gray-800/50 rounded-xl p-4">
                              <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
                                {((showRawForIdx[idx] !== undefined ? showRawForIdx[idx] : activeJob?.output_language === 'original') && video.raw_transcript)
                                  ? video.raw_transcript
                                  : video.transcript}
                              </p>
                            </div>
                          </>
                        ) : (
                          <div className={`rounded-xl p-4 text-center ${
                            video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited'
                              ? 'bg-orange-50/70 dark:bg-orange-950/20 border border-orange-200 dark:border-orange-900/40'
                              : 'bg-gray-50 dark:bg-gray-800/40'
                          }`}>
                            {video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited' ? (
                              <Clock size={20} className="text-orange-500 mx-auto mb-2" />
                            ) : (
                              <AlertCircle size={20} className="text-amber-500 mx-auto mb-2" />
                            )}
                            <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                              {video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited'
                                ? 'YouTube is temporarily throttling transcript requests (HTTP 429). The system applies backoff pacing and saves your progress.'
                                : (video.error_message || 'Closed captions are not available on YouTube for this video.')}
                            </p>
                            <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                              Reason Code: <code className="text-violet-600 dark:text-violet-400">{video.error_code || (video.status === 'rate_limited' ? 'RATE_LIMITED' : 'NO_CAPTIONS')}</code>
                            </p>
                            {(video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited') && (
                              <p className="text-xs text-orange-600 dark:text-orange-400 mt-2 font-medium">
                                Progress is preserved in disk checkpoints. You can resume this job whenever the cooldown finishes or your IP rate limit clears.
                              </p>
                            )}
                            {video.video_url && (
                              <div className="mt-3">
                                <a
                                  href={video.video_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="inline-flex items-center gap-1 text-xs font-semibold text-violet-600 dark:text-violet-400 hover:underline"
                                >
                                  Open Video on YouTube <ExternalLink size={12} />
                                </a>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </Container>
      </section>
    </>
  );
}
