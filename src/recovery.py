from pathlib import Path
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

from src.logging_config import logger
from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.image_validator import ImageValidator


class ReconciliationReport(BaseModel):
    job_id: str
    total_tasks: int = 0
    verified_on_disk: int = 0
    promoted_to_completed: int = 0
    corrupted_or_missing: List[str] = Field(default_factory=list)
    interrupted_reset: List[str] = Field(default_factory=list)
    flagged_for_attention: List[str] = Field(default_factory=list)
    safe_to_run: int = 0
    summary_message: str = ""


class RecoveryManager:
    """Handles startup recovery, crash state reconciliation, and file integrity audits."""

    def __init__(self, store: ProgressStore):
        self.store = store

    def reconcile_job(self, job_id: str) -> ReconciliationReport:
        """Audits all tasks for a job against actual disk storage and reconciles state safely."""
        report = ReconciliationReport(job_id=job_id)
        job = self.store.get_job(job_id)
        if not job:
            report.summary_message = f"Job '{job_id}' not found."
            return report

        tasks = self.store.get_tasks_for_job(job_id)
        report.total_tasks = len(tasks)

        logger.info(f"Reconciling job '{job_id}' with {report.total_tasks} tasks...")

        for task in tasks:
            task_id = task["id"]
            status = task["status"]
            file_path = task.get("file_path")

            if status == TaskStatus.COMPLETED:
                if not file_path:
                    # Missing record in generated_images!
                    self.store.set_task_status(
                        task_id=task_id,
                        new_status=TaskStatus.NEEDS_ATTENTION,
                        error_message="Marked completed but no image record found in database.",
                        error_type="MISSING_METADATA",
                    )
                    report.corrupted_or_missing.append(task_id)
                else:
                    val = ImageValidator.validate_file(Path(file_path))
                    if not val.is_valid:
                        # File is missing or corrupted on disk!
                        self.store.set_task_status(
                            task_id=task_id,
                            new_status=TaskStatus.NEEDS_ATTENTION,
                            error_message=f"Image file on disk is invalid or missing: {val.error_message}",
                            error_type="CORRUPT_OR_MISSING_FILE",
                        )
                        report.corrupted_or_missing.append(task_id)
                    else:
                        report.verified_on_disk += 1

            elif status in (TaskStatus.SUBMITTING, TaskStatus.GENERATING):
                # Process crashed while submitting or generating
                # Check if file was already saved before crash
                if file_path and ImageValidator.validate_file(Path(file_path)).is_valid:
                    self.store.set_task_status(task_id=task_id, new_status=TaskStatus.COMPLETED)
                    report.promoted_to_completed += 1
                else:
                    # Uncertain state: Prompt might have been sent to Gemini
                    self.store.set_task_status(
                        task_id=task_id,
                        new_status=TaskStatus.NEEDS_ATTENTION,
                        error_message="Interrupted while generating. Review Gemini session before retrying to prevent duplicate generation.",
                        error_type="INTERRUPTED_GENERATION",
                    )
                    report.flagged_for_attention.append(task_id)

            elif status in (TaskStatus.DOWNLOADING, TaskStatus.VALIDATING):
                # Crashed during download or validation
                if file_path and ImageValidator.validate_file(Path(file_path)).is_valid:
                    self.store.set_task_status(task_id=task_id, new_status=TaskStatus.COMPLETED)
                    report.promoted_to_completed += 1
                else:
                    # Safe to reset back to PENDING or RETRY_WAIT
                    self.store.set_task_status(
                        task_id=task_id,
                        new_status=TaskStatus.PENDING,
                        error_message="Interrupted during download/validation; safely reset to PENDING.",
                        error_type="INTERRUPTED_DOWNLOAD",
                    )
                    report.interrupted_reset.append(task_id)

            elif status in (TaskStatus.PENDING, TaskStatus.RETRY_WAIT):
                report.safe_to_run += 1

        # Refresh job aggregates
        counts = self.store.refresh_job_counts(job_id)

        # Update overall job status if needed
        if job["status"] == JobStatus.RUNNING:
            self.store.set_job_status(job_id, JobStatus.PAUSED)
            logger.info(f"Active running job '{job_id}' was paused for safety upon recovery.")

        report.summary_message = (
            f"Reconciliation complete: {report.verified_on_disk} verified on disk, "
            f"{report.promoted_to_completed} recovered, "
            f"{len(report.corrupted_or_missing)} missing/corrupted, "
            f"{len(report.interrupted_reset)} reset to pending, "
            f"{len(report.flagged_for_attention)} flagged for attention."
        )
        logger.info(report.summary_message)
        return report

    def reset_failed_tasks(self, job_id: str) -> int:
        """Resets all FAILED and NEEDS_ATTENTION tasks back to PENDING for user-initiated retry."""
        tasks = self.store.get_tasks_for_job(job_id)
        count = 0
        for t in tasks:
            if t["status"] in (TaskStatus.FAILED, TaskStatus.NEEDS_ATTENTION, TaskStatus.RETRY_WAIT):
                self.store.set_task_status(
                    task_id=t["id"],
                    new_status=TaskStatus.PENDING,
                    error_message=None,
                    error_type=None,
                )
                count += 1
        self.store.refresh_job_counts(job_id)
        logger.info(f"Reset {count} failed/flagged task(s) to PENDING in job '{job_id}'.")
        return count
