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
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import settings
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.duration_filter import evaluate_duration, parse_iso_duration
from services.csv_safety import safe_csv_row
from services.public_errors import public_message
from services.transcript_limiter import transcript_limiter

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")


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


class JobLimitError(Exception):
    """Raised when starting or resuming a job would exceed the active-job limits."""

    def __init__(self, scope: str) -> None:
        self.scope = scope  # "user" | "server"
        super().__init__(f"active job limit reached ({scope})")


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
# Statuses that only make sense while a task is running in this process.
RUNNING_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COOLDOWN})
_INTERRUPTED_MESSAGE = "Interrupted by a server restart. Resume to continue."


class TranscriptJobManager:
    """Manages long-running channel transcript background jobs with rate-limiting and resume.

    Lifecycle (single instance; the checkpoint file is the source of truth)::

        queued -> running <-> cooldown -> completed | failed | cancelled
                     |                         (terminal: kept JOB_RETENTION_DAYS, then deleted)
                     +-> paused  (rate-limit pause, shutdown or crash; resumable)

    Memory: ``_jobs`` holds jobs whose task is running (or that callers pinned);
    finished jobs move to a small LRU (``JOB_MEMORY_CACHE_SIZE``) and are otherwise
    read back from disk on demand, so completed transcripts are not held forever.
    """

    def __init__(self, jobs_dir: Path | str | None = None) -> None:
        self._jobs: dict[str, TranscriptJobProgress] = {}
        self._recent: OrderedDict[str, TranscriptJobProgress] = OrderedDict()
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._jobs_dir: Path = Path(jobs_dir) if jobs_dir else settings.transcript_jobs_dir
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ------------------------------------------------------------

    def _iter_checkpoints(self):
        """Yield ``(path, job)`` for every readable checkpoint (unreadable ones are logged)."""
        for file_path in sorted(self._jobs_dir.glob("*.json")):
            if self._job_path(file_path.stem) is None:
                continue
            try:
                job = TranscriptJobProgress(**json.loads(file_path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Skipping unreadable transcript job checkpoint %s: %s", file_path.name, exc)
                continue
            yield file_path, job

    def recover_interrupted_jobs(self) -> int:
        """Mark jobs left queued/running/cooldown by a crash or kill as PAUSED (resumable).

        Called once at startup, before any job is started in this process.
        """
        recovered = 0
        for _, job in self._iter_checkpoints():
            if job.job_id in self._tasks or job.status not in RUNNING_STATUSES:
                continue
            job.status = JobStatus.PAUSED
            job.error = _INTERRUPTED_MESSAGE
            job.cooldown_seconds_remaining = 0.0
            for item in job.videos:
                if item.status == "processing":
                    item.status = "pending"
            self._recalculate_counters(job)
            self._save_checkpoint(job)
            recovered += 1
        if recovered:
            logger.warning("Recovered %d interrupted transcript job(s) as paused", recovered)
        return recovered

    def cleanup_expired_jobs(self, now: datetime | None = None) -> list[str]:
        """Delete checkpoints of jobs not updated for JOB_RETENTION_DAYS (never running jobs).

        Safe to call from a worker thread: it only touches files. Returns the removed ids.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=settings.job_retention_days)
        removed: list[str] = []
        for file_path in self._jobs_dir.glob("*.json"):
            job_id = file_path.stem
            if self._job_path(job_id) is None or job_id in self._jobs or job_id in self._tasks:
                continue
            try:
                if datetime.fromtimestamp(file_path.stat().st_mtime, timezone.utc) >= cutoff:
                    continue  # cheap pre-filter: recently written
                job = TranscriptJobProgress(**json.loads(file_path.read_text(encoding="utf-8")))
                updated = datetime.fromisoformat(job.updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if job.status in RUNNING_STATUSES or updated >= cutoff:
                    continue
                file_path.unlink()
                removed.append(job_id)
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning("Could not evaluate job checkpoint %s for expiry: %s", file_path.name, exc)
        for stale_tmp in self._jobs_dir.glob("*.tmp"):
            try:
                if datetime.fromtimestamp(stale_tmp.stat().st_mtime, timezone.utc) < now - timedelta(hours=1):
                    stale_tmp.unlink()
            except OSError:
                continue
        if removed:
            logger.info(
                "Deleted %d expired transcript job(s) (retention %d days)", len(removed), settings.job_retention_days,
            )
        return removed

    async def run_retention_cleanup(self) -> int:
        """Expire old jobs off the event loop, then drop them from the in-memory LRU."""
        removed = await _to_thread(self.cleanup_expired_jobs)
        for job_id in removed:
            self._recent.pop(job_id, None)
        return len(removed)

    def _remember(self, job: TranscriptJobProgress) -> None:
        """Keep a non-running job in the small LRU used for polling and downloads."""
        if settings.job_memory_cache_size <= 0:
            return
        self._recent[job.job_id] = job
        self._recent.move_to_end(job.job_id)
        while len(self._recent) > settings.job_memory_cache_size:
            self._recent.popitem(last=False)

    def _launch(self, job: TranscriptJobProgress, coro: Coroutine) -> asyncio.Task:
        """Run ``coro`` for ``job``; when it ends the job is released from the active set."""
        self._recent.pop(job.job_id, None)
        self._jobs[job.job_id] = job
        task = asyncio.create_task(coro)
        self._tasks[job.job_id] = task

        def _release(done: asyncio.Task, job_id: str = job.job_id) -> None:
            if self._tasks.get(job_id) is not done:
                return  # superseded by a resume that started a new task
            self._tasks.pop(job_id, None)
            finished = self._jobs.pop(job_id, None)
            if finished is not None:
                self._remember(finished)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Transcript job %s task crashed: %r", job_id, done.exception())

        task.add_done_callback(_release)
        return task

    def _job_path(self, job_id: str, suffix: str = ".json") -> Path | None:
        """Checkpoint path for a job id, or None when the id is not a valid opaque id.

        Job ids are ``uuid4().hex[:12]``; anything else (traversal sequences, separators,
        encoded input) is rejected, and the resolved path must stay inside the jobs dir.
        """
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            return None
        root = self._jobs_dir.resolve()
        path = (root / f"{job_id}{suffix}").resolve()
        return path if path.is_relative_to(root) else None

    def _save_checkpoint(self, job: TranscriptJobProgress) -> None:
        """Persist current job progress to disk atomically."""
        try:
            target = self._job_path(job.job_id)
            temp = self._job_path(job.job_id, ".tmp")
            if target is None or temp is None:
                logger.error("Refusing to checkpoint job with invalid id %r", str(job.job_id)[:32])
                return
            job.checkpoint_file = target.name
            job.updated_at = datetime.now(timezone.utc).isoformat()
            temp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
            temp.replace(target)
        except Exception as exc:
            logger.warning("Failed to save transcript job checkpoint for %s: %s", job.job_id, exc)

    def storage_writable(self) -> bool:
        """Cheap readiness probe: the checkpoint directory exists and is writable."""
        try:
            probe = self._jobs_dir / ".healthcheck"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError as exc:
            logger.error("Transcript job storage %s is not writable: %s", self._jobs_dir, exc)
            return False

    def active_job_count(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.done())

    def _check_capacity(self, owner: str | None) -> None:
        """Enforce MAX_ACTIVE_JOBS (server) and MAX_ACTIVE_JOBS_PER_USER before launching a task.

        Callers must not await between this check and registering the new task, so the
        check-then-start sequence is atomic on the event loop.
        """
        active = [jid for jid, task in self._tasks.items() if not task.done()]
        if len(active) >= settings.max_active_jobs:
            raise JobLimitError("server")
        if owner is not None:
            mine = sum(1 for jid in active if (job := self._jobs.get(jid)) is not None and job.owner == owner)
            if mine >= settings.max_active_jobs_per_user:
                raise JobLimitError("user")

    async def shutdown(self) -> None:
        """Stop running jobs on server shutdown, checkpointing them as PAUSED so they can be resumed."""
        running = {jid: t for jid, t in self._tasks.items() if not t.done()}
        jobs = {jid: self._jobs.get(jid) for jid in running}
        for job_id, task in running.items():
            job = jobs[job_id]
            if job and job.status not in TERMINAL_STATUSES:
                job.status = JobStatus.PAUSED
                job.error = "Interrupted by server shutdown. Resume to continue."
            task.cancel()
        if running:
            await asyncio.gather(*running.values(), return_exceptions=True)
        for job in jobs.values():
            if job is None:
                continue
            if job.error == "Interrupted by server shutdown. Resume to continue.":
                job.status = JobStatus.PAUSED  # the task's CancelledError handler may have set CANCELLED
                for item in job.videos:
                    if item.status == "processing":
                        item.status = "pending"
            self._save_checkpoint(job)
        self._tasks.clear()
        if running:
            logger.info("Paused %d running transcript job(s) for shutdown", len(running))

    def get_job(self, job_id: str) -> TranscriptJobProgress | None:
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        job = self._recent.get(job_id)
        if job is not None:
            self._recent.move_to_end(job_id)
            return job
        # Job IDs are uuid4().hex[:12]; reject anything else before touching the filesystem
        target = self._job_path(job_id)
        if target is None or not target.exists():
            return None
        try:
            job = TranscriptJobProgress(**json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("Unreadable transcript job checkpoint %s: %s", target.name, exc)
            return None
        self._remember(job)
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
        owner: str | None = None,
    ) -> TranscriptJobProgress:
        """Initialize and launch background transcript job for a YouTube channel."""
        self._check_capacity(owner)
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
            owner=owner,
            max_videos=max_videos,
            videos=[],
        )
        self._save_checkpoint(progress)

        self._launch(
            progress,
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
            ),
        )

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

            if not job.videos and job.total_discovered == 0 and job.status not in (
                JobStatus.COMPLETED, JobStatus.CANCELLED,
            ):
                # Discovery never finished (restart, crash or failure): run it again.
                self._check_capacity(job.owner)
                job.status = JobStatus.QUEUED
                job.error = None
                self._save_checkpoint(job)
                self._launch(job, self._discover_and_run(
                    progress=job,
                    clean_handle=job.channel_handle.strip().lstrip("@"),
                    max_videos=job.max_videos,
                    min_duration=180,
                    max_duration=1800,
                    force_refresh=False,
                    caption_concurrency=settings.transcript_max_concurrency,
                    whisper_concurrency=settings.whisper_max_concurrency,
                    published_after=job.published_after,
                    published_before=job.published_before,
                    output_language=job.output_language,
                ))
                logger.info("Restarted discovery for transcript job %s", job_id)
                return job

            # "processing" items were interrupted mid-flight (crash) and are retried too.
            pending_items = [
                v for v in job.videos if v.status in ("pending", "processing", "rate_limited", "temporary_error")
            ]
            if not pending_items:
                logger.info("Job %s has no pending or rate-limited items to resume.", job_id)
                if job.status not in (JobStatus.COMPLETED, JobStatus.CANCELLED):
                    job.status = JobStatus.COMPLETED
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    self._save_checkpoint(job)
                return job

            self._check_capacity(job.owner)

            # Reset statuses and the per-item retry budget: a resumed item gets fresh attempts
            # (otherwise items that used their retries while rate-limited would be skipped).
            for v in pending_items:
                if v.status in ("rate_limited", "processing"):
                    v.status = "pending"
                v.attempt_count = 0

            job.status = JobStatus.RUNNING
            job.error = None
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self._save_checkpoint(job)

            self._launch(job, self._run_job(
                job,
                force_refresh=False,
                caption_concurrency=settings.transcript_max_concurrency,
                whisper_concurrency=settings.whisper_max_concurrency,
            ))
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
            # Client-visible; the exception detail stays in the server log.
            progress.error = "Channel discovery failed. Check the channel handle and try again."
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
                from services.translation.service import get_translation_service
                trans_svc = get_translation_service()
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
                                output_format=getattr(job, "output_language", "original"),
                            )
                            if whisper_res.success and (whisper_res.plain_text or whisper_res.paragraph_text):
                                item.status = "success"
                                item.transcript = whisper_res.plain_text or whisper_res.paragraph_text or ""
                                item.raw_transcript = getattr(whisper_res, "raw_transcript", "") or item.transcript
                                item.language = whisper_res.language or "Hinglish"
                                item.source_language = str(getattr(whisper_res, "source_language", None) or item.language)
                                item.source_language_code = str(getattr(whisper_res, "source_language_code", None) or "")
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
                    logger.warning("[Job %s] Exception processing video %s: %s", job.job_id, item.video_id, exc)
                    item.status = "failed"
                    item.error_code = "UNEXPECTED_ERROR"
                    item.error_message = public_message("UNEXPECTED_ERROR")
                    item.completed_at = datetime.now(timezone.utc).isoformat()
                    break

            if item.status == "processing":
                # Retry budget exhausted without a final outcome: keep it resumable.
                item.status = "rate_limited" if item.error_code == "RATE_LIMITED" else "pending"
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
            writer.writerow(safe_csv_row(row))

        return output.getvalue()


# Global Singleton
transcript_job_manager = TranscriptJobManager()
