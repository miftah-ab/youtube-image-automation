import hashlib
import os
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image
from pydantic import BaseModel

from src.logging_config import logger


class ImageValidationResult(BaseModel):
    is_valid: bool
    file_path: str
    file_size_bytes: int = 0
    sha256_hash: str = ""
    detected_format: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    error_message: Optional[str] = None


class ImageValidator:
    """Validates downloaded image files against corruption, empty payloads, HTML error pages, and formats."""

    # Common HTML error markers that might be returned in an HTTP error or login bounce
    HTML_SIGNATURES = [
        b"<!doctype html",
        b"<html",
        b"<head",
        b"<body",
        b"error 404",
        b"403 forbidden",
        b"500 internal server error",
    ]

    ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "JPG"}

    @classmethod
    def compute_sha256(cls, file_path: Path) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @classmethod
    def validate_file(cls, path_to_file: Path) -> ImageValidationResult:
        """Thoroughly checks that the file exists, is non-empty, is not HTML/corrupt,
        and is a decodable image file.
        """
        path_str = str(path_to_file)

        if not path_to_file.exists():
            return ImageValidationResult(
                is_valid=False,
                file_path=path_str,
                error_message=f"File does not exist: {path_str}",
            )

        file_size = path_to_file.stat().st_size
        if file_size == 0:
            return ImageValidationResult(
                is_valid=False,
                file_path=path_str,
                file_size_bytes=0,
                error_message="File is empty (0 bytes).",
            )

        # Minimum reasonable image header size
        if file_size < 32:
            return ImageValidationResult(
                is_valid=False,
                file_path=path_str,
                file_size_bytes=file_size,
                error_message=f"File size too small to be a valid image ({file_size} bytes).",
            )

        # Check for HTML error response payload
        try:
            with open(path_to_file, "rb") as f:
                header = f.read(1024).lower()
                for marker in cls.HTML_SIGNATURES:
                    if marker in header:
                        return ImageValidationResult(
                            is_valid=False,
                            file_path=path_str,
                            file_size_bytes=file_size,
                            error_message="File content contains HTML error text rather than binary image data.",
                        )
        except Exception as e:
            return ImageValidationResult(
                is_valid=False,
                file_path=path_str,
                error_message=f"Could not read file header: {e}",
            )

        # Compute SHA256
        sha256_hash = cls.compute_sha256(path_to_file)

        # Verify image using PIL
        try:
            with Image.open(path_to_file) as img:
                detected_format = img.format.upper() if img.format else "UNKNOWN"
                width, height = img.size

                # Verify image data integrity
                img.verify()

            # Second pass to ensure decodability (verify() alone doesn't decode all bytes)
            with Image.open(path_to_file) as img:
                img.load()

            if detected_format not in cls.ALLOWED_IMAGE_FORMATS:
                return ImageValidationResult(
                    is_valid=False,
                    file_path=path_str,
                    file_size_bytes=file_size,
                    sha256_hash=sha256_hash,
                    detected_format=detected_format,
                    error_message=f"Unsupported image format: {detected_format}",
                )

            return ImageValidationResult(
                is_valid=True,
                file_path=path_str,
                file_size_bytes=file_size,
                sha256_hash=sha256_hash,
                detected_format=detected_format,
                width=width,
                height=height,
            )

        except Exception as e:
            return ImageValidationResult(
                is_valid=False,
                file_path=path_str,
                file_size_bytes=file_size,
                sha256_hash=sha256_hash,
                error_message=f"Image decoding failed: {e}",
            )
