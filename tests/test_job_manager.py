import io
import pytest
from pathlib import Path
from PIL import Image
from unittest.mock import MagicMock

from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.channel_config import ChannelRegistry
from src.image_downloader import ImageDownloader
from src.drive_sync import DriveSyncManager
from src.job_manager import JobManager
from src.gemini_browser import GeminiBrowserAdapter, GeminiRateLimitError


@pytest.fixture
def manager_setup(tmp_path):
    db_file = tmp_path / "jm_test.db"
    store = ProgressStore(db_file)
    registry = ChannelRegistry(Path("config/channels.yaml"))
    downloader = ImageDownloader(tmp_path / "output")
    drive_sync = DriveSyncManager(mode="local", local_path=str(tmp_path / "output"))

    mock_browser = MagicMock(spec=GeminiBrowserAdapter)
    mock_browser.check_auth_status.return_value = (True, "Ready")

    # Generate a real valid PNG image byte buffer for mock generation
    img = Image.new("RGB", (640, 360), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    mock_browser.wait_for_image_completion.return_value = buf.getvalue()

    manager = JobManager(
        store=store,
        channel_registry=registry,
        downloader=downloader,
        drive_sync=drive_sync,
        browser_adapter=mock_browser,
        inter_prompt_delay_seconds=0,
    )
    # Redirect lock file to tmp_path
    manager.LOCK_FILE = tmp_path / "automation.lock"

    store.upsert_channel("ninety_sense", "Ninety Sense", "Psychology", "Ninety-Sense")
    store.upsert_video("vid-jm", "ninety_sense", "page-jm", "Habits of Highly Effective Minds")

    return manager, store, mock_browser, tmp_path


def test_lock_acquisition_and_concurrency(manager_setup):
    manager, store, mock_browser, tmp_path = manager_setup

    assert manager._acquire_lock() is True
    assert manager.LOCK_FILE.exists()

    # Second acquisition by another instance
    manager2 = JobManager(store, ChannelRegistry(), manager.downloader, manager.drive_sync)
    manager2.LOCK_FILE = manager.LOCK_FILE

    # Cleanup
    manager._release_lock()
    assert not manager.LOCK_FILE.exists()


def test_execute_task_pipeline_success(manager_setup):
    manager, store, mock_browser, tmp_path = manager_setup

    prompts = [{"index": 1, "label": "IMAGE 001", "text": "A brain glowing with light."}]
    store.create_job("job-pipe-1", "vid-jm", "ninety_sense", prompts)

    job = store.get_job("job-pipe-1")
    channel = manager.channel_registry.get("ninety_sense")
    task = store.get_next_runnable_task("job-pipe-1")

    success = manager._execute_task_pipeline(task, channel, job)
    assert success is True

    updated_task = store.get_task(task["id"])
    assert updated_task["status"] == TaskStatus.COMPLETED
    assert updated_task["file_path"] is not None
    assert Path(updated_task["file_path"]).exists()


def test_handles_rate_limit_pause(manager_setup):
    manager, store, mock_browser, tmp_path = manager_setup
    mock_browser.wait_for_image_completion.side_effect = GeminiRateLimitError("Rate limit reached")

    prompts = [{"index": 1, "label": "IMAGE 001", "text": "Prompt 1"}]
    store.create_job("job-pipe-2", "vid-jm", "ninety_sense", prompts)

    job = store.get_job("job-pipe-2")
    channel = manager.channel_registry.get("ninety_sense")
    task = store.get_next_runnable_task("job-pipe-2")

    with pytest.raises(GeminiRateLimitError):
        manager._execute_task_pipeline(task, channel, job)
