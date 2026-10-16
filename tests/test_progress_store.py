import pytest
from pathlib import Path
from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.image_validator import ImageValidationResult


@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    return ProgressStore(db_file)


def test_schema_init_and_channel_video_upsert(store):
    store.upsert_channel("ninety_sense", "Ninety Sense", "Psychology", "Ninety-Sense")
    store.upsert_video("vid-001", "ninety_sense", "page-12345", "The Science of Habits")

    # Re-upserting updates cleanly without error
    store.upsert_video("vid-001", "ninety_sense", "page-12345", "The Science of Habits - Final Cut")


def test_atomic_job_creation_and_tasks(store):
    store.upsert_channel("case_xyz", "Case XYZ", "Storytelling", "Case-XYZ")
    store.upsert_video("vid-002", "case_xyz", "page-999", "The Lost City")

    prompts = [
        {"index": 1, "label": "IMAGE 001", "text": "Map of the ancient ruins."},
        {"index": 2, "label": "IMAGE 002", "text": "Archaeologist uncovering an artifact."},
    ]

    store.create_job("job-test-1", "vid-002", "case_xyz", prompts)

    job = store.get_job("job-test-1")
    assert job is not None
    assert job["total_prompts"] == 2
    assert job["status"] == JobStatus.CREATED

    tasks = store.get_tasks_for_job("job-test-1")
    assert len(tasks) == 2
    assert tasks[0]["status"] == TaskStatus.PENDING
    assert tasks[0]["prompt_index"] == 1
    assert tasks[1]["prompt_index"] == 2


def test_prevent_duplicate_job_creation(store):
    store.upsert_channel("american_wallet", "American Wallet", "Finance", "American-Wallet")
    store.upsert_video("vid-003", "american_wallet", "page-111", "Index Funds 101")

    prompts = [{"index": 1, "label": "IMAGE 001", "text": "Stock market chart."}]
    store.create_job("job-duplicate-test", "vid-003", "american_wallet", prompts)

    with pytest.raises(ValueError, match="already exists"):
        store.create_job("job-duplicate-test", "vid-003", "american_wallet", prompts)


def test_atomic_status_transitions_and_completion(store):
    store.upsert_channel("ninety_sense", "Ninety Sense", "Psychology", "Ninety-Sense")
    store.upsert_video("vid-004", "ninety_sense", "page-222", "Body Language Secrets")

    prompts = [{"index": 1, "label": "IMAGE 001", "text": "Micro-expressions comparison."}]
    store.create_job("job-status-test", "vid-004", "ninety_sense", prompts)

    task = store.get_next_runnable_task("job-status-test")
    assert task is not None
    task_id = task["id"]

    # Transition to SUBMITTING
    ok = store.set_task_status(task_id, TaskStatus.SUBMITTING, expected_status=TaskStatus.PENDING)
    assert ok is True

    # Transition to GENERATING
    ok = store.set_task_status(task_id, TaskStatus.GENERATING, expected_status=TaskStatus.SUBMITTING)
    assert ok is True

    # Record Success with Image Validation
    val_result = ImageValidationResult(
        is_valid=True,
        file_path="/output/test.png",
        file_size_bytes=10240,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        detected_format="PNG",
        width=1280,
        height=720,
    )

    store.record_task_success(task_id, val_result, "local", "Local Verified")

    updated_task = store.get_task(task_id)
    assert updated_task["status"] == TaskStatus.COMPLETED
    assert updated_task["sha256_hash"] == val_result.sha256_hash

    # Aggregates
    counts = store.refresh_job_counts("job-status-test")
    assert counts["completed"] == 1
    assert counts["failed"] == 0
