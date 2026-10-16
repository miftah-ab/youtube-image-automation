import os
import json
import time
import socket
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import requests
from pydantic import BaseModel

from src.logging_config import logger


class DriveUploadResult(BaseModel):
    success: bool
    file_id: Optional[str] = None
    web_link: Optional[str] = None
    is_duplicate: bool = False
    error_message: Optional[str] = None


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Temporary local HTTP server handler to receive Google OAuth authorization code."""
    auth_code: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:sans-serif;text-align:center;padding:50px;'>")
            self.wfile.write(b"<h2>Google Drive Authorization Successful!</h2>")
            self.wfile.write(b"<p>You can close this tab and return to the YouTube Image Automation dashboard.</p>")
            self.wfile.write(b"</body></html>")
        elif "error" in params:
            OAuthCallbackHandler.error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Authorization Denied</h2></body></html>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Silence default HTTP server console noise
        pass


class GoogleDriveApiClient:
    """Official Google Drive v3 REST API client with OAuth 2.0 authorization,
    automatic token refresh, folder hierarchy management, and confirmed upload verification.
    """

    OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
    DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    REDIRECT_URI = "http://localhost:8080/oauth2callback"

    def __init__(
        self,
        token_path: Path = Path("data/gdrive_token.json"),
        client_secrets_path: Path = Path("client_secrets.json"),
    ):
        self.token_path = token_path
        self.client_secrets_path = client_secrets_path
        self._folder_cache: Dict[str, str] = {}  # "parent_id/name" -> folder_id

    def _get_client_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Reads client ID and client Secret from environment or client_secrets.json."""
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

        if client_id and client_secret:
            return client_id.strip(), client_secret.strip()

        if self.client_secrets_path.exists():
            try:
                with open(self.client_secrets_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                installed = data.get("installed") or data.get("web") or {}
                return installed.get("client_id"), installed.get("client_secret")
            except Exception as e:
                logger.error(f"Failed parsing {self.client_secrets_path}: {e}")

        return None, None

    @property
    def is_configured(self) -> bool:
        cid, csec = self._get_client_credentials()
        return bool(cid and csec)

    def is_authorized(self) -> bool:
        if not self.token_path.exists():
            return False
        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("refresh_token") or data.get("access_token"))
        except Exception:
            return False

    def get_authorization_url(self) -> Tuple[Optional[str], Optional[str]]:
        """Generates browser OAuth URL for user authorization."""
        client_id, _ = self._get_client_credentials()
        if not client_id:
            return None, "Google OAuth Client ID is missing. Add GOOGLE_CLIENT_ID to .env or provide client_secrets.json."

        params = {
            "client_id": client_id,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{self.OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return url, None

    def start_local_auth_server(self, timeout_seconds: int = 120) -> Tuple[bool, str]:
        """Runs local HTTP server on port 8080 to capture OAuth code and exchanges it for tokens."""
        client_id, client_secret = self._get_client_credentials()
        if not client_id or not client_secret:
            return False, "Google credentials not configured."

        OAuthCallbackHandler.auth_code = None
        OAuthCallbackHandler.error = None

        server_address = ("127.0.0.1", 8080)
        try:
            httpd = HTTPServer(server_address, OAuthCallbackHandler)
            httpd.timeout = 2.0
        except OSError as e:
            return False, f"Could not bind to port 8080 on localhost: {e}"

        start_time = time.time()
        logger.info(f"Awaiting OAuth callback on {self.REDIRECT_URI}...")

        while time.time() - start_time < timeout_seconds:
            httpd.handle_request()
            if OAuthCallbackHandler.auth_code:
                break
            if OAuthCallbackHandler.error:
                return False, f"OAuth error: {OAuthCallbackHandler.error}"

        httpd.server_close()

        if not OAuthCallbackHandler.auth_code:
            return False, "OAuth authorization timed out. User did not complete login in time."

        # Exchange authorization code for tokens
        return self._exchange_code_for_tokens(OAuthCallbackHandler.auth_code, client_id, client_secret)

    def _exchange_code_for_tokens(self, code: str, client_id: str, client_secret: str) -> Tuple[bool, str]:
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": self.REDIRECT_URI,
            "grant_type": "authorization_code",
        }
        try:
            resp = requests.post(self.OAUTH_TOKEN_URL, data=data, timeout=30)
            if resp.status_code != 200:
                return False, f"Token exchange failed ({resp.status_code}): {resp.text}"

            token_data = resp.json()
            token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)

            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as f:
                json.dump(token_data, f, indent=2)

            logger.info("Successfully obtained and saved Google Drive OAuth tokens.")
            return True, "Google Drive authorization successful!"
        except Exception as e:
            return False, f"Failed exchanging code: {e}"

    def get_valid_access_token(self) -> Optional[str]:
        """Returns active access token, refreshing automatically via refresh_token if expired."""
        if not self.token_path.exists():
            return None

        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                token_data = json.load(f)

            expires_at = token_data.get("expires_at", 0)
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")

            # Check if expired or about to expire in 60s
            if time.time() < (expires_at - 60) and access_token:
                return access_token

            # Refresh token
            if not refresh_token:
                return None

            client_id, client_secret = self._get_client_credentials()
            if not client_id or not client_secret:
                return None

            refresh_data = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            resp = requests.post(self.OAUTH_TOKEN_URL, data=refresh_data, timeout=30)
            if resp.status_code == 200:
                new_data = resp.json()
                token_data["access_token"] = new_data["access_token"]
                token_data["expires_at"] = time.time() + new_data.get("expires_in", 3600)
                if "refresh_token" in new_data:
                    token_data["refresh_token"] = new_data["refresh_token"]

                with open(self.token_path, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, indent=2)

                return token_data["access_token"]
            else:
                logger.error(f"Failed to refresh Google OAuth token ({resp.status_code}): {resp.text}")
                return None

        except Exception as e:
            logger.error(f"Error reading Google token file: {e}")
            return None

    def get_or_create_folder(self, folder_name: str, parent_id: Optional[str] = None) -> Optional[str]:
        """Finds or creates a folder on Google Drive in an idempotent manner."""
        cache_key = f"{parent_id or 'root'}/{folder_name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        token = self.get_valid_access_token()
        if not token:
            logger.error("Cannot query Drive folder: No valid access token.")
            return None

        headers = {"Authorization": f"Bearer {token}"}
        query_parts = [
            f"name = '{folder_name}'",
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false",
        ]
        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")

        q = " and ".join(query_parts)
        url = f"{self.DRIVE_API_BASE}/files"
        params = {"q": q, "fields": "files(id, name)", "pageSize": 1}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                files = resp.json().get("files", [])
                if files:
                    folder_id = files[0]["id"]
                    self._folder_cache[cache_key] = folder_id
                    return folder_id

            # Folder does not exist -> Create it
            body: Dict[str, Any] = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                body["parents"] = [parent_id]

            create_resp = requests.post(url, headers=headers, json=body, timeout=30)
            if create_resp.status_code in (200, 201):
                folder_id = create_resp.json()["id"]
                self._folder_cache[cache_key] = folder_id
                logger.info(f"Created Google Drive folder: '{folder_name}' (ID: {folder_id})")
                return folder_id
            else:
                logger.error(f"Failed to create Drive folder '{folder_name}': {create_resp.text}")
                return None

        except Exception as e:
            logger.error(f"Drive folder query/creation failed: {e}")
            return None

    def upload_image(
        self,
        local_file_path: Path,
        channel_subfolder: str,
        video_identifier: str,
        overwrite: bool = False,
    ) -> DriveUploadResult:
        """Uploads a local image file to Google Drive under YouTube-Automation/{Channel}/{Video}/."""
        if not local_file_path.exists():
            return DriveUploadResult(success=False, error_message=f"Local file not found: {local_file_path}")

        token = self.get_valid_access_token()
        if not token:
            return DriveUploadResult(
                success=False,
                error_message="Google Drive API is not authorized. Please authorize via dashboard.",
            )

        # 1. Resolve folder hierarchy: YouTube-Automation -> Channel -> Video
        root_id = self.get_or_create_folder("YouTube-Automation")
        if not root_id:
            return DriveUploadResult(success=False, error_message="Could not resolve root 'YouTube-Automation' folder on Drive.")

        channel_id = self.get_or_create_folder(channel_subfolder, parent_id=root_id)
        if not channel_id:
            return DriveUploadResult(success=False, error_message=f"Could not resolve channel folder '{channel_subfolder}' on Drive.")

        video_id = self.get_or_create_folder(video_identifier, parent_id=channel_id)
        if not video_id:
            return DriveUploadResult(success=False, error_message=f"Could not resolve video folder '{video_identifier}' on Drive.")

        filename = local_file_path.name

        # 2. Duplicate Check: see if file with identical name already exists in target folder
        headers = {"Authorization": f"Bearer {token}"}
        check_q = f"name = '{filename}' and '{video_id}' in parents and trashed = false"
        check_resp = requests.get(
            f"{self.DRIVE_API_BASE}/files",
            headers=headers,
            params={"q": check_q, "fields": "files(id, name, webViewLink)"},
            timeout=30,
        )

        if check_resp.status_code == 200:
            existing = check_resp.json().get("files", [])
            if existing and not overwrite:
                f_id = existing[0]["id"]
                w_link = existing[0].get("webViewLink")
                logger.info(f"File '{filename}' already exists on Google Drive (ID: {f_id}). Skipping re-upload.")
                return DriveUploadResult(
                    success=True,
                    file_id=f_id,
                    web_link=w_link,
                    is_duplicate=True,
                )

        # 3. Multipart Upload
        metadata = {
            "name": filename,
            "parents": [video_id],
        }

        # Determine MIME type
        ext = local_file_path.suffix.lower()
        mime_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        mime_type = mime_types.get(ext, "application/octet-stream")

        try:
            with open(local_file_path, "rb") as f:
                file_bytes = f.read()

            files = {
                "data": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
                "file": (filename, file_bytes, mime_type),
            }

            upload_url = f"{self.DRIVE_UPLOAD_BASE}/files?uploadType=multipart&fields=id,name,webViewLink"
            upload_resp = requests.post(upload_url, headers=headers, files=files, timeout=60)

            if upload_resp.status_code in (200, 201):
                data = upload_resp.json()
                uploaded_id = data["id"]
                web_link = data.get("webViewLink")
                logger.info(f"Google Drive upload confirmed: '{filename}' (ID: {uploaded_id})")
                return DriveUploadResult(
                    success=True,
                    file_id=uploaded_id,
                    web_link=web_link,
                )
            else:
                err = f"Drive upload failed ({upload_resp.status_code}): {upload_resp.text}"
                logger.error(err)
                return DriveUploadResult(success=False, error_message=err)

        except Exception as e:
            err = f"Exception during Drive upload: {e}"
            logger.error(err)
            return DriveUploadResult(success=False, error_message=err)
