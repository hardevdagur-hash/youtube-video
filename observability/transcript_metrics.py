"""Transcript & STT Observability Metrics.

Tracks production KPIs:
- Transcript Success Rate (%)
- Caption Success Rate (%)
- Groq Fallback Rate (%)
- Groq Success Rate (%)
- Average Transcription Time & Duration
- Translation requests and cache hit rates
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class TranscriptMetrics:
    total_requests: int = 0
    successful_transcripts: int = 0
    failed_transcripts: int = 0

    caption_attempts: int = 0
    caption_successes: int = 0
    caption_failures: int = 0

    groq_fallbacks: int = 0
    groq_successes: int = 0
    groq_failures: int = 0

    translation_requests_total: int = 0
    translation_requests_en: int = 0
    translation_requests_hi: int = 0
    translation_cache_hits: int = 0

    total_transcription_time_sec: float = 0.0
    total_video_duration_sec: float = 0.0
    total_words_generated: int = 0


class TranscriptMetricsTracker:
    """Thread-safe collector for transcript pipeline KPIs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics = TranscriptMetrics()

    def record_request_start(self) -> None:
        with self._lock:
            self._metrics.total_requests += 1

    def record_caption_result(self, success: bool) -> None:
        with self._lock:
            self._metrics.caption_attempts += 1
            if success:
                self._metrics.caption_successes += 1
            else:
                self._metrics.caption_failures += 1

    def record_groq_fallback(self, success: bool, duration_sec: float = 0.0, video_dur_sec: float = 0.0, words: int = 0) -> None:
        with self._lock:
            self._metrics.groq_fallbacks += 1
            if success:
                self._metrics.groq_successes += 1
                self._metrics.total_transcription_time_sec += duration_sec
                self._metrics.total_video_duration_sec += video_dur_sec
                self._metrics.total_words_generated += words
            else:
                self._metrics.groq_failures += 1

    def record_final_result(self, success: bool) -> None:
        with self._lock:
            if success:
                self._metrics.successful_transcripts += 1
            else:
                self._metrics.failed_transcripts += 1

    def record_translation(self, lang: str, cache_hit: bool) -> None:
        with self._lock:
            self._metrics.translation_requests_total += 1
            if lang == "en":
                self._metrics.translation_requests_en += 1
            elif lang == "hi":
                self._metrics.translation_requests_hi += 1
            if cache_hit:
                self._metrics.translation_cache_hits += 1

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            m = self._metrics
            total_reqs = m.total_requests
            success_rate = (m.successful_transcripts / total_reqs * 100) if total_reqs > 0 else 100.0
            cap_rate = (m.caption_successes / m.caption_attempts * 100) if m.caption_attempts > 0 else 0.0
            fallback_rate = (m.groq_fallbacks / total_reqs * 100) if total_reqs > 0 else 0.0
            groq_success_rate = (m.groq_successes / m.groq_fallbacks * 100) if m.groq_fallbacks > 0 else 100.0
            avg_transcription_time = (
                (m.total_transcription_time_sec / m.groq_successes) if m.groq_successes > 0 else 0.0
            )
            avg_video_duration = (
                (m.total_video_duration_sec / m.groq_successes) if m.groq_successes > 0 else 0.0
            )
            avg_words = (
                (m.total_words_generated / m.groq_successes) if m.groq_successes > 0 else 0.0
            )
            trans_hit_rate = (
                (m.translation_cache_hits / m.translation_requests_total * 100)
                if m.translation_requests_total > 0
                else 0.0
            )

            return {
                "total_transcript_requests": total_reqs,
                "transcript_success_rate_percent": round(success_rate, 2),
                "caption_attempts": m.caption_attempts,
                "caption_success_rate_percent": round(cap_rate, 2),
                "groq_fallback_rate_percent": round(fallback_rate, 2),
                "groq_success_rate_percent": round(groq_success_rate, 2),
                "average_transcription_time_seconds": round(avg_transcription_time, 2),
                "average_video_duration_seconds": round(avg_video_duration, 2),
                "average_transcript_word_count": round(avg_words, 1),
                "translation_requests_total": m.translation_requests_total,
                "translation_cache_hit_rate_percent": round(trans_hit_rate, 2),
            }


# Singleton tracker
transcript_metrics = TranscriptMetricsTracker()
