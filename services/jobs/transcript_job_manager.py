"""Transcript Job Manager — asynchronous background execution for channel transcripts.

Manages channel transcript discovery, duration filtering, controlled concurrency,
global request pacing, circuit breaker cooldowns, persistent checkpointing, and resume.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.duration_filter import evaluate_duration, parse_iso_duration
from transcript_reliability.transcript_limiter import transcript_limiter

logger = logging.getLogger(__name__)


def _to_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    import functools
    return loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


def parse_date_boundary(date_str: str | None, is_end_of_day: bool = False) -> datetime | None:
    """Parse date boundary strings (YYYY-MM-DD or ISO 8601) into UTC datetime."""
    if not date_str:
        return None
    s = date_str.strip()
    if not s:
        return None
    if len(s) == 10 and s.count("-") == 2:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            if is_end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        clean_s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        logger.warning("Could not parse date boundary '%s'", date_str)
        return None


def parse_published_at(pub_str: str | None) -> datetime | None:
    """Parse YouTube snippet publishedAt ISO 8601 string into UTC datetime."""
    if not pub_str:
        return None
    try:
        clean_s = pub_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class TranscriptJobManager:
    """Manages long-running channel transcript background jobs with rate-limiting and resume."""

    def __init__(self, jobs_dir: Path | str | None = None) -> None:
        self._jobs: dict[str, TranscriptJobProgress] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._jobs_dir: Path = Path(jobs_dir) if jobs_dir else settings.transcript_jobs_dir
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load_checkpoints()

    def _load_checkpoints(self) -> None:
        """Load existing persisted jobs from disk."""
        try:
            for file_path in self._jobs_dir.glob("*.json"):
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    job = TranscriptJobProgress(**data)
                    self._jobs[job.job_id] = job
                    logger.debug("Loaded transcript job checkpoint %s from %s", job.job_id, file_path.name)
                except Exception as exc:
                    logger.warning("Failed to load transcript job checkpoint from %s: %s", file_path, exc)
        except Exception as exc:
            logger.warning("Failed to scan transcript jobs directory %s: %s", self._jobs_dir, exc)

    def _save_checkpoint(self, job: TranscriptJobProgress) -> None:
        """Persist current job progress to disk atomically."""
        try:
            target = self._jobs_dir / f"{job.job_id}.json"
            job.checkpoint_file = str(target)
            job.updated_at = datetime.now(timezone.utc).isoformat()
            temp = self._jobs_dir / f"{job.job_id}.tmp"
            temp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
            temp.replace(target)
        except Exception as exc:
            logger.warning("Failed to save transcript job checkpoint for %s: %s", job.job_id, exc)

    def get_job(self, job_id: str) -> TranscriptJobProgress | None:
        job = self._jobs.get(job_id)
        if not job:
            # Try disk
            target = self._jobs_dir / f"{job_id}.json"
            if target.exists():
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                    job = TranscriptJobProgress(**data)
                    self._jobs[job_id] = job
                except Exception:
                    pass
        return job

    async def cancel_job(self, job_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(job_id)
            job = self.get_job(job_id)
            if task and not task.done():
                task.cancel()
            if job:
                job.status = JobStatus.CANCELLED
                job.updated_at = datetime.now(timezone.utc).isoformat()
                self._save_checkpoint(job)
                return True
        return False

    async def start_channel_job(
        self,
        channel_handle: str,
        max_videos: int = 0,
        min_duration: int = 180,
        max_duration: int = 1800,
        force_refresh: bool = False,
        caption_concurrency: int | None = None,
        whisper_concurrency: int = 1,
        published_after: str | None = None,
        published_before: str | None = None,
        output_language: str = "en",
    ) -> TranscriptJobProgress:
        """Initialize and launch background transcript job for a YouTube channel."""
        clean_handle = channel_handle.strip().lstrip("@")
        job_id = uuid.uuid4().hex[:12]

        eff_caption_conc = caption_concurrency or settings.transcript_max_concurrency

        progress = TranscriptJobProgress(
            job_id=job_id,
            channel_handle=channel_handle,
            channel_id="",
            channel_title=clean_handle,
            status=JobStatus.QUEUED,
            total_discovered=0,
            eligible_videos=0,
            skipped_videos=0,
            remaining=0,
            published_after=published_after,
            published_before=published_before,
            output_language=output_language or "en",
            videos=[],
        )
        self._jobs[job_id] = progress
        self._save_checkpoint(progress)

        task = asyncio.create_task(
            self._discover_and_run(
                progress=progress,
                clean_handle=clean_handle,
                max_videos=max_videos,
                min_duration=min_duration,
                max_duration=max_duration,
                force_refresh=force_refresh,
                caption_concurrency=eff_caption_conc,
                whisper_concurrency=whisper_concurrency,
                published_after=published_after,
                published_before=published_before,
                output_language=output_language or "en",
            )
        )
        self._tasks[job_id] = task

        logger.info(
            "Queued transcript job %s for channel '%s' (concurrency=%d, pacing=%.1fs, out_lang=%s, pub_after=%s, pub_before=%s)",
            job_id, clean_handle, eff_caption_conc, settings.transcript_request_interval,
            output_language, published_after, published_before,
        )
        return progress

    async def resume_job(self, job_id: str) -> TranscriptJobProgress | None:
        """Resume an existing pending, paused, or rate-limited job from where it left off."""
        job = self.get_job(job_id)
        if not job:
            return None

        async with self._lock:
            existing_task = self._tasks.get(job_id)
            if existing_task and not existing_task.done():
                logger.info("Job %s is already running.", job_id)
                return job

            pending_items = [v for v in job.videos if v.status in ("pending", "rate_limited", "temporary_error")]
            if not pending_items:
                logger.info("Job %s has no pending or rate-limited items to resume.", job_id)
                if job.status not in (JobStatus.COMPLETED, JobStatus.CANCELLED):
                    job.status = JobStatus.COMPLETED
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    self._save_checkpoint(job)
                return job

            # Reset statuses for resumption
            for v in pending_items:
                if v.status == "rate_limited":
                    v.status = "pending"

            job.status = JobStatus.RUNNING
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self._save_checkpoint(job)

            task = asyncio.create_task(
                self._run_job(
                    job,
                    force_refresh=False,
                    caption_concurrency=settings.transcript_max_concurrency,
                    whisper_concurrency=settings.whisper_max_concurrency,
                )
            )
            self._tasks[job_id] = task
            logger.info("Resumed transcript job %s (%d items remaining)", job_id, len(pending_items))
            return job

    async def _discover_and_run(
        self,
        progress: TranscriptJobProgress,
        clean_handle: str,
        max_videos: int,
        min_duration: int,
        max_duration: int,
        force_refresh: bool,
        caption_concurrency: int,
        whisper_concurrency: int,
        published_after: str | None = None,
        published_before: str | None = None,
        output_language: str = "en",
    ) -> None:
        from api.channel_service import ChannelService
        from api.video_service import VideoService

        progress.status = JobStatus.RUNNING
        progress.updated_at = datetime.now(timezone.utc).isoformat()
        self._save_checkpoint(progress)

        try:
            channel_svc = ChannelService()
            video_svc = VideoService()

            channel_data = await _to_thread(channel_svc.resolve_handle, clean_handle)
            channel_id = channel_data["id"]
            channel_title = channel_data["snippet"]["title"]

            progress.channel_id = channel_id
            progress.channel_title = channel_title
            progress.updated_at = datetime.now(timezone.utc).isoformat()

            playlist_id = await _to_thread(video_svc.get_uploads_playlist_id, channel_id)

            pub_after_dt = parse_date_boundary(published_after or progress.published_after, is_end_of_day=False)
            pub_before_dt = parse_date_boundary(published_before or progress.published_before, is_end_of_day=True)

            all_video_ids: list[str] = []
            metadata_map: dict[str, dict] = {}
            next_page: str | None = None
            target_limit = max_videos if max_videos > 0 else 50000

            while len(all_video_ids) < target_limit:
                if progress.status == JobStatus.CANCELLED:
                    return
                page = await _to_thread(video_svc.get_playlist_items, playlist_id, next_page)
                vids = page.get("video_ids", [])
                if not vids:
                    break

                new_vids = [v for v in vids if v and v not in all_video_ids]
                if not new_vids:
                    break

                try:
                    items = await _to_thread(video_svc.get_videos_batch, new_vids)
                    for item in items:
                        vid = item.get("id")
                        if vid:
                            metadata_map[vid] = item
                except Exception as exc:
                    logger.warning("Failed to fetch metadata batch: %s", exc)

                stop_early = False
                for vid in new_vids:
                    all_video_ids.append(vid)
                    # Uploads playlist is reverse chronological. If a video is older than pub_after_dt,
                    # we can stop further playlist pagination early to save quota and network calls.
                    if pub_after_dt:
                        item = metadata_map.get(vid, {})
                        item_pub_str = item.get("snippet", {}).get("publishedAt", "")
                        item_pub_dt = parse_published_at(item_pub_str)
                        if item_pub_dt and item_pub_dt < pub_after_dt:
                            stop_early = True

                    if len(all_video_ids) >= target_limit:
                        stop_early = True
                        break

                if stop_early:
                    logger.info("Discovery reached date cutoff or video limit (%d discovered). Stopping pagination early.", len(all_video_ids))
                    break

                next_page = page.get("next_page_token")
                if not next_page:
                    break

            progress.total_discovered = len(all_video_ids)
            progress.updated_at = datetime.now(timezone.utc).isoformat()

            eligible_items: list[TranscriptVideoItem] = []
            skipped_count = 0

            for vid in all_video_ids:
                item = metadata_map.get(vid, {})
                snippet = item.get("snippet", {})
                cd = item.get("contentDetails", {})
                title = snippet.get("title", "")
                published_at = snippet.get("publishedAt", "")
                duration_iso = cd.get("duration", "PT0S")
                duration_seconds = parse_iso_duration(duration_iso)
                live_status = snippet.get("liveBroadcastContent", "none")

                # Date boundary filtering
                item_pub_dt = parse_published_at(published_at)
                if pub_after_dt and item_pub_dt and item_pub_dt < pub_after_dt:
                    skipped_count += 1
                    continue
                if pub_before_dt and item_pub_dt and item_pub_dt > pub_before_dt:
                    skipped_count += 1
                    continue

                filter_res = evaluate_duration(
                    duration_seconds=duration_seconds,
                    live_status=live_status,
                    min_seconds=min_duration,
                    max_seconds=max_duration,
                )

                if filter_res.is_eligible:
                    eligible_items.append(
                        TranscriptVideoItem(
                            video_id=vid,
                            video_url=f"https://www.youtube.com/watch?v={vid}",
                            channel_id=channel_id,
                            channel_title=channel_title,
                            title=title,
                            published_at=published_at,
                            duration_seconds=filter_res.duration_seconds,
                            duration=filter_res.duration_formatted,
                            status="pending",
                        )
                    )
                else:
                    skipped_count += 1

            progress.eligible_videos = len(eligible_items)
            progress.skipped_videos = skipped_count
            progress.remaining = len(eligible_items)
            progress.videos = eligible_items
            progress.updated_at = datetime.now(timezone.utc).isoformat()
            self._save_checkpoint(progress)

            logger.info(
                "Job %s discovery complete: channel='%s', discovered=%d, eligible=%d",
                progress.job_id, channel_title, len(all_video_ids), len(eligible_items),
            )

            await self._run_job(
                progress,
                force_refresh=force_refresh,
                caption_concurrency=caption_concurrency,
                whisper_concurrency=whisper_concurrency,
            )

        except asyncio.CancelledError:
            progress.status = JobStatus.CANCELLED
            progress.updated_at = datetime.now(timezone.utc).isoformat()
            self._save_checkpoint(progress)
        except Exception as exc:
            logger.exception("Job %s encountered error during discovery: %s", progress.job_id, exc)
            progress.status = JobStatus.FAILED
            progress.error = str(exc)
            progress.updated_at = datetime.now(timezone.utc).isoformat()
            self._save_checkpoint(progress)

    def _recalculate_counters(self, job: TranscriptJobProgress) -> None:
        """Update job progress counters accurately."""
        job.processed = sum(1 for v in job.videos if v.status not in ("pending", "processing"))
        job.remaining = sum(1 for v in job.videos if v.status in ("pending", "processing"))
        job.progress_percent = int((job.processed / max(1, job.eligible_videos)) * 100)

        job.successful = sum(1 for v in job.videos if v.status == "success")
        job.caption_count = sum(1 for v in job.videos if v.status == "success" and v.method == "caption")
        job.whisper_count = sum(1 for v in job.videos if v.status == "success" and v.method == "speech_to_text")
        job.no_captions = sum(
            1 for v in job.videos
            if v.status == "no_captions" or v.error_code in ("NO_CAPTIONS", "CAPTIONS_DISABLED")
        )
        job.rate_limited = sum(
            1 for v in job.videos
            if v.status == "rate_limited" or v.error_code == "RATE_LIMITED"
        )
        job.failed = sum(
            1 for v in job.videos
            if v.status == "failed"
            and v.error_code not in ("NO_CAPTIONS", "CAPTIONS_DISABLED", "RATE_LIMITED")
        )
        job.updated_at = datetime.now(timezone.utc).isoformat()

    async def _apply_output_language(self, job: TranscriptJobProgress, item: TranscriptVideoItem) -> None:
        """Transform acquired transcript to Simple English or requested language with graceful fallback."""
        out_lang = (getattr(job, "output_language", None) or "original").lower().strip()
        item.output_format = out_lang

        # CANONICAL INVARIANT: Original Spoken is 100% lossless source representation
        if out_lang in ("original", "original_spoken"):
            item.transcript = item.raw_transcript
            item.language = item.source_language or item.language or "en"
            return

        if out_lang in ("en", "hi") and item.raw_transcript:
            try:
                from services.translation.service import TranslationService
                trans_svc = TranslationService()
                trans_res = await _to_thread(
                    trans_svc.translate,
                    video_id=item.video_id,
                    original_text=item.raw_transcript,
                    target_language=out_lang,
                    source_language=item.source_language or item.language or "auto",
                )
                if trans_res and trans_res.get("transcript"):
                    transformed = trans_res["transcript"]
                    item.transcript = transformed
                    if out_lang == "en":
                        item.simple_english_transcript = transformed
                        item.language = "Simple English"
                    elif out_lang == "hi":
                        item.simple_hindi_transcript = transformed
                        item.language = "Simple Hindi"
            except Exception as trans_exc:
                logger.warning(
                    "[Job %s] Failed to transform video %s to %s: %s; falling back to raw transcript",
                    job.job_id, item.video_id, out_lang, trans_exc,
                )
                item.transcript = item.raw_transcript

    async def _run_job(
        self,
        job: TranscriptJobProgress,
        force_refresh: bool = False,
        caption_concurrency: int = 1,
        whisper_concurrency: int = 1,
    ) -> None:
        """Execute transcript retrieval with pacing, circuit breaker cooldown, and retry."""
        from services.transcript_service import TranscriptService

        transcript_svc = TranscriptService()
        job.status = JobStatus.RUNNING
        job.updated_at = datetime.now(timezone.utc).isoformat()
        self._save_checkpoint(job)

        consecutive_rate_limits = 0

        for item in job.videos:
            if job.status == JobStatus.CANCELLED:
                break

            # Skip videos that already succeeded or have confirmed no captions
            if item.status == "success" or item.status == "no_captions":
                continue

            item.status = "processing"
            item.last_attempt_at = datetime.now(timezone.utc).isoformat()
            self._recalculate_counters(job)

            # Process with retries under rate limit backoff
            max_item_retries = max(1, settings.transcript_max_rate_limit_retries)
            acquired_success = False

            while item.attempt_count < max_item_retries and not acquired_success:
                item.attempt_count += 1

                # 1. Pacing & Circuit Breaker Check
                await transcript_limiter.acquire(item.video_id)

                # 2. Caption fetch attempt
                try:
                    res = await _to_thread(
                        transcript_svc.get_transcript,
                        video_id=item.video_id,
                        force_refresh=force_refresh,
                        allow_whisper=False,
                        output_format=getattr(job, "output_language", "original"),
                    )
                    if res.success and (res.plain_text or res.paragraph_text or getattr(res, "raw_transcript", None)):
                        item.status = "success"
                        # CANONICAL INVARIANT: raw_transcript is the authentic verbatim source
                        raw_source = getattr(res, "raw_transcript", "") or getattr(res, "plain_text", "") or getattr(res, "paragraph_text", "") or ""
                        if not isinstance(raw_source, str):
                            raw_source = str(raw_source) if raw_source else ""
                        item.raw_transcript = raw_source

                        src_lang = getattr(res, "source_language", None) or getattr(res, "language", None) or "en"
                        if not isinstance(src_lang, str):
                            src_lang = str(src_lang) if src_lang else "en"
                        item.source_language = src_lang

                        src_code = getattr(res, "source_language_code", None) or getattr(res, "language_code", None) or ""
                        if not isinstance(src_code, str):
                            src_code = str(src_code) if src_code else ""
                        item.source_language_code = src_code

                        item.language = src_lang
                        item.output_format = str(getattr(job, "output_language", "original") or "original")
                        item.transcript = raw_source
                        item.source = "youtube"
                        item.method = "caption"
                        item.error_code = None
                        item.error_message = None
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        consecutive_rate_limits = 0
                        acquired_success = True
                        await self._apply_output_language(job, item)
                        break

                    error_code = getattr(res, "error_code", None) or "NO_CAPTIONS"
                    error_msg = getattr(res, "error", "") or "No captions available"

                    # Handle RATE_LIMITED signal
                    if error_code == "RATE_LIMITED":
                        consecutive_rate_limits += 1
                        cooldown = transcript_limiter.record_rate_limit(item.video_id)
                        item.status = "rate_limited"
                        item.error_code = "RATE_LIMITED"
                        item.error_message = "Rate limited by YouTube. System in cooldown."
                        item.retryable = True

                        logger.warning(
                            "[Job %s] Video %s rate-limited. Entering COOLDOWN for %.1fs (attempt %d/%d)",
                            job.job_id, item.video_id, cooldown, item.attempt_count, max_item_retries,
                        )

                        # Update job to COOLDOWN state and countdown
                        job.status = JobStatus.COOLDOWN
                        end_cooldown = time.time() + cooldown
                        while time.time() < end_cooldown:
                            if job.status == JobStatus.CANCELLED:
                                break
                            job.cooldown_seconds_remaining = round(max(0.0, end_cooldown - time.time()), 1)
                            self._recalculate_counters(job)
                            self._save_checkpoint(job)
                            await asyncio.sleep(min(2.0, max(0.5, end_cooldown - time.time())))

                        job.cooldown_seconds_remaining = 0.0
                        job.status = JobStatus.RUNNING

                        # If consecutive rate limits exceed 4, pause job to allow IP to recover
                        if consecutive_rate_limits >= 4:
                            logger.error(
                                "[Job %s] Consecutive rate limits reached threshold (%d). Pausing job.",
                                job.job_id, consecutive_rate_limits,
                            )
                            job.status = JobStatus.PAUSED
                            job.error = "Paused due to persistent YouTube rate limiting. Resume when ready."
                            self._recalculate_counters(job)
                            self._save_checkpoint(job)
                            return

                        # Retry same item after cooldown
                        continue

                    # If captions disabled/not found and whisper enabled, try Whisper STT
                    if error_code in ("NO_CAPTIONS", "CAPTIONS_DISABLED") and settings.whisper_enabled:
                        try:
                            whisper_res = await _to_thread(
                                transcript_svc.get_transcript,
                                video_id=item.video_id,
                                force_refresh=force_refresh,
                                allow_whisper=True,
                            )
                            if whisper_res.success and (whisper_res.plain_text or whisper_res.paragraph_text):
                                item.status = "success"
                                item.transcript = whisper_res.plain_text or whisper_res.paragraph_text or ""
                                item.raw_transcript = getattr(whisper_res, "raw_transcript", "") or item.transcript
                                item.language = whisper_res.language or "Hinglish"
                                item.source = "whisper"
                                item.method = "speech_to_text"
                                item.completed_at = datetime.now(timezone.utc).isoformat()
                                acquired_success = True
                                await self._apply_output_language(job, item)
                                break
                        except Exception as w_exc:
                            logger.warning("Whisper STT fallback failed for %s: %s", item.video_id, w_exc)

                    # Mark non-rate-limited failure
                    if error_code in ("NO_CAPTIONS", "CAPTIONS_DISABLED"):
                        item.status = "no_captions"
                        item.error_code = error_code
                        item.error_message = error_msg
                        item.retryable = False
                    elif error_code in ("VIDEO_UNAVAILABLE", "PRIVATE_VIDEO"):
                        item.status = "failed"
                        item.error_code = error_code
                        item.error_message = error_msg
                        item.retryable = False
                    else:
                        item.status = "failed"
                        item.error_code = error_code
                        item.error_message = error_msg
                        item.retryable = False
                    item.completed_at = datetime.now(timezone.utc).isoformat()
                    break

                except Exception as exc:
                    logger.warning("Exception processing video %s: %s", item.video_id, exc)
                    item.status = "failed"
                    item.error_code = "UNEXPECTED_ERROR"
                    item.error_message = str(exc)
                    item.completed_at = datetime.now(timezone.utc).isoformat()
                    break

            self._recalculate_counters(job)
            self._save_checkpoint(job)

        if job.status not in (JobStatus.CANCELLED, JobStatus.PAUSED):
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc).isoformat()
            job.cooldown_seconds_remaining = 0.0
            self._recalculate_counters(job)
            self._save_checkpoint(job)
            logger.info(
                "Job %s completed: %d/%d success (captions=%d, whisper=%d, no_captions=%d, rate_limited=%d, failed=%d)",
                job.job_id, job.successful, job.eligible_videos,
                job.caption_count, job.whisper_count, job.no_captions, job.rate_limited, job.failed,
            )

    def generate_csv(self, job_id: str, include_audit_columns: bool = False) -> str:
        """Generate compliant CSV for the job with exact representation matching job output mode."""
        job = self.get_job(job_id)
        if not job:
            return ""

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")

        headers = [
            "video_id",
            "video_url",
            "channel_id",
            "channel_title",
            "title",
            "published_at",
            "duration_seconds",
            "duration",
            "language",
            "status",
            "transcript",
            "source",
            "method",
            "error_code",
            "error_message",
        ]
        if include_audit_columns:
            headers.extend(["output_format", "source_language", "source_language_code"])

        writer.writerow(headers)

        out_mode = (getattr(job, "output_language", None) or "original").lower().strip()

        for v in job.videos:
            if out_mode in ("original", "original_spoken"):
                row_transcript = v.raw_transcript or v.transcript
                row_lang = v.source_language or v.language or "en"
                # Fix metadata if old checkpoint marked Hindi transcript as English (India)
                if row_lang == "English (India)" and v.raw_transcript:
                    row_lang = "Hindi"
            elif out_mode == "hi":
                row_transcript = v.simple_hindi_transcript or v.raw_transcript or v.transcript
                row_lang = "Simple Hindi" if (v.simple_hindi_transcript or v.language == "Simple Hindi") else (v.language or "Hindi")
            else:
                row_transcript = v.simple_english_transcript or v.transcript
                row_lang = "Simple English" if (v.simple_english_transcript or v.language == "Simple English") else (v.language or "en")

            row = [
                v.video_id,
                v.video_url,
                v.channel_id,
                v.channel_title,
                v.title,
                v.published_at,
                v.duration_seconds,
                v.duration,
                row_lang or "",
                v.status,
                row_transcript,
                v.source or "",
                v.method or "",
                v.error_code or "",
                v.error_message or "",
            ]
            if include_audit_columns:
                row.extend([
                    out_mode,
                    v.source_language or row_lang,
                    v.source_language_code or ("hi" if row_lang == "Hindi" else "en"),
                ])
            writer.writerow(row)

        return output.getvalue()


# Global Singleton
transcript_job_manager = TranscriptJobManager()
