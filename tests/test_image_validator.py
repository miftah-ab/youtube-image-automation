import io
import pytest
from PIL import Image
from src.image_validator import ImageValidator


def test_validate_valid_png(tmp_path):
    img_path = tmp_path / "valid.png"
    img = Image.new("RGB", (640, 360), color="blue")
    img.save(img_path, format="PNG")

    result = ImageValidator.validate_file(img_path)
    assert result.is_valid is True
    assert result.detected_format == "PNG"
    assert result.width == 640
    assert result.height == 360
    assert result.file_size_bytes > 0
    assert len(result.sha256_hash) == 64


def test_validate_valid_jpeg(tmp_path):
    img_path = tmp_path / "valid.jpg"
    img = Image.new("RGB", (800, 600), color="red")
    img.save(img_path, format="JPEG")

    result = ImageValidator.validate_file(img_path)
    assert result.is_valid is True
    assert result.detected_format == "JPEG"
    assert result.width == 800
    assert result.height == 600


def test_reject_zero_byte_file(tmp_path):
    empty_path = tmp_path / "empty.png"
    empty_path.write_bytes(b"")

    result = ImageValidator.validate_file(empty_path)
    assert result.is_valid is False
    assert "empty" in result.error_message.lower()


def test_reject_html_error_page(tmp_path):
    html_path = tmp_path / "error.png"
    html_content = b"<!DOCTYPE html><html><body><h1>500 Internal Server Error</h1></body></html>"
    html_path.write_bytes(html_content)

    result = ImageValidator.validate_file(html_path)
    assert result.is_valid is False
    assert "html error" in result.error_message.lower()


def test_reject_corrupted_data(tmp_path):
    corrupt_path = tmp_path / "corrupt.png"
    corrupt_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"garbage_random_bytes_corrupted_content")

    result = ImageValidator.validate_file(corrupt_path)
    assert result.is_valid is False
    assert "decoding failed" in result.error_message.lower() or "unsupported" in result.error_message.lower()


def test_reject_nonexistent_file(tmp_path):
    missing_path = tmp_path / "does_not_exist.png"
    result = ImageValidator.validate_file(missing_path)
    assert result.is_valid is False
    assert "does not exist" in result.error_message.lower()
