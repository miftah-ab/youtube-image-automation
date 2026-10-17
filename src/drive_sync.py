import os
import shutil
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from pydantic import BaseModel

from src.logging_config import logger
from src.image_validator import ImageValidator
from src.gdrive_api import GoogleDriveApiClient, DriveUploadResult


class StorageStatus(BaseModel):
    mode: str  # "automatic", "gdrive_desktop", "gdrive_api", "local"
    configured_path: str
    is_accessible: bool
    is_writable: bool
    free_space_bytes: Optional[int] = None
    free_space_gb: Optional[float] = None
    cloud_sync_verified: bool = False
    active_method: str  # "desktop", "api", "local"
    status_label: str
    message: str


class DriveSyncManager:
    """Unified storage and cloud synchronization manager supporting:
    1. Local-only storage.
    2. Google Drive for desktop synchronized folder.
    3. Google Drive REST API direct verified upload.
    4. Automatic fallback: Desktop folder when available, API upload when desktop is missing.
    """

    CLOUD_SYNC_UNVERIFIED_LABEL = "Saved locally; cloud sync unverified"
    CLOUD_SYNC_CONFIRMED_LABEL = "Cloud upload confirmed (Drive API)"

    def __init__(
        self,
        mode: str = "automatic",
        local_path: str = "output",
        gdrive_path: str = "G:\\My Drive\\YouTube-Automation",
        api_client: Optional[GoogleDriveApiClient] = None,
    ):
        self.mode = mode.lower()
        self.local_path = Path(local_path)
        self.gdrive_path = Path(gdrive_path)
        self.api_client = api_client or GoogleDriveApiClient()

    def get_active_local_root(self) -> Path:
        """Returns the local staging or target folder on disk."""
        if self.mode == "gdrive_desktop":
            return self.gdrive_path
        elif self.mode == "automatic":
            # If desktop folder exists and is writable, use it; otherwise local staging
            if self.gdrive_path.exists() and os.access(str(self.gdrive_path), os.W_OK):
                return self.gdrive_path
            return self.local_path
        return self.local_path

    get_active_root_path = get_active_local_root

    def inspect_storage(self) -> StorageStatus:
        target = self.get_active_local_root()
        path_str = str(target)

        # Check local folder existence and accessibility
        is_accessible = target.exists()
        if not is_accessible:
            if target == self.local_path:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    is_accessible = True
                except Exception:
                    is_accessible = False

        is_writable = False
        if is_accessible:
            probe = target / ".sync_probe.tmp"
            try:
                with open(probe, "w") as f:
                    f.write("probe")
                probe.unlink()
                is_writable = True
            except Exception:
                is_writable = False

        free_bytes = None
        free_gb = None
        if is_accessible:
            try:
                _, _, free = shutil.disk_usage(str(target))
                free_bytes = free
                free_gb = round(free / (1024 ** 3), 2)
            except Exception:
                pass

        # Determine active method & labels
        api_auth = self.api_client.is_authorized()

        if self.mode == "local":
            return StorageStatus(
                mode="local",
                configured_path=path_str,
                is_accessible=is_accessible,
                is_writable=is_writable,
                free_space_bytes=free_bytes,
                free_space_gb=free_gb,
                cloud_sync_verified=False,
                active_method="local",
                status_label="Local Storage Only",
                message=f"Saving to local directory: {path_str}",
            )

        elif self.mode == "gdrive_desktop":
            if is_accessible and is_writable:
                return StorageStatus(
                    mode="gdrive_desktop",
                    configured_path=path_str,
                    is_accessible=True,
                    is_writable=True,
                    free_space_bytes=free_bytes,
                    free_space_gb=free_gb,
                    cloud_sync_verified=False,
                    active_method="desktop",
                    status_label="Drive for Desktop Active (Cloud sync unverified)",
                    message=(
                        f"Writing to synchronized folder '{path_str}'. Files are queued for Google Drive client upload. "
                        "Writing locally does not prove files have reached Google's cloud servers."
                    ),
                )
            else:
                return StorageStatus(
                    mode="gdrive_desktop",
                    configured_path=path_str,
                    is_accessible=False,
                    is_writable=False,
                    cloud_sync_verified=False,
                    active_method="desktop",
                    status_label="Drive Desktop Folder Unavailable",
                    message=f"Google Drive folder '{path_str}' is missing or not writable. Check Google Drive for Desktop app.",
                )

        elif self.mode == "gdrive_api":
            if api_auth:
                return StorageStatus(
                    mode="gdrive_api",
                    configured_path=str(self.local_path),
                    is_accessible=is_accessible,
                    is_writable=is_writable,
                    free_space_bytes=free_bytes,
                    free_space_gb=free_gb,
                    cloud_sync_verified=True,
                    active_method="api",
                    status_label="Drive API Active (Confirmed Cloud Uploads)",
                    message="Direct Google Drive v3 REST API upload is authorized and active.",
                )
            else:
                return StorageStatus(
                    mode="gdrive_api",
                    configured_path=str(self.local_path),
                    is_accessible=is_accessible,
                    is_writable=is_writable,
                    cloud_sync_verified=False,
                    active_method="api",
                    status_label="Drive API Not Authorized",
                    message="Google Drive API credentials configured, but user authorization is required via OAuth.",
                )

        else:  # "automatic"
            desktop_available = self.gdrive_path.exists() and os.access(str(self.gdrive_path), os.W_OK)
            if desktop_available:
                return StorageStatus(
                    mode="automatic",
                    configured_path=str(self.gdrive_path),
                    is_accessible=True,
                    is_writable=True,
                    free_space_bytes=free_bytes,
                    free_space_gb=free_gb,
                    cloud_sync_verified=False,
                    active_method="desktop",
                    status_label="Automatic: Drive for Desktop Active",
                    message=f"Synchronized folder '{self.gdrive_path}' detected. Writing to Desktop synced directory.",
                )
            elif api_auth:
                return StorageStatus(
                    mode="automatic",
                    configured_path=str(self.local_path),
                    is_accessible=is_accessible,
                    is_writable=is_writable,
                    free_space_bytes=free_bytes,
                    free_space_gb=free_gb,
                    cloud_sync_verified=True,
                    active_method="api",
                    status_label="Automatic: Drive API Fallback Active",
                    message="Drive for Desktop not found. Automatically falling back to direct Google Drive API uploads.",
                )
            else:
                return StorageStatus(
                    mode="automatic",
                    configured_path=str(self.local_path),
                    is_accessible=is_accessible,
                    is_writable=is_writable,
                    free_space_bytes=free_bytes,
                    free_space_gb=free_gb,
                    cloud_sync_verified=False,
                    active_method="local",
                    status_label="Automatic: Local Staging Only",
                    message="Neither Drive for Desktop nor Drive API is currently active. Saving to local storage.",
                )

    def sync_to_destination(
        self,
        source_file: Path,
        channel_subfolder: str,
        video_identifier: str,
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        """Saves file to local destination and uploads via Google Drive API if requested.
        Returns: (success, status_label, drive_file_id, error_message).
        """
        # Step 1: Ensure local write is always preserved as source of truth
        active_root = self.get_active_local_root()
        final_dest = active_root / channel_subfolder / video_identifier / source_file.name
        final_dest.parent.mkdir(parents=True, exist_ok=True)

        if not final_dest.exists():
            temp_dest = final_dest.with_suffix(".tmp")
            try:
                shutil.copy2(str(source_file), str(temp_dest))
                shutil.move(str(temp_dest), str(final_dest))
            except Exception as e:
                err = f"Failed saving to {final_dest}: {e}"
                logger.error(err)
                return False, "Local Write Failed", None, err

        # Step 2: Handle Cloud Sync based on mode
        status = self.inspect_storage()

        # If active method is API (either in gdrive_api mode or automatic fallback)
        if status.active_method == "api":
            logger.info(f"Initiating Google Drive API upload for '{source_file.name}'...")
            upload_res: DriveUploadResult = self.api_client.upload_image(
                local_file_path=final_dest,
                channel_subfolder=channel_subfolder,
                video_identifier=video_identifier,
            )
            if upload_res.success:
                f_id = upload_res.file_id
                label = f"Confirmed Cloud Upload (Drive ID: {f_id})"
                return True, label, f_id, None
            else:
                err = f"API Upload failed: {upload_res.error_message}. Local file preserved."
                logger.warning(err)
                return True, "Saved locally; API upload failed", None, err

        elif status.active_method == "desktop":
            return True, self.CLOUD_SYNC_UNVERIFIED_LABEL, None, None

        else:
            return True, "Local Verified", None, None
