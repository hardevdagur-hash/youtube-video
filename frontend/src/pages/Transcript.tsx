import { motion } from 'framer-motion';
import { useState, useCallback, useRef, useEffect, useMemo } from 'react';
import { Loader2, ChevronRight, Download, RefreshCw, Copy, Check } from 'lucide-react';
import { Container, Card } from '../components/ui';
import VideoUrlInput from '../components/transcript/VideoUrlInput';
import { transcriptService } from '../services/TranscriptService';
import { isJobPollable, startJobPolling } from '../services/jobPolling';
import { useAuth } from '../auth/AuthContext';
import { simpleModeLabel } from '../types';
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

  const { limits } = useAuth();
  const [maxVideos, setMaxVideos] = useState(limits.maxVideosPerJob);
  const [channelVideos, setChannelVideos] = useState<ChannelVideoTranscriptSimple[] | null>(null);
  const [channelLoading, setChannelLoading] = useState(false);
  const [channelError, setChannelError] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<TranscriptJobProgressData | null>(null);
  const [useBackgroundMode, setUseBackgroundMode] = useState(true);
  // Server-enforced cap for the selected mode (background jobs allow more than synchronous runs)
  const videoCap = useBackgroundMode ? limits.maxVideosPerJob : limits.maxVideosSync;
  const effectiveMaxVideos = Math.min(maxVideos, videoCap);
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

        // Establish canonical verbatim source (IMMUTABLE). Never fall back to the
        // translated text: when a translation was returned, only raw_* is the original.
        const isOriginalPayload = data.output_language === 'original';
        const rawText = data.raw_transcript || (isOriginalPayload ? data.transcript : '');
        const rawSegs = data.raw_segments && data.raw_segments.length > 0
          ? data.raw_segments
          : (isOriginalPayload ? data.segments : []);
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

  // Background Job polling: only while the job can still change (queued/running/cooldown).
  // Restarts only when the job or its pollability changes, not on every progress update.
  const activeJobId = activeJob?.job_id;
  const activeJobPollable = isJobPollable(activeJob?.status);
  useEffect(() => {
    if (!activeJobId || !activeJobPollable) {
      return;
    }
    return startJobPolling({
      jobId: activeJobId,
      fetchProgress: id => transcriptService.getJobProgress(id, 'progress'),
      fetchFull: id => transcriptService.getJobProgress(id, 'full'),
      onUpdate: updated => {
        setActiveJob(updated);
        if (updated.videos && updated.videos.length > 0) {
          setChannelVideos(updated.videos);
        }
      },
      onStop: () => setChannelLoading(false),
      onFatalError: err => {
        setChannelLoading(false);
        if ((err as { status?: number })?.status === 404) {
          setChannelError('This job is no longer available.');
        }
      },
    });
  }, [activeJobId, activeJobPollable]);

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
          effectiveMaxVideos,
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
        const videos = await transcriptService.fetchChannelTranscriptsSimple(handle, effectiveMaxVideos, 5, true, channelOutputLang);
        setChannelVideos(videos);
        setChannelLoading(false);
      }
    } catch (err) {
      setChannelLoading(false);
      if (err instanceof TypeError) {
        setChannelError('Cannot reach the server. Check your connection and try again.');
      } else {
        setChannelError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [effectiveMaxVideos, useBackgroundMode, publishedAfter, publishedBefore, channelOutputLang]);

  const handleCancelJob = async () => {
    if (!activeJob) return;
    try {
      await transcriptService.cancelJob(activeJob.job_id);
      setActiveJob(prev => prev ? { ...prev, status: 'cancelled' } : null);
      setChannelLoading(false);
      // Polling showed transcript previews only; load the full results once.
      const full = await transcriptService.getJobProgress(activeJob.job_id, 'full').catch(() => null);
      if (full) {
        setActiveJob(full);
        if (full.videos && full.videos.length > 0) {
          setChannelVideos(full.videos);
        }
      }
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

      // Channel exports follow the channel language selector; single videos follow the viewer mode.
      const body: Record<string, unknown> = {
        format: 'csv',
        output_language: channelHandle ? channelOutputLang : selectedMode,
      };
      if (channelHandle) {
        body.channel_handle = channelHandle;
        body.max_videos = Math.min(maxVideos, limits.maxVideosSync);
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
  }, [processingVideoId, validatedVideoId, activeJob, maxVideos, selectedMode, channelOutputLang, limits.maxVideosSync]);

  // Statistics calculation
  const totalEligible = channelVideos?.length || 0;
  const captionCount = channelVideos?.filter(v => v.method === 'caption').length || 0;
  const whisperCount = channelVideos?.filter(v => v.method === 'speech_to_text').length || 0;
  const successCount = captionCount + whisperCount;
  const noCaptionsCount = channelVideos?.filter(v => !v.transcript && (v.error_code === 'NO_CAPTIONS' || v.error_code === 'CAPTIONS_DISABLED')).length || 0;
  const failedCount = totalEligible - successCount - noCaptionsCount;

  const fieldCls =
    'rounded-xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-900 dark:text-white placeholder:text-gray-400 outline-none transition-all-200 focus:border-blue-600 focus:ring-4 focus:ring-blue-600/15';
  const pillCls = (active: boolean) =>
    `px-3.5 py-1.5 rounded-full text-[13px] transition-colors cursor-pointer ${
      active
        ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
        : 'bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
    }`;
  const tagCls = 'px-2.5 py-0.5 rounded-full text-[11px] font-medium';

  return (
    <>
      <section className="pt-28 sm:pt-36 pb-12 sm:pb-16 text-center px-4">
        <motion.h1
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
          className="text-[44px] sm:text-[64px] lg:text-[80px] font-semibold leading-[1.05] tracking-[-0.035em] text-gray-900 dark:text-white"
        >
          Transcripts.
        </motion.h1>
      </section>

      <section className="pb-24">
        <Container>
          <div className="max-w-3xl mx-auto">
            {/* --- INPUT SECTION --- */}
            <div className={videoResult || channelVideos || validatedVideoId ? 'mb-8' : ''}>
              <Card padding="lg" className="mb-4">
                <h3 className="text-[21px] font-semibold tracking-tight text-gray-900 dark:text-white mb-5">
                  Video
                </h3>
                <VideoUrlInput
                  onValidUrl={handleValidUrl}
                  onChannelDetected={handleChannelSubmit}
                  onSubmit={handleSingleVideoSubmit}
                  isProcessing={loading}
                />
              </Card>

              <Card padding="lg">
                <h3 className="text-[21px] font-semibold tracking-tight text-gray-900 dark:text-white mb-5">
                  Channel
                </h3>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (channelInputRef.current?.value) {
                      handleChannelSubmit();
                    }
                  }}
                >
                  <input
                    ref={channelInputRef}
                    type="text"
                    placeholder="@handle or channel link"
                    aria-label="Channel handle or URL"
                    defaultValue=""
                    className={`${fieldCls} w-full h-12 px-4 text-[15px] mb-6`}
                    autoComplete="off"
                  />

                  <div className="divide-y divide-gray-200 dark:divide-gray-800 border-y border-gray-200 dark:border-gray-800 mb-6">
                    <div className="flex items-center justify-between gap-4 py-3.5">
                      <span className="text-[15px] text-gray-900 dark:text-gray-100">Videos</span>
                      <input
                        type="number"
                        value={maxVideos}
                        onChange={(e) => setMaxVideos(Math.max(1, Math.min(videoCap, parseInt(e.target.value) || videoCap)))}
                        min={1}
                        max={videoCap}
                        aria-label={`Max videos (1–${videoCap})`}
                        className={`${fieldCls} w-24 h-9 px-3 text-sm text-right`}
                      />
                    </div>

                    <label className="flex items-center justify-between gap-4 py-3.5 cursor-pointer select-none">
                      <span className="text-[15px] text-gray-900 dark:text-gray-100">Background job</span>
                      <input
                        type="checkbox"
                        checked={useBackgroundMode}
                        onChange={(e) => setUseBackgroundMode(e.target.checked)}
                        className="sr-only peer"
                      />
                      <span className="relative w-[51px] h-[31px] rounded-full bg-gray-300 dark:bg-gray-700 peer-checked:bg-emerald-400 transition-colors after:absolute after:top-[2px] after:left-[2px] after:w-[27px] after:h-[27px] after:rounded-full after:bg-white after:shadow after:transition-transform peer-checked:after:translate-x-5" />
                    </label>

                    <div className="py-3.5">
                      <div className="flex items-center justify-between gap-4 mb-3">
                        <span className="text-[15px] text-gray-900 dark:text-gray-100">Published</span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {[
                          { id: 'all', label: 'All' },
                          { id: '30d', label: '30D' },
                          { id: '3m', label: '3M' },
                          { id: '6m', label: '6M' },
                          { id: '1y', label: '1Y' },
                          { id: 'custom', label: 'Custom' },
                        ].map((preset) => (
                          <button
                            key={preset.id}
                            type="button"
                            onClick={() => handleDatePresetChange(preset.id as any)}
                            className={pillCls(channelDatePreset === preset.id)}
                          >
                            {preset.label}
                          </button>
                        ))}
                      </div>
                      {channelDatePreset === 'custom' && (
                        <div className="grid grid-cols-2 gap-2 mt-3">
                          <input
                            type="date"
                            value={publishedAfter}
                            aria-label="Published after"
                            onChange={(e) => {
                              setPublishedAfter(e.target.value);
                              setChannelDatePreset('custom');
                            }}
                            className={`${fieldCls} w-full h-10 px-3 text-sm`}
                          />
                          <input
                            type="date"
                            value={publishedBefore}
                            aria-label="Published before"
                            onChange={(e) => {
                              setPublishedBefore(e.target.value);
                              setChannelDatePreset('custom');
                            }}
                            className={`${fieldCls} w-full h-10 px-3 text-sm`}
                          />
                        </div>
                      )}
                    </div>

                    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 py-3.5">
                      <span className="text-[15px] text-gray-900 dark:text-gray-100">Language</span>
                      <div className="flex flex-wrap gap-1.5">
                        <button type="button" onClick={() => setChannelOutputLang('en')} className={pillCls(channelOutputLang === 'en')}>
                          Simple English
                        </button>
                        <button type="button" onClick={() => setChannelOutputLang('original')} className={pillCls(channelOutputLang === 'original')}>
                          Original
                        </button>
                        <button type="button" onClick={() => setChannelOutputLang('hi')} className={pillCls(channelOutputLang === 'hi')}>
                          Simple Hindi
                        </button>
                      </div>
                    </div>
                  </div>

                  <button
                    type="submit"
                    disabled={channelLoading}
                    className="w-full inline-flex items-center justify-center gap-2 h-12 rounded-full bg-blue-600 text-white font-medium text-[15px] hover:bg-blue-700 active:bg-blue-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {channelLoading ? <Loader2 size={16} className="animate-spin" /> : 'Get Transcripts'}
                  </button>
                </form>
              </Card>
            </div>

            {/* --- MULTI-STAGE PRODUCTION STATUS STEPPER --- */}
            {loading && (
              <Card padding="lg" className="mb-4">
                <div className="flex items-center gap-3">
                  <Loader2 size={18} className="animate-spin text-gray-400" />
                  <span className="text-[15px] text-gray-900 dark:text-white">
                    {{
                      VALIDATING: 'Checking link',
                      CHECKING_CACHE: 'Checking link',
                      FETCHING_CAPTIONS: 'Reading captions',
                      EXTRACTING_AUDIO: 'Transcribing',
                      TRANSCRIBING: 'Finishing up',
                    }[pipelineStage as string] || 'Working'}
                  </span>
                </div>
              </Card>
            )}

            {/* --- SINGLE VIDEO ERROR --- */}
            {structuredError && !loading && (
              <Card padding="lg" className="mb-4">
                <h3 className="text-[17px] font-semibold text-gray-900 dark:text-white">
                  Couldn’t get transcript
                </h3>
                <p className="text-sm text-gray-500 mt-1">
                  {structuredError.message}
                </p>
                <div className="mt-4 flex items-center gap-5 text-sm">
                  {structuredError.retryable && (
                    <button
                      onClick={handleRetry}
                      className="inline-flex items-center gap-0.5 text-blue-600 dark:text-blue-400 hover:underline cursor-pointer"
                    >
                      Try again <ChevronRight size={14} />
                    </button>
                  )}
                  {currentVideoUrl && (
                    <a
                      href={currentVideoUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-0.5 text-blue-600 dark:text-blue-400 hover:underline"
                    >
                      YouTube <ChevronRight size={14} />
                    </a>
                  )}
                </div>
              </Card>
            )}

            {/* --- CHANNEL ERROR --- */}
            {channelError && !channelLoading && (
              <Card padding="lg" className="mb-4">
                <p className="text-sm text-gray-900 dark:text-white">{channelError}</p>
                <button
                  onClick={() => { setChannelError(null); handleChannelSubmit(); }}
                  className="mt-3 inline-flex items-center gap-0.5 text-sm text-blue-600 dark:text-blue-400 hover:underline"
                >
                  Try again <ChevronRight size={14} />
                </button>
              </Card>
            )}

            {/* --- ACTIVE BACKGROUND JOB PROGRESS CARD --- */}
            {activeJob && (
              <Card padding="lg" className="mb-4">
                <div className="flex items-start justify-between gap-4 mb-5">
                  <div className="min-w-0">
                    <h3 className="text-[21px] font-semibold tracking-tight text-gray-900 dark:text-white truncate">
                      {activeJob.channel_title}
                    </h3>
                    <p className="text-sm text-gray-500 flex items-center gap-1.5">
                      {activeJob.status === 'running' && <><Loader2 size={12} className="animate-spin" /> Running</>}
                      {activeJob.status === 'cooldown' && <>Cooling down · {Math.ceil(activeJob.cooldown_seconds_remaining || 0)}s</>}
                      {activeJob.status === 'paused' && <>Paused</>}
                      {activeJob.status === 'completed' && <>Done</>}
                      {activeJob.status === 'cancelled' && <>Cancelled</>}
                    </p>
                    {activeJob.error && (activeJob.status === 'paused' || activeJob.status === 'completed') && (
                      <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">{activeJob.error}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-4 flex-shrink-0 text-sm">
                    {(activeJob.status === 'running' || activeJob.status === 'cooldown') && (
                      <button
                        onClick={handleCancelJob}
                        className="text-red-500 hover:underline cursor-pointer"
                      >
                        Cancel
                      </button>
                    )}
                    {(activeJob.status === 'paused' || activeJob.status === 'cancelled' || (activeJob.status === 'completed' && ((activeJob.rate_limited || 0) > 0 || (activeJob.translation_failed || 0) > 0 || activeJob.remaining > 0))) && (
                      <button
                        onClick={handleResumeJob}
                        disabled={channelLoading}
                        className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-full bg-blue-600 text-white hover:bg-blue-700 cursor-pointer disabled:opacity-50"
                      >
                        <RefreshCw size={12} className={channelLoading ? "animate-spin" : ""} /> Resume
                      </button>
                    )}
                  </div>
                </div>

                {/* Progress bar */}
                <div className="mb-6">
                  <div className="flex justify-between text-xs text-gray-500 mb-2">
                    <span>{activeJob.processed} / {activeJob.eligible_videos}</span>
                    <span>{activeJob.progress_percent}%</span>
                  </div>
                  <div className="w-full bg-gray-200 dark:bg-gray-800 h-1 rounded-full overflow-hidden">
                    <div
                      className="bg-blue-600 h-full transition-all duration-300"
                      style={{ width: `${activeJob.progress_percent}%` }}
                    />
                  </div>
                </div>

                {/* Metric counters */}
                <div className="grid grid-cols-3 sm:grid-cols-6 gap-y-4 text-center">
                  {[
                    { label: 'Captions', value: activeJob.caption_count },
                    { label: 'Whisper', value: activeJob.whisper_count },
                    { label: 'No captions', value: activeJob.no_captions },
                    { label: 'Limited', value: activeJob.rate_limited || 0 },
                    { label: 'Failed', value: activeJob.failed },
                    { label: 'Left', value: activeJob.remaining },
                  ].map((m) => (
                    <div key={m.label}>
                      <p className="text-[28px] font-semibold tracking-tight text-gray-900 dark:text-white leading-none">{m.value}</p>
                      <p className="text-xs text-gray-500 mt-1.5">{m.label}</p>
                    </div>
                  ))}
                </div>
              </Card>
            )}

            {/* --- SINGLE VIDEO RESULT --- */}
            {(canonicalData || unifiedResult || videoResult) && !channelVideos && (
              <div className="mb-6 space-y-4">
                <Card padding="lg">
                  {/* Header */}
                  <h2 className="text-[24px] sm:text-[28px] font-semibold tracking-tight leading-tight text-gray-900 dark:text-white mb-2">
                    {canonicalData?.title || unifiedResult?.title || videoResult?.title || 'YouTube Video'}
                  </h2>

                  {/* Metadata */}
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-500 mb-6">
                    <span>{displayedWordCount} words</span>
                    {(canonicalData?.duration_seconds || unifiedResult?.duration_seconds) ? (
                      <span>
                        · {Math.floor((canonicalData?.duration_seconds || unifiedResult?.duration_seconds || 0) / 60)}:{String(Math.floor((canonicalData?.duration_seconds || unifiedResult?.duration_seconds || 0) % 60)).padStart(2, '0')}
                      </span>
                    ) : videoResult?.duration ? (
                      <span>· {videoResult.duration}</span>
                    ) : null}
                    {currentVideoUrl && (
                      <a
                        href={currentVideoUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-0.5 text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        YouTube <ChevronRight size={14} />
                      </a>
                    )}
                  </div>

                  {/* Segmented control: language */}
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
                    <div className="inline-flex bg-gray-100 dark:bg-gray-800 p-0.5 rounded-[9px]">
                      {([
                        { id: 'en', label: 'English' },
                        { id: 'original', label: 'Original' },
                        { id: 'hi', label: 'हिंदी' },
                      ] as { id: OutputLanguage; label: string }[]).map((opt) => (
                        <button
                          key={opt.id}
                          onClick={() => handleLanguageChange(opt.id)}
                          disabled={isTranslating && selectedMode === opt.id}
                          className={`px-4 py-1.5 rounded-[7px] text-[13px] transition-all cursor-pointer inline-flex items-center gap-1.5 ${
                            selectedMode === opt.id
                              ? 'bg-white dark:bg-gray-600 text-gray-900 dark:text-white shadow-sm font-medium'
                              : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'
                          }`}
                        >
                          {isTranslating && selectedMode === opt.id && <Loader2 size={12} className="animate-spin" />}
                          {opt.label}
                        </button>
                      ))}
                    </div>

                    <div className="flex items-center gap-4 text-sm">
                      {displayedSegments && displayedSegments.length > 0 && (
                        <button
                          onClick={() => setViewMode(viewMode === 'text' ? 'segments' : 'text')}
                          className="text-blue-600 dark:text-blue-400 hover:underline cursor-pointer"
                        >
                          {viewMode === 'text' ? 'Timestamps' : 'Text'}
                        </button>
                      )}
                      <button
                        onClick={handleCopyTranscript}
                        className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:underline cursor-pointer"
                      >
                        {copied ? <><Check size={14} /> Copied</> : <><Copy size={14} /> Copy</>}
                      </button>
                    </div>
                  </div>

                  {/* Transcript Body */}
                  {viewMode === 'segments' && displayedSegments && displayedSegments.length > 0 ? (
                    <div className="max-h-[28rem] overflow-y-auto space-y-3 bg-gray-100 dark:bg-gray-800 rounded-xl p-5">
                      {displayedSegments.map((seg: UnifiedTranscriptSegment, idx: number) => (
                        <div key={idx} className="flex items-start gap-4 text-[15px]">
                          <span className="font-mono text-xs text-gray-500 pt-1 w-10 flex-shrink-0">
                            {Math.floor(seg.start / 60)}:{String(Math.floor(seg.start % 60)).padStart(2, '0')}
                          </span>
                          <p className="text-gray-900 dark:text-gray-100 leading-relaxed">{seg.text}</p>
                        </div>
                      ))}
                    </div>
                  ) : isTranslating && !displayedTranscript ? (
                    <div className="min-h-48 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-xl p-8">
                      <Loader2 size={22} className="animate-spin text-gray-400" />
                    </div>
                  ) : (
                    <div className="max-h-[28rem] overflow-y-auto bg-gray-100 dark:bg-gray-800 rounded-xl p-5">
                      <p className="text-[15px] text-gray-900 dark:text-gray-100 whitespace-pre-wrap leading-[1.6]">
                        {displayedTranscript}
                      </p>
                    </div>
                  )}

                  {/* Single Video CSV Export */}
                  {(processingVideoId || validatedVideoId) && (
                    <div className="flex items-center justify-end gap-3 mt-6">
                      {csvExportStatus && (
                        <p className={`text-xs ${csvExportStatus === 'Download started' ? 'text-gray-500' : 'text-red-500'}`}>
                          {csvExportStatus}
                        </p>
                      )}
                      <button
                        onClick={() => handleCsvExport()}
                        disabled={csvExporting}
                        className="inline-flex items-center gap-1.5 px-5 py-2 rounded-full bg-blue-600 text-white text-sm hover:bg-blue-700 transition-colors disabled:opacity-50 cursor-pointer"
                      >
                        {csvExporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                        CSV
                      </button>
                    </div>
                  )}
                </Card>
              </div>
            )}

            {/* --- CHANNEL RESULTS --- */}
            {channelVideos && (
              <div className="space-y-4 mb-6">
                {/* CSV Export Card with accurate counters */}
                <Card padding="lg">
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex gap-8">
                      <div>
                        <p className="text-[28px] font-semibold tracking-tight text-gray-900 dark:text-white leading-none">{successCount}</p>
                        <p className="text-xs text-gray-500 mt-1.5">of {totalEligible} ready</p>
                      </div>
                      {noCaptionsCount + failedCount > 0 && (
                        <div>
                          <p className="text-[28px] font-semibold tracking-tight text-gray-900 dark:text-white leading-none">{noCaptionsCount + failedCount}</p>
                          <p className="text-xs text-gray-500 mt-1.5">missing</p>
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-3">
                      {csvExportStatus && (
                        <p className={`text-xs ${csvExportStatus === 'Download started' ? 'text-gray-500' : 'text-red-500'}`}>
                          {csvExportStatus}
                        </p>
                      )}
                      <button
                        onClick={() => handleCsvExport(channelInputRef.current?.value ? cleanHandle(channelInputRef.current.value) : undefined)}
                        disabled={csvExporting}
                        className="inline-flex items-center gap-1.5 px-5 py-2 rounded-full bg-blue-600 text-white text-sm hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {csvExporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                        CSV
                      </button>
                    </div>
                  </div>
                </Card>

                {/* Video List */}
                {channelVideos.length === 0 && (
                  <Card padding="lg">
                    <p className="text-sm text-gray-500 text-center py-4">
                      No videos found.
                    </p>
                  </Card>
                )}

                {channelVideos.length > 0 && (
                <div className="rounded-2xl overflow-hidden bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-800">
                {channelVideos.map((video, idx) => (
                  <div key={idx}>
                    <button
                      onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
                      className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-gray-50 dark:hover:bg-gray-800/40 transition-all-200"
                    >
                      <div className="flex-1 min-w-0 text-left">
                        <p className="text-[15px] text-gray-900 dark:text-white truncate">
                          {video.title || `Video ${idx + 1}`}
                        </p>
                        <p className="text-xs text-gray-500 mt-0.5">
                          {video.duration}{video.published_at ? ` · ${video.published_at.split('T')[0]}` : ''}
                        </p>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        {video.method === 'caption' && (
                          <span className={`${tagCls} bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300`}>Captions</span>
                        )}
                        {video.method === 'speech_to_text' && (
                          <span className={`${tagCls} bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300`}>Whisper</span>
                        )}
                        {(!video.transcript && (video.error_code === 'NO_CAPTIONS' || video.error_code === 'CAPTIONS_DISABLED')) && (
                          <span className={`${tagCls} bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300`}>No captions</span>
                        )}
                        {(!video.transcript && (video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited')) && (
                          <span className={`${tagCls} bg-orange-50 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300`}>Limited</span>
                        )}
                        {video.status === 'processing' && (
                          <Loader2 size={14} className="animate-spin text-gray-400" />
                        )}
                        {video.status === 'pending' && (
                          <span className={`${tagCls} text-gray-400`}>Pending</span>
                        )}
                        {(!video.transcript && video.error_code && video.error_code !== 'NO_CAPTIONS' && video.error_code !== 'CAPTIONS_DISABLED' && video.error_code !== 'RATE_LIMITED' && video.status !== 'rate_limited') && (
                          <span className={`${tagCls} bg-red-50 text-red-600 dark:bg-red-950/40 dark:text-red-300`}>Failed</span>
                        )}
                      </div>
                      <ChevronRight size={16} className={`text-gray-400 flex-shrink-0 transition-transform-200 ${expandedIdx === idx ? 'rotate-90' : ''}`} />
                    </button>

                    {expandedIdx === idx && (
                      <div className="px-5 pb-5">
                        {video.transcript ? (
                          <>
                            {video.video_url && (
                              <a
                                href={video.video_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-0.5 mb-3 text-sm text-blue-600 dark:text-blue-400 hover:underline"
                              >
                                YouTube <ChevronRight size={14} />
                              </a>
                            )}

                            {/* Simple English / Simple Hindi vs Original Spoken Toggle */}
                            {Boolean(video.raw_transcript) && (() => {
                              const isRawActive = showRawForIdx[idx] !== undefined
                                ? showRawForIdx[idx]
                                : (activeJob?.output_language === 'original');
                              const simpleLabel = simpleModeLabel(activeJob?.output_language ?? channelOutputLang);
                              return (
                                <>
                                <div className="flex items-center gap-1.5 mb-3">
                                  <button
                                    type="button"
                                    onClick={() => setShowRawForIdx(prev => ({ ...prev, [idx]: false }))}
                                    className={pillCls(!isRawActive)}
                                  >
                                    {simpleLabel}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => setShowRawForIdx(prev => ({ ...prev, [idx]: true }))}
                                    className={pillCls(isRawActive)}
                                  >
                                    Original
                                  </button>
                                </div>
                                {!isRawActive && video.translation_error && (
                                  <p role="alert" className="mb-3 text-xs text-amber-600 dark:text-amber-400">
                                    {simpleLabel} unavailable — showing original.
                                  </p>
                                )}
                                </>
                              );
                            })()}

                            <div className="max-h-80 overflow-y-auto bg-gray-100 dark:bg-gray-800 rounded-xl p-5">
                              <p className="text-[15px] text-gray-900 dark:text-gray-100 whitespace-pre-wrap leading-[1.6]">
                                {((showRawForIdx[idx] !== undefined ? showRawForIdx[idx] : activeJob?.output_language === 'original') && video.raw_transcript)
                                  ? video.raw_transcript
                                  : video.transcript}
                              </p>
                            </div>
                          </>
                        ) : (
                          <div className="rounded-xl p-5 text-center bg-gray-100 dark:bg-gray-800">
                            <p className="text-sm text-gray-600 dark:text-gray-300">
                              {video.error_code === 'RATE_LIMITED' || video.status === 'rate_limited'
                                ? 'Rate limited. Resume the job later.'
                                : (video.error_message || 'No captions available.')}
                            </p>
                            {video.video_url && (
                              <a
                                href={video.video_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-0.5 mt-2 text-sm text-blue-600 dark:text-blue-400 hover:underline"
                              >
                                YouTube <ChevronRight size={14} />
                              </a>
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
            )}
          </div>
        </Container>
      </section>
    </>
  );
}
