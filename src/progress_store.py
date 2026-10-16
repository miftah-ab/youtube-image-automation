import os
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Callable, Union
from contextlib import contextmanager

from src.logging_config import logger
from src.image_validator import ImageValidationResult


# Task Status Enumeration
class TaskStatus:
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    GENERATING = "GENERATING"
    DOWNLOADING = "DOWNLOADING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


# Job Status Enumeration
class JobStatus:
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ProgressStore:
    """ACID SQLite store for persistent jobs, tasks, attempts, and generated images."""

    def __init__(self, db_path: Union[str, Path] = "data/jobs.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=15.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Initializes tables and indexes with atomic migrations."""
        with self.get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                theme TEXT,
                output_subfolder TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL REFERENCES channels(id),
                notion_page_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(id),
                channel_id TEXT NOT NULL REFERENCES channels(id),
                status TEXT NOT NULL DEFAULT 'CREATED',
                total_prompts INTEGER NOT NULL DEFAULT 0,
                completed_prompts INTEGER NOT NULL DEFAULT 0,
                failed_prompts INTEGER NOT NULL DEFAULT 0,
                current_task_index INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS prompt_tasks (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                prompt_index INTEGER NOT NULL,
                label TEXT NOT NULL,
                raw_prompt TEXT NOT NULL,
                final_prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                last_error TEXT,
                error_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id, prompt_index)
            );

            CREATE TABLE IF NOT EXISTS generated_images (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE REFERENCES prompt_tasks(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                sha256_hash TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                format TEXT,
                storage_mode TEXT NOT NULL,
                cloud_sync_status TEXT NOT NULL,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS task_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES prompt_tasks(id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_job_status ON prompt_tasks(job_id, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_job_index ON prompt_tasks(job_id, prompt_index);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            """)

    def upsert_channel(self, channel_id: str, name: str, theme: str, output_subfolder: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (id, name, theme, output_subfolder)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    theme = excluded.theme,
                    output_subfolder = excluded.output_subfolder
                """,
                (channel_id, name, theme, output_subfolder),
            )

    def upsert_video(self, video_id: str, channel_id: str, notion_page_id: str, title: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO videos (id, channel_id, notion_page_id, title)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    notion_page_id = excluded.notion_page_id
                """,
                (video_id, channel_id, notion_page_id, title),
            )

    def create_job(
        self,
        job_id: str,
        video_id: str,
        channel_id: str,
        prompts: List[Dict[str, Any]],
        max_retries: int = 3,
    ) -> None:
        """Atomically creates a job and all its constituent prompt tasks."""
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.get_connection() as conn:
            # Check if job already exists
            cur = conn.execute("SELECT id, status FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if row:
                raise ValueError(f"Job with ID '{job_id}' already exists (status: {row['status']}).")

            conn.execute(
                """
                INSERT INTO jobs (id, video_id, channel_id, status, total_prompts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, video_id, channel_id, JobStatus.CREATED, len(prompts), now, now),
            )

            task_records = []
            for p in prompts:
                task_id = f"{job_id}-task-{p['index']:03d}"
                task_records.append((
                    task_id,
                    job_id,
                    p["index"],
                    p.get("label", f"Prompt {p['index']}"),
                    p.get("raw_prompt", p.get("text", "")),
                    p.get("final_prompt", p.get("text", "")),
                    TaskStatus.PENDING,
                    0,
                    max_retries,
                    now,
                    now,
                ))

            conn.executemany(
                """
                INSERT INTO prompt_tasks (
                    id, job_id, prompt_index, label, raw_prompt, final_prompt,
                    status, retry_count, max_retries, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                task_records,
            )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT j.*, v.title as video_title, c.name as channel_name
                FROM jobs j
                JOIN videos v ON j.video_id = v.id
                JOIN channels c ON j.channel_id = c.id
                WHERE j.id = ?
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT j.*, v.title as video_title, c.name as channel_name
                FROM jobs j
                JOIN videos v ON j.video_id = v.id
                JOIN channels c ON j.channel_id = c.id
                ORDER BY j.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_tasks_for_job(self, job_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT t.*, gi.file_path, gi.sha256_hash, gi.file_size_bytes, gi.format, gi.cloud_sync_status
                FROM prompt_tasks t
                LEFT JOIN generated_images gi ON t.id = gi.task_id
                WHERE t.job_id = ?
                ORDER BY t.prompt_index ASC
                """,
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT t.*, gi.file_path, gi.sha256_hash, gi.file_size_bytes, gi.format, gi.cloud_sync_status
                FROM prompt_tasks t
                LEFT JOIN generated_images gi ON t.id = gi.task_id
                WHERE t.id = ?
                """,
                (task_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_next_runnable_task(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM prompt_tasks
                WHERE job_id = ? AND status IN (?, ?)
                ORDER BY prompt_index ASC
                LIMIT 1
                """,
                (job_id, TaskStatus.PENDING, TaskStatus.RETRY_WAIT),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def set_task_status(
        self,
        task_id: str,
        new_status: str,
        expected_status: Optional[str] = None,
        error_message: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> bool:
        """Atomic Compare-And-Swap status transition for a task."""
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.get_connection() as conn:
            if expected_status:
                cur = conn.execute(
                    """
                    UPDATE prompt_tasks
                    SET status = ?, last_error = ?, error_type = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (new_status, error_message, error_type, now, task_id, expected_status),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE prompt_tasks
                    SET status = ?, last_error = ?, error_type = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status, error_message, error_type, now, task_id),
                )
            return cur.rowcount > 0

    def record_task_success(
        self,
        task_id: str,
        val: ImageValidationResult,
        storage_mode: str,
        cloud_sync_status: str,
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        image_id = f"img-{task_id}"
        with self.get_connection() as conn:
            # 1. Insert or replace generated_images
            conn.execute(
                """
                INSERT INTO generated_images (
                    id, task_id, file_path, file_size_bytes, sha256_hash,
                    width, height, format, storage_mode, cloud_sync_status, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    file_size_bytes = excluded.file_size_bytes,
                    sha256_hash = excluded.sha256_hash,
                    width = excluded.width,
                    height = excluded.height,
                    format = excluded.format,
                    storage_mode = excluded.storage_mode,
                    cloud_sync_status = excluded.cloud_sync_status,
                    verified_at = excluded.verified_at
                """,
                (
                    image_id,
                    task_id,
                    val.file_path,
                    val.file_size_bytes,
                    val.sha256_hash,
                    val.width,
                    val.height,
                    val.detected_format,
                    storage_mode,
                    cloud_sync_status,
                    now,
                ),
            )

            # 2. Update task status to COMPLETED
            conn.execute(
                """
                UPDATE prompt_tasks
                SET status = ?, last_error = NULL, error_type = NULL, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.COMPLETED, now, task_id),
            )

    def record_task_failure(
        self,
        task_id: str,
        error_message: str,
        error_type: str,
        can_retry: bool,
    ) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.get_connection() as conn:
            cur = conn.execute("SELECT retry_count, max_retries FROM prompt_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return TaskStatus.FAILED

            current_retries = row["retry_count"]
            max_retries = row["max_retries"]

            new_status = TaskStatus.RETRY_WAIT if (can_retry and current_retries + 1 < max_retries) else TaskStatus.FAILED

            conn.execute(
                """
                UPDATE prompt_tasks
                SET status = ?, retry_count = retry_count + 1, last_error = ?, error_type = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, error_message, error_type, now, task_id),
            )
            return new_status

    def set_job_status(self, job_id: str, new_status: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.get_connection() as conn:
            extra = ""
            if new_status == JobStatus.RUNNING:
                extra = ", started_at = COALESCE(started_at, ?)"
            elif new_status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.STOPPED):
                extra = ", finished_at = ?"

            params = [new_status, now]
            if extra:
                params.append(now)
            params.append(job_id)

            conn.execute(f"UPDATE jobs SET status = ?, updated_at = ? {extra} WHERE id = ?", tuple(params))

    def refresh_job_counts(self, job_id: str) -> Dict[str, int]:
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed
                FROM prompt_tasks
                WHERE job_id = ?
                """,
                (job_id,),
            )
            row = cur.fetchone()
            total = row["total"] or 0
            completed = row["completed"] or 0
            failed = row["failed"] or 0

            conn.execute(
                """
                UPDATE jobs
                SET total_prompts = ?, completed_prompts = ?, failed_prompts = ?, updated_at = ?
                WHERE id = ?
                """,
                (total, completed, failed, datetime.datetime.now(datetime.timezone.utc), job_id),
            )
            return {"total": total, "completed": completed, "failed": failed}
