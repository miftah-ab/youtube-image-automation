import io
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from src.gdrive_api import GoogleDriveApiClient, DriveUploadResult
from src.drive_sync import DriveSyncManager


@pytest.fixture
def api_client(tmp_path):
    token_file = tmp_path / "test_token.json"
    secrets_file = tmp_path / "client_secrets.json"
    client = GoogleDriveApiClient(token_path=token_file, client_secrets_path=secrets_file)
    return client


def test_client_configuration_check(api_client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert api_client.is_configured is False

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    assert api_client.is_configured is True


def test_authorization_url_generation(api_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "my-test-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "my-secret")

    url, err = api_client.get_authorization_url()
    assert err is None
    assert "accounts.google.com/o/oauth2/v2/auth" in url
    assert "client_id=my-test-id" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Foauth2callback" in url


def test_drive_folder_idempotency(api_client):
    api_client.get_valid_access_token = MagicMock(return_value="mock_access_token")

    with patch("requests.get") as mock_get:
        # Simulate existing folder returned
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"files": [{"id": "folder_12345", "name": "YouTube-Automation"}]}
        mock_get.return_value = mock_resp

        folder_id = api_client.get_or_create_folder("YouTube-Automation")
        assert folder_id == "folder_12345"

        # Subsequent call uses cache without network request
        folder_id_cached = api_client.get_or_create_folder("YouTube-Automation")
        assert folder_id_cached == "folder_12345"
        assert mock_get.call_count == 1


def test_upload_image_duplicate_skips_reupload(api_client, tmp_path):
    api_client.get_valid_access_token = MagicMock(return_value="mock_access_token")
    api_client.get_or_create_folder = MagicMock(return_value="folder_video_id")

    img_file = tmp_path / "image-001.png"
    img_file.write_bytes(b"dummy image bytes")

    with patch("requests.get") as mock_get:
        # Existing file detected
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"files": [{"id": "existing_file_999", "name": "image-001.png", "webViewLink": "https://drive.google.com/file/d/existing_file_999"}]}
        mock_get.return_value = mock_resp

        result = api_client.upload_image(
            local_file_path=img_file,
            channel_subfolder="American-Wallet",
            video_identifier="TAW-001",
            overwrite=False,
        )

        assert result.success is True
        assert result.file_id == "existing_file_999"
        assert result.is_duplicate is True


def test_automatic_mode_falls_back_to_api_when_desktop_missing(tmp_path):
    mock_api = MagicMock(spec=GoogleDriveApiClient)
    mock_api.is_authorized.return_value = True
    mock_api.upload_image.return_value = DriveUploadResult(
        success=True,
        file_id="api_file_456",
        web_link="https://drive.google.com/file/d/api_file_456",
    )

    nonexistent_desktop_path = tmp_path / "NonexistentDrive" / "Folder"

    sync_mgr = DriveSyncManager(
        mode="automatic",
        local_path=str(tmp_path / "local_output"),
        gdrive_path=str(nonexistent_desktop_path),
        api_client=mock_api,
    )

    # Test inspection: should detect missing desktop and activate API fallback
    status = sync_mgr.inspect_storage()
    assert status.active_method == "api"
    assert "Automatic: Drive API Fallback Active" in status.status_label

    # Test sync execution
    src_file = tmp_path / "image-001.png"
    src_file.write_bytes(b"image binary payload")

    ok, label, f_id, err = sync_mgr.sync_to_destination(
        source_file=src_file,
        channel_subfolder="American-Wallet",
        video_identifier="TAW-001",
    )

    assert ok is True
    assert "Confirmed Cloud Upload" in label
    assert f_id == "api_file_456"
    assert mock_api.upload_image.called
