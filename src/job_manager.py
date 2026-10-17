import os
import time
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from pydantic import BaseModel

from src.logging_config import logger
from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.channel_config import ChannelRegistry, ChannelConfig
from src.image_downloader import ImageDownloader
from src.image_validator import ImageValidator
from src.drive_sync import DriveSyncManager
from src.gemini_browser import (
    GeminiBrowserAdapter,
    GeminiAuthRequiredError,
    GeminiRateLimitError,
    GeminiPromptRefusedError,
    GeminiSelectorError,
)


class WorkerState(BaseModel):
    is_running: bool = False
    is_paused: bool = False
    current_job_id: Optional[str] = None
    current_task_id: Optional[str] = None
    current_prompt_index: Optional[int] = None
    status_message: str = "Idle"
    error_message: Optional[str] = None


class JobManager:
    """Orchestrates job execution, background worker lifecycle, concurrency prevention,
    retries, and safe status transitions.
    """

    LOCK_FILE = Path("data/automation.lock")

    def __init__(
        self,
        store: ProgressStore,
        channel_registry: ChannelRegistry,
        downloader: ImageDownloader,
        drive_sync: DriveSyncManager,
        browser_adapter: Optional[GeminiBrowserAdapter] = None,
        inter_prompt_delay_seconds: int = 4,
    ):
        self.store = store
        self.channel_registry = channel_registry
        self.downloader = downloader
        self.drive_sync = drive_sync
        self.browser_adapter = browser_adapter or GeminiBrowserAdapter()
        self.inter_prompt_delay = inter_prompt_delay_seconds

        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._state_lock = threading.Lock()
        self.state = WorkerState()

    def _acquire_lock(self) -> bool:
        """Ensures single-instance worker execution to protect browser profile."""
        try:
            self.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            if self.LOCK_FILE.exists():
                # Check if stale lock (> 24 hours old) or currently active
                try:
                    with open(self.LOCK_FILE, "r") as f:
                        pid = f.read().strip()
                    logger.warning(f"Lock file exists with PID/worker {pid}. Attempting cleanup if dead...")
                except Exception:
                    pass
            with open(self.LOCK_FILE, "w") as f:
                f.write(f"pid={os.getpid()};time={time.time()}")
            return True
        except Exception as e:
            logger.error(f"Failed to acquire worker lock: {e}")
            return False

    def _release_lock(self) -> None:
        try:
            if self.LOCK_FILE.exists():
                self.LOCK_FILE.unlink()
        except Exception as e:
            logger.warning(f"Failed to release worker lock: {e}")

    def start_job(self, job_id: str) -> bool:
        """Starts processing a job in a background worker thread."""
        with self._state_lock:
            if self.state.is_running and self.state.is_paused and self.state.current_job_id == job_id:
                logger.info(f"Job {job_id} is currently paused. Resuming worker execution...")
                self._pause_event.clear()
                self.state.is_paused = False
                self.state.status_message = "Resumed job execution."
                self.store.set_job_status(job_id, JobStatus.RUNNING)
                return True

            if self.state.is_running:
                logger.warning(f"Worker is already running job {self.state.current_job_id}.")
                return False

            job = self.store.get_job(job_id)
            if not job:
                logger.error(f"Cannot start non-existent job {job_id}")
                return False

            if not self._acquire_lock():
                self.state.error_message = "Another worker process has acquired the browser lock."
                return False

            self._stop_event.clear()
            self._pause_event.clear()
            self.state = WorkerState(
                is_running=True,
                is_paused=False,
                current_job_id=job_id,
                status_message=f"Starting job {job_id}...",
            )

            self.store.set_job_status(job_id, JobStatus.RUNNING)

            self._worker_thread = threading.Thread(
                target=self._run_job_loop,
                args=(job_id,),
                name=f"Worker-{job_id}",
                daemon=True,
            )
            self._worker_thread.start()
            return True

    def pause_job(self) -> None:
        with self._state_lock:
            if not self.state.is_running:
                return
            self._pause_event.set()
            self.state.is_paused = True
            self.state.status_message = "Pausing after current task..."
            if self.state.current_job_id:
                self.store.set_job_status(self.state.current_job_id, JobStatus.PAUSED)
        logger.info("Worker pause requested.")

    def resume_job(self) -> None:
        with self._state_lock:
            if not self.state.is_paused or not self.state.current_job_id:
                return
            self._pause_event.clear()
            self.state.is_paused = False
            self.state.status_message = "Resuming job..."
            self.store.set_job_status(self.state.current_job_id, JobStatus.RUNNING)
        logger.info("Worker resume requested.")

    def stop_job(self) -> None:
        with self._state_lock:
            if not self.state.is_running:
                return
            self._stop_event.set()
            self.state.status_message = "Stopping job safely..."
            if self.state.current_job_id:
                self.store.set_job_status(self.state.current_job_id, JobStatus.STOPPED)
        logger.info("Worker stop requested.")

    def _run_job_loop(self, job_id: str) -> None:
        """Main background loop processing tasks sequentially."""
        logger.info(f"Worker thread started for job {job_id}.")
        try:
            job = self.store.get_job(job_id)
            channel = self.channel_registry.get(job["channel_id"])
            if not channel:
                raise ValueError(f"Unknown channel ID {job['channel_id']}")

            # Launch and verify browser
            self._update_status("Launching browser session...")
            self.browser_adapter.launch()

            # Verify authentication
            is_auth, auth_msg = self.browser_adapter.check_auth_status()
            if not is_auth:
                logger.warning(f"Browser authentication check: {auth_msg}")
                self._update_status(f"Action Required: {auth_msg}")
                self.pause_job()

            # Handle channel reference image if required
            if channel.requires_reference:
                found_refs, missing_refs = channel.check_references()
                if missing_refs:
                    err = f"Channel requires reference image but configured file is missing: {missing_refs[0]}"
                    logger.error(err)
                    self.store.set_job_status(job_id, JobStatus.FAILED)
                    self._update_status(f"Error: {err}")
                    return
                if found_refs:
                    self._update_status(f"Attaching channel reference: {found_refs[0].name}...")
                    self.browser_adapter.attach_reference_image(found_refs[0])

            # Process tasks
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    self._update_status("Worker is paused.")
                    time.sleep(1)
                    continue

                task = self.store.get_next_runnable_task(job_id)
                if not task:
                    # No more runnable tasks
                    counts = self.store.refresh_job_counts(job_id)
                    final_status = JobStatus.COMPLETED if counts["failed"] == 0 else JobStatus.FAILED
                    self.store.set_job_status(job_id, final_status)
                    self._update_status(f"Job finished: {counts['completed']} completed, {counts['failed']} failed.")
                    break

                task_id = task["id"]
                prompt_index = task["prompt_index"]
                final_prompt = task["final_prompt"]

                with self._state_lock:
                    self.state.current_task_id = task_id
                    self.state.current_prompt_index = prompt_index
                    self.state.status_message = f"Processing task #{prompt_index}..."

                success = self._execute_task_pipeline(task, channel, job)
                if not success:
                    logger.warning(f"Task #{prompt_index} did not complete successfully.")

                self.store.refresh_job_counts(job_id)

                # Delay between generations
                time.sleep(self.inter_prompt_delay)

        except GeminiRateLimitError as rle:
            logger.error(f"Rate limit encountered: {rle}")
            self.store.set_job_status(job_id, JobStatus.PAUSED)
            self._update_status(f"Job paused due to rate limit: {rle}")

        except GeminiAuthRequiredError as are:
            logger.error(f"Authentication required: {are}")
            self.store.set_job_status(job_id, JobStatus.PAUSED)
            self._update_status(f"Job paused: Please sign in to Google/Gemini in the open browser window.")

        except Exception as e:
            logger.exception(f"Unexpected worker failure: {e}")
            self.store.set_job_status(job_id, JobStatus.FAILED)
            self._update_status(f"Fatal worker error: {e}")

        finally:
            self._release_lock()
            with self._state_lock:
                self.state.is_running = False
                self.state.current_task_id = None
                self.state.current_prompt_index = None
            logger.info(f"Worker loop exited for job {job_id}.")

    def _execute_task_pipeline(self, task: Dict[str, Any], channel: ChannelConfig, job: Dict[str, Any]) -> bool:
        """Executes full pipeline for a single prompt task with explicit state transitions."""
        task_id = task["id"]
        prompt_index = task["prompt_index"]
        prompt_text = task["final_prompt"]
        video_title = job.get("video_title", "Video")

        try:
            # Step 1: SUBMITTING
            self.store.set_task_status(task_id, TaskStatus.SUBMITTING, expected_status=task["status"])
            self.browser_adapter.submit_prompt(prompt_text)

            # Step 2: GENERATING
            self.store.set_task_status(task_id, TaskStatus.GENERATING, expected_status=TaskStatus.SUBMITTING)
            image_bytes = self.browser_adapter.wait_for_image_completion(timeout_seconds=120)

            # Step 3: DOWNLOADING & VALIDATING
            self.store.set_task_status(task_id, TaskStatus.DOWNLOADING, expected_status=TaskStatus.GENERATING)
            download_result = self.downloader.save_image_bytes(
                image_bytes=image_bytes,
                channel_subfolder=channel.output_subfolder,
                video_identifier=video_title,
                filename_pattern=channel.filename_pattern,
                index=prompt_index,
            )

            if not download_result.success or not download_result.validation:
                raise ValueError(f"Download or validation failed: {download_result.error_message}")

            self.store.set_task_status(task_id, TaskStatus.VALIDATING, expected_status=TaskStatus.DOWNLOADING)
            final_local_file = Path(download_result.final_path)

            # Step 4: Storage sync (Local, Drive for desktop, or Drive API fallback)
            sync_ok, sync_status_label, drive_file_id, sync_err = self.drive_sync.sync_to_destination(
                source_file=final_local_file,
                channel_subfolder=channel.output_subfolder,
                video_identifier=video_title,
            )

            # Step 5: Mark COMPLETED
            self.store.record_task_success(
                task_id=task_id,
                val=download_result.validation,
                storage_mode=self.drive_sync.mode,
                cloud_sync_status=sync_status_label,
            )
            logger.info(f"Task #{prompt_index} completed successfully.")
            return True

        except (GeminiRateLimitError, GeminiAuthRequiredError):
            # Re-raise to pause the entire job
            raise

        except GeminiPromptRefusedError as gpre:
            logger.warning(f"Task #{prompt_index} refused by Gemini: {gpre}")
            self.store.record_task_failure(
                task_id=task_id,
                error_message=str(gpre),
                error_type="POLICY_REFUSAL",
                can_retry=False,
            )
            return False

        except Exception as e:
            logger.error(f"Task #{prompt_index} failed: {e}")
            self.store.record_task_failure(
                task_id=task_id,
                error_message=str(e),
                error_type="PIPELINE_ERROR",
                can_retry=True,
            )
            return False

    def _update_status(self, msg: str) -> None:
        with self._state_lock:
            self.state.status_message = msg
        logger.info(f"[JobManager] {msg}")
