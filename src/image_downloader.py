import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Union, Dict, Any
from pydantic import BaseModel

from src.image_validator import ImageValidator, ImageValidationResult
from src.logging_config import logger


class DownloadResult(BaseModel):
    success: bool
    final_path: Optional[str] = None
    temporary_path: Optional[str] = None
    validation: Optional[ImageValidationResult] = None
    error_message: Optional[str] = None
    is_duplicate: bool = False


class ImageDownloader:
    """Manages secure, atomic download and local storage of generated images."""

    def __init__(self, base_output_dir: Union[str, Path] = "output"):
        self.base_output_dir = Path(base_output_dir)
        self.base_output_dir.mkdir(parents=True, exist_ok=True)

    def prepare_destination_path(
        self,
        channel_subfolder: str,
        video_identifier: str,
        filename_pattern: str,
        index: int,
        detected_ext: str = "png",
    ) -> Path:
        """Constructs deterministic target path: base/channel/video/image-001.ext"""
        # Clean folder names to prevent path traversal
        clean_channel = "".join(c for c in channel_subfolder if c.isalnum() or c in ("-", "_", " ")).strip()
        clean_video = "".join(c for c in video_identifier if c.isalnum() or c in ("-", "_", " ")).strip()

        folder = self.base_output_dir / clean_channel / clean_video
        folder.mkdir(parents=True, exist_ok=True)

        clean_ext = detected_ext.lower().lstrip(".")
        filename = filename_pattern.format(index=index, ext=clean_ext)
        return folder / filename

    def save_image_bytes(
        self,
        image_bytes: bytes,
        channel_subfolder: str,
        video_identifier: str,
        filename_pattern: str,
        index: int,
        overwrite: bool = False,
    ) -> DownloadResult:
        """Atomically saves bytes via a temporary file, validates it, and promotes it to final location."""
        if not image_bytes:
            return DownloadResult(success=False, error_message="Received empty byte buffer for image.")

        temp_fd, temp_path_str = tempfile.mkstemp(suffix=".tmp", prefix="img_dl_")
        temp_path = Path(temp_path_str)

        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(image_bytes)

            return self._validate_and_promote(
                temp_path=temp_path,
                channel_subfolder=channel_subfolder,
                video_identifier=video_identifier,
                filename_pattern=filename_pattern,
                index=index,
                overwrite=overwrite,
            )
        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            logger.error(f"Failed to save image bytes: {e}")
            return DownloadResult(success=False, error_message=str(e))

    def save_existing_temp_file(
        self,
        downloaded_temp_path: Path,
        channel_subfolder: str,
        video_identifier: str,
        filename_pattern: str,
        index: int,
        overwrite: bool = False,
    ) -> DownloadResult:
        """Promotes an existing Playwright-downloaded file through validation into destination."""
        return self._validate_and_promote(
            temp_path=downloaded_temp_path,
            channel_subfolder=channel_subfolder,
            video_identifier=video_identifier,
            filename_pattern=filename_pattern,
            index=index,
            overwrite=overwrite,
        )

    def _validate_and_promote(
        self,
        temp_path: Path,
        channel_subfolder: str,
        video_identifier: str,
        filename_pattern: str,
        index: int,
        overwrite: bool = False,
    ) -> DownloadResult:
        # Step 1: Validate file integrity
        validation = ImageValidator.validate_file(temp_path)
        if not validation.is_valid:
            # Preserve temp file for diagnostics if needed, or remove
            err = f"Image validation failed: {validation.error_message}"
            logger.warning(err)
            return DownloadResult(
                success=False,
                temporary_path=str(temp_path),
                validation=validation,
                error_message=err,
            )

        # Step 2: Determine appropriate extension based on actual detected format
        fmt = (validation.detected_format or "PNG").lower()
        ext_map = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}
        ext = ext_map.get(fmt, fmt)

        # Step 3: Determine destination path
        destination = self.prepare_destination_path(
            channel_subfolder=channel_subfolder,
            video_identifier=video_identifier,
            filename_pattern=filename_pattern,
            index=index,
            detected_ext=ext,
        )

        # Step 4: Check if destination already exists
        if destination.exists() and not overwrite:
            existing_val = ImageValidator.validate_file(destination)
            if existing_val.is_valid and existing_val.sha256_hash == validation.sha256_hash:
                # Exactly the same file already verified and in place
                try:
                    temp_path.unlink()
                except Exception:
                    pass
                return DownloadResult(
                    success=True,
                    final_path=str(destination),
                    validation=validation,
                    is_duplicate=True,
                )
            else:
                err = f"Destination file already exists and overwrite is disabled: {destination}"
                logger.error(err)
                return DownloadResult(
                    success=False,
                    temporary_path=str(temp_path),
                    validation=validation,
                    error_message=err,
                )

        # Step 5: Atomically promote file
        try:
            shutil.move(str(temp_path), str(destination))
            logger.info(f"Successfully validated and saved image: {destination} ({validation.width}x{validation.height}, {validation.file_size_bytes} bytes)")
            return DownloadResult(
                success=True,
                final_path=str(destination),
                validation=validation,
            )
        except Exception as e:
            err = f"Failed to promote validated image to {destination}: {e}"
            logger.error(err)
            return DownloadResult(
                success=False,
                temporary_path=str(temp_path),
                validation=validation,
                error_message=err,
            )
