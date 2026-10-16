import pytest
from pathlib import Path
from PIL import Image
from src.drive_sync import DriveSyncManager, StorageStatus


def test_local_storage_inspection(tmp_path):
    local_dir = tmp_path / "output_test"
    sync_mgr = DriveSyncManager(mode="local", local_path=str(local_dir))

    status = sync_mgr.inspect_storage()
    assert status.mode == "local"
    assert status.is_accessible is True
    assert status.is_writable is True
    assert status.status_label == "Local Storage Only"


def test_missing_gdrive_desktop_folder_inspection(tmp_path):
    nonexistent = tmp_path / "NonexistentDrive" / "Folder"
    sync_mgr = DriveSyncManager(mode="gdrive_desktop", gdrive_path=str(nonexistent))

    status = sync_mgr.inspect_storage()
    assert status.mode == "gdrive_desktop"
    assert status.is_accessible is False
    assert "Unavailable" in status.status_label


def test_drive_sync_copy_to_desktop_destination(tmp_path):
    src_file = tmp_path / "source.png"
    img = Image.new("RGB", (50, 50), color="yellow")
    img.save(src_file, format="PNG")

    dest_dir = tmp_path / "gdrive_sim"
    dest_dir.mkdir()

    sync_mgr = DriveSyncManager(mode="gdrive_desktop", gdrive_path=str(dest_dir))

    ok, label, f_id, err = sync_mgr.sync_to_destination(
        source_file=src_file,
        channel_subfolder="Channel-A",
        video_identifier="Video-1",
    )
    assert ok is True
    assert label == DriveSyncManager.CLOUD_SYNC_UNVERIFIED_LABEL
    assert (dest_dir / "Channel-A" / "Video-1" / "source.png").exists()
