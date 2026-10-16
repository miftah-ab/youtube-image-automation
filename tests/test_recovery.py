import pytest
from pathlib import Path
from PIL import Image
from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.recovery import RecoveryManager
from src.image_validator import ImageValidator


@pytest.fixture
def env(tmp_path):
    db_file = tmp_path / "recovery_test.db"
    store = ProgressStore(db_file)
    recovery = RecoveryManager(store)

    store.upsert_channel("ninety_sense", "Ninety Sense", "Psychology", "Ninety-Sense")
    store.upsert_video("vid-rec", "ninety_sense", "page-rec", "Recovery Test Video")

    return store, recovery, tmp_path


def test_reconciliation_verifies_intact_completed_files(env):
    store, recovery, tmp_path = env
    img_file = tmp_path / "image-001.png"
    img = Image.new("RGB", (100, 100), color="green")
    img.save(img_file, format="PNG")

    prompts = [{"index": 1, "label": "IMAGE 001", "text": "Prompt 1"}]
    store.create_job("job-rec-1", "vid-rec", "ninety_sense", prompts)
    task_id = "job-rec-1-task-001"

    val = ImageValidator.validate_file(img_file)
    store.record_task_success(task_id, val, "local", "Local Verified")

    report = recovery.reconcile_job("job-rec-1")
    assert report.verified_on_disk == 1
    assert len(report.corrupted_or_missing) == 0


def test_reconciliation_flags_missing_file_for_attention(env):
    store, recovery, tmp_path = env
    missing_file = tmp_path / "deleted_image.png"

    prompts = [{"index": 1, "label": "IMAGE 001", "text": "Prompt 1"}]
    store.create_job("job-rec-2", "vid-rec", "ninety_sense", prompts)
    task_id = "job-rec-2-task-001"

    # Fake successful record pointing to non-existent file
    from src.image_validator import ImageValidationResult
    fake_val = ImageValidationResult(
        is_valid=True,
        file_path=str(missing_file),
        file_size_bytes=1000,
        sha256_hash="fakehash",
        detected_format="PNG",
        width=100,
        height=100,
    )
    store.record_task_success(task_id, fake_val, "local", "Local Verified")

    report = recovery.reconcile_job("job-rec-2")
    assert len(report.corrupted_or_missing) == 1
    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.NEEDS_ATTENTION


def test_reconciliation_interrupted_generating_state(env):
    store, recovery, tmp_path = env
    prompts = [{"index": 1, "label": "IMAGE 001", "text": "Prompt 1"}]
    store.create_job("job-rec-3", "vid-rec", "ninety_sense", prompts)
    task_id = "job-rec-3-task-001"

    # Simulate app crash mid-generation
    store.set_task_status(task_id, TaskStatus.GENERATING)

    report = recovery.reconcile_job("job-rec-3")
    assert len(report.flagged_for_attention) == 1
    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.NEEDS_ATTENTION


def test_reset_failed_tasks_to_pending(env):
    store, recovery, tmp_path = env
    prompts = [
        {"index": 1, "label": "IMAGE 001", "text": "Prompt 1"},
        {"index": 2, "label": "IMAGE 002", "text": "Prompt 2"},
    ]
    store.create_job("job-rec-4", "vid-rec", "ninety_sense", prompts)

    store.set_task_status("job-rec-4-task-001", TaskStatus.FAILED, error_message="Network glitch")
    store.set_task_status("job-rec-4-task-002", TaskStatus.NEEDS_ATTENTION, error_message="Review needed")

    reset_count = recovery.reset_failed_tasks("job-rec-4")
    assert reset_count == 2

    task1 = store.get_task("job-rec-4-task-001")
    task2 = store.get_task("job-rec-4-task-002")
    assert task1["status"] == TaskStatus.PENDING
    assert task2["status"] == TaskStatus.PENDING
