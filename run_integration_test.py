import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from src.logging_config import logger
from src.channel_config import ChannelRegistry
from src.notion_client import NotionClient
from src.prompt_parser import PromptParser
from src.image_validator import ImageValidator
from src.image_downloader import ImageDownloader
from src.drive_sync import DriveSyncManager
from src.progress_store import ProgressStore, TaskStatus, JobStatus
from src.recovery import RecoveryManager
from src.job_manager import JobManager
from src.gemini_browser import GeminiBrowserAdapter


def run_mock_integration(test_output_dir: Path):
    logger.info("=== RUNNING MOCK END-TO-END INTEGRATION TEST ===")

    # 1. Channels
    reg = ChannelRegistry(Path("config/channels.yaml"))
    ch = reg.get("ninety_sense")
    assert ch is not None, "Failed to load channel 'ninety_sense'"
    logger.info(f"[PASS] Channel loaded: {ch.name}")

    # 2. Synthetic Notion Blocks & Prompt Parser
    synthetic_blocks = [
        {"type": "heading_1", "text": "Episode 12 - Cognitive Bias"},
        {"type": "heading_2", "text": "Prompts"},
        {"type": "paragraph", "text": "IMAGE 001", "id": "b-1"},
        {"type": "paragraph", "text": "An isolated individual sitting at a wooden desk surrounded by floating clocks, moody atmospheric lighting.", "id": "b-2"},
        {"type": "paragraph", "text": "IMAGE 002: A split brain diagram with abstract neon synaptic connections.", "id": "b-3"},
    ]
    parser = PromptParser(prompt_section_heading=ch.notion.prompt_section_heading)
    parse_result = parser.parse_blocks(synthetic_blocks)
    assert parse_result.total_count == 2, f"Expected 2 prompts, got {parse_result.total_count}"
    logger.info(f"[PASS] PromptParser extracted {parse_result.total_count} prompts in order.")

    # 3. SQLite Storage & ACID Job Creation
    db_file = test_output_dir / "integration_jobs.db"
    store = ProgressStore(db_file)
    store.upsert_channel(ch.id, ch.name, ch.theme, ch.output_subfolder)
    store.upsert_video("vid-int-01", ch.id, "page-mock-01", "Episode 12 - Cognitive Bias")

    job_id = "job-mock-integration-01"
    prompt_records = []
    for p in parse_result.prompts:
        prompt_records.append({
            "index": p.index,
            "label": p.label,
            "raw_prompt": p.text,
            "final_prompt": ch.prepare_prompt(p.text),
        })
    store.create_job(job_id, "vid-int-01", ch.id, prompt_records)
    logger.info(f"[PASS] Job {job_id} created with {len(prompt_records)} tasks in SQLite.")

    # 4. Atomic Download & PIL Image Validation
    from PIL import Image
    import io
    dummy_img = Image.new("RGB", (1280, 720), color="navy")
    buf = io.BytesIO()
    dummy_img.save(buf, format="PNG")
    raw_png_bytes = buf.getvalue()

    dl = ImageDownloader(base_output_dir=test_output_dir / "output")
    dl_res = dl.save_image_bytes(
        image_bytes=raw_png_bytes,
        channel_subfolder=ch.output_subfolder,
        video_identifier="Episode 12 - Cognitive Bias",
        filename_pattern=ch.filename_pattern,
        index=1,
    )
    assert dl_res.success is True, f"Download failed: {dl_res.error_message}"
    assert Path(dl_res.final_path).exists()
    logger.info(f"[PASS] ImageDownloader atomically saved & validated {dl_res.final_path} (SHA256: {dl_res.validation.sha256_hash[:16]}...)")

    # 5. Storage Sync
    sync = DriveSyncManager(mode="local", local_path=str(test_output_dir / "output"))
    sync_ok, sync_label, drive_file_id, sync_err = sync.sync_to_destination(
        source_file=Path(dl_res.final_path),
        channel_subfolder=ch.output_subfolder,
        video_identifier="Episode 12 - Cognitive Bias",
    )
    assert sync_ok is True
    logger.info(f"[PASS] DriveSync: {sync_label}")

    # 6. Record Success in DB
    task1 = store.get_tasks_for_job(job_id)[0]
    store.record_task_success(task1["id"], dl_res.validation, "local", sync_label)
    logger.info(f"[PASS] Recorded task success in SQLite.")

    # 7. Recovery & Reconciliation Audit
    recovery = RecoveryManager(store)
    report = recovery.reconcile_job(job_id)
    assert report.verified_on_disk == 1
    logger.info(f"[PASS] RecoveryManager audited files on disk: {report.summary_message}")

    logger.info("=== ALL MOCK INTEGRATION CHECKS PASSED SUCCESSFULLY ===")


def run_live_integration():
    logger.info("=== RUNNING LIVE INTEGRATION TEST ===")
    notion_token = os.getenv("NOTION_API_KEY")
    if not notion_token:
        logger.error("NOTION_API_KEY is not set in environment or .env. Cannot run live Notion test.")
        return

    client = NotionClient(api_key=notion_token)
    ok, msg = client.test_connection()
    if not ok:
        logger.error(f"Notion Connection Failed: {msg}")
        return
    logger.info(f"Notion Connection Successful: {msg}")

    # Launch browser to check Gemini
    browser = GeminiBrowserAdapter(profile_dir=Path("data/browser_profile"), headless=False)
    try:
        browser.launch()
        is_auth, auth_msg = browser.check_auth_status()
        logger.info(f"Gemini Auth Status: {auth_msg} (is_authenticated={is_auth})")
    finally:
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YouTube Image Automation Integration Test Runner")
    parser.add_argument("--live", action="store_true", help="Run live integration test with real Notion and Gemini")
    args = parser.parse_args()

    if args.live:
        run_live_integration()
    else:
        test_dir = Path("temp/mock_integration")
        test_dir.mkdir(parents=True, exist_ok=True)
        run_mock_integration(test_dir)
