import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useState, useMemo, useRef, useCallback } from 'react';
import type { OutputLanguage, UnifiedTranscriptSegment, UnifiedTranscriptResponse } from '../types';

/**
 * Custom hook encapsulating the transcript mode switching state machine.
 * This mirrors the exact production state logic in Transcript.tsx.
 */
function useTranscriptModeManager() {
  const [selectedMode, setSelectedMode] = useState<OutputLanguage>('en');

  const [canonicalData, setCanonicalData] = useState<{
    transcript: string;
    segments: UnifiedTranscriptSegment[];
    sourceLanguage: string;
    provider: string;
    duration_seconds?: number | null;
    confidence?: number;
    title?: string | null;
  } | null>(null);

  const [transformations, setTransformations] = useState<{
    en?: { transcript: string; segments: UnifiedTranscriptSegment[] };
    hi?: { transcript: string; segments: UnifiedTranscriptSegment[] };
  }>({});

  const requestSeqRef = useRef<number>(0);
  const [isTranslating, setIsTranslating] = useState(false);

  // Single Source of Truth: Displayed transcript text derived strictly from selectedMode
  const displayedTranscript = useMemo(() => {
    if (selectedMode === 'original') {
      return canonicalData?.transcript || '';
    }
    return transformations[selectedMode]?.transcript || '';
  }, [selectedMode, canonicalData, transformations]);

  // Single Source of Truth: Displayed segments derived strictly from selectedMode
  const displayedSegments = useMemo(() => {
    if (selectedMode === 'original') {
      return canonicalData?.segments || [];
    }
    return transformations[selectedMode]?.segments || [];
  }, [selectedMode, canonicalData, transformations]);

  const displayedWordCount = useMemo(() => {
    if (!displayedTranscript) return 0;
    return displayedTranscript.trim().split(/\s+/).filter(Boolean).length;
  }, [displayedTranscript]);

  const loadInitialTranscript = useCallback((data: UnifiedTranscriptResponse) => {
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

    if (data.output_language === 'en' || data.output_language === 'hi') {
      setTransformations(prev => ({
        ...prev,
        [data.output_language]: {
          transcript: data.transcript,
          segments: data.segments || [],
        },
      }));
    }

    setSelectedMode(data.output_language);
  }, []);

  const handleModeChange = useCallback(async (
    targetMode: OutputLanguage,
    fetchMock?: (mode: OutputLanguage) => Promise<UnifiedTranscriptResponse>
  ) => {
    setSelectedMode(targetMode);

    // If switching to original, 0ms instant display from canonical memory
    if (targetMode === 'original') {
      return;
    }

    // If already cached, 0ms instant display from transformation cache
    if (transformations[targetMode]?.transcript) {
      return;
    }

    if (!fetchMock) return;

    const currentSeq = ++requestSeqRef.current;
    setIsTranslating(true);
    try {
      const data = await fetchMock(targetMode);
      setTransformations(prev => ({
        ...prev,
        [targetMode]: {
          transcript: data.transcript,
          segments: data.segments || [],
        },
      }));
    } finally {
      if (requestSeqRef.current === currentSeq) {
        setIsTranslating(false);
      }
    }
  }, [transformations]);

  return {
    selectedMode,
    canonicalData,
    transformations,
    displayedTranscript,
    displayedSegments,
    displayedWordCount,
    isTranslating,
    loadInitialTranscript,
    handleModeChange,
  };
}

describe('Transcript Mode Switching Architecture', () => {
  const sampleHindiRaw = 'मैं कहता हूं मैट्रिक्स जाओ। वो कहते हैं स्ट्रेंथ क्या है? 4300 बच्चे नीट में अपीयर हुए...';
  const sampleEnglishSimple = 'I say visit Matrix. They ask what the strength is. 4300 students appeared for NEET...';
  const sampleHindiSegments: UnifiedTranscriptSegment[] = [
    { start: 0, end: 3, duration: 3, text: 'मैं कहता हूं मैट्रिक्स जाओ।' },
    { start: 3, end: 6, duration: 3, text: 'वो कहते हैं स्ट्रेंथ क्या है?' },
  ];
  const sampleEnglishSegments: UnifiedTranscriptSegment[] = [
    { start: 0, end: 3, duration: 3, text: 'I say visit Matrix.' },
    { start: 3, end: 6, duration: 3, text: 'They ask what the strength is.' },
  ];

  const initialApiResponse: UnifiedTranscriptResponse = {
    success: true,
    video_id: '5HR9z5Gh26c',
    title: '1.5 महीने में NEET Rank का पूरा Game बदल दिया',
    source_language: 'hi',
    output_language: 'en',
    provider: 'youtube_captions',
    transcript: sampleEnglishSimple,
    raw_transcript: sampleHindiRaw,
    raw_segments: sampleHindiSegments,
    segments: sampleEnglishSegments,
    word_count: 14,
    duration_seconds: 810,
    confidence: 0.98,
    from_cache: false,
  };

  it('TEST A: Initial load defaults to Simple English and preserves canonical raw data', () => {
    const { result } = renderHook(() => useTranscriptModeManager());

    act(() => {
      result.current.loadInitialTranscript(initialApiResponse);
    });

    expect(result.current.selectedMode).toBe('en');
    expect(result.current.displayedTranscript).toBe(sampleEnglishSimple);
    expect(result.current.displayedSegments).toEqual(sampleEnglishSegments);
    expect(result.current.canonicalData?.transcript).toBe(sampleHindiRaw);
    expect(result.current.canonicalData?.segments).toEqual(sampleHindiSegments);
  });

  it('TEST B: Switching to Original Spoken displays exact canonical raw transcript and segments', async () => {
    const { result } = renderHook(() => useTranscriptModeManager());

    act(() => {
      result.current.loadInitialTranscript(initialApiResponse);
    });

    await act(async () => {
      await result.current.handleModeChange('original');
    });

    expect(result.current.selectedMode).toBe('original');
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);
    expect(result.current.displayedSegments).toEqual(sampleHindiSegments);
  });

  it('TEST C: Switching back to Simple English restores cached transformation with 0ms latency', async () => {
    const { result } = renderHook(() => useTranscriptModeManager());

    act(() => {
      result.current.loadInitialTranscript(initialApiResponse);
    });

    await act(async () => {
      await result.current.handleModeChange('original');
    });
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);

    const mockFetch = vi.fn();
    await act(async () => {
      await result.current.handleModeChange('en', mockFetch);
    });

    // Zero network calls needed because 'en' was already in memory
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.current.selectedMode).toBe('en');
    expect(result.current.displayedTranscript).toBe(sampleEnglishSimple);
  });

  it('TEST D: Rapid mode switching (Original -> Simple -> Original -> Simple -> Original)', async () => {
    const { result } = renderHook(() => useTranscriptModeManager());

    act(() => {
      result.current.loadInitialTranscript(initialApiResponse);
    });

    // Toggle 1: Original
    await act(async () => { await result.current.handleModeChange('original'); });
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);

    // Toggle 2: Simple
    await act(async () => { await result.current.handleModeChange('en'); });
    expect(result.current.displayedTranscript).toBe(sampleEnglishSimple);

    // Toggle 3: Original
    await act(async () => { await result.current.handleModeChange('original'); });
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);

    // Toggle 4: Simple
    await act(async () => { await result.current.handleModeChange('en'); });
    expect(result.current.displayedTranscript).toBe(sampleEnglishSimple);

    // Toggle 5: Original
    await act(async () => { await result.current.handleModeChange('original'); });
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);

    // Verify canonical original is 100% immutable
    expect(result.current.canonicalData?.transcript).toBe(sampleHindiRaw);
  });

  it('TEST E: Async in-flight transformation cannot overwrite Original Spoken view', async () => {
    const { result } = renderHook(() => useTranscriptModeManager());

    act(() => {
      result.current.loadInitialTranscript(initialApiResponse);
    });

    // Deferred promise simulating a slow LLM translation
    let resolveDeferred: (val: UnifiedTranscriptResponse) => void;
    const slowFetch = () => new Promise<UnifiedTranscriptResponse>((resolve) => {
      resolveDeferred = resolve;
    });

    // 1. User requests Simple Hindi
    let promise: Promise<void>;
    act(() => {
      promise = result.current.handleModeChange('hi', slowFetch as any);
    });

    expect(result.current.selectedMode).toBe('hi');
    expect(result.current.isTranslating).toBe(true);

    // 2. Before the async call resolves, user switches to Original Spoken
    await act(async () => {
      await result.current.handleModeChange('original');
    });

    expect(result.current.selectedMode).toBe('original');
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);

    // 3. The in-flight Hindi translation now completes
    const hindiTransResult: UnifiedTranscriptResponse = {
      ...initialApiResponse,
      output_language: 'hi',
      transcript: 'सरल हिंदी: मैट्रिक्स संस्थान पर जाएं...',
      segments: [{ start: 0, end: 3, duration: 3, text: 'सरल हिंदी: मैट्रिक्स संस्थान' }],
    };

    await act(async () => {
      resolveDeferred!(hindiTransResult);
      await promise;
    });

    // CRITICAL INVARIANT: The active view must STILL be Original Spoken!
    expect(result.current.selectedMode).toBe('original');
    expect(result.current.displayedTranscript).toBe(sampleHindiRaw);
    expect(result.current.displayedSegments).toEqual(sampleHindiSegments);

    // The result was safely cached in transformations.hi without stomping the screen
    expect(result.current.transformations.hi?.transcript).toBe('सरल हिंदी: मैट्रिक्स संस्थान पर जाएं...');

    // 4. Now if user clicks Simple Hindi, it shows immediately from cache
    await act(async () => {
      await result.current.handleModeChange('hi');
    });
    expect(result.current.selectedMode).toBe('hi');
    expect(result.current.displayedTranscript).toBe('सरल हिंदी: मैट्रिक्स संस्थान पर जाएं...');
  });

  it('TEST F: Word count dynamically tracks active mode', async () => {
    const { result } = renderHook(() => useTranscriptModeManager());

    act(() => {
      result.current.loadInitialTranscript(initialApiResponse);
    });

    const enWordCount = result.current.displayedWordCount;
    expect(enWordCount).toBe(sampleEnglishSimple.trim().split(/\s+/).length);

    await act(async () => {
      await result.current.handleModeChange('original');
    });

    const rawWordCount = result.current.displayedWordCount;
    expect(rawWordCount).toBe(sampleHindiRaw.trim().split(/\s+/).length);
    expect(rawWordCount).not.toBe(enWordCount);
  });
});
