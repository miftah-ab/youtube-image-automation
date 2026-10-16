import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel

from src.logging_config import logger


class NotionPageSummary(BaseModel):
    id: str
    title: str
    url: str
    last_edited_time: Optional[str] = None


class NotionClient:
    """Robust client for Notion API v1 handling authentication, page retrieval,
    nested block pagination, and error classification.
    """

    BASE_URL = "https://api.notion.com/v1"
    DEFAULT_API_VERSION = "2022-06-28"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        self.api_key = (api_key or os.getenv("NOTION_API_KEY", "")).strip()
        self.api_version = api_version
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and len(self.api_key) > 10)

    def _get_headers(self) -> Dict[str, str]:
        if not self.is_configured:
            raise ValueError(
                "Notion API token is not configured. Set NOTION_API_KEY in .env or via settings."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.api_version,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{endpoint}"
        headers = self._get_headers()

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=self.timeout,
                )

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning(f"Notion API rate-limited (429). Retrying after {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if resp.status_code in (500, 502, 503, 504) and attempt < self.max_retries:
                    wait_s = 2 ** attempt
                    logger.warning(f"Notion API server error ({resp.status_code}). Retrying in {wait_s}s...")
                    time.sleep(wait_s)
                    continue

                # Specific descriptive error formatting
                error_payload = resp.json() if "application/json" in resp.headers.get("Content-Type", "") else {}
                code = error_payload.get("code", "unknown")
                msg = error_payload.get("message", resp.text)

                if resp.status_code == 401:
                    raise PermissionError(f"Notion Authentication Failed (401): {msg}. Verify your integration token.")
                elif resp.status_code == 404:
                    raise FileNotFoundError(
                        f"Notion resource not found (404): {msg}. "
                        "Ensure the page has been shared with your Notion integration."
                    )
                else:
                    raise RuntimeError(f"Notion API Error ({resp.status_code} {code}): {msg}")

            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self.max_retries:
                    wait_s = 2 ** attempt
                    logger.warning(f"Notion network failure: {e}. Retrying in {wait_s}s...")
                    time.sleep(wait_s)
                else:
                    raise ConnectionError(f"Failed to connect to Notion API after {self.max_retries} attempts: {e}")

        raise RuntimeError("Unexpected exhaustion of retry loop in NotionClient.")

    def test_connection(self) -> Tuple[bool, str]:
        """Tests token validity against the Notion users endpoint."""
        if not self.is_configured:
            return False, "Notion API key is missing or empty."
        try:
            data = self._request("GET", "/users/me")
            bot_name = data.get("name", "Notion Bot")
            return True, f"Successfully authenticated as '{bot_name}'."
        except Exception as e:
            return False, str(e)

    def list_shared_pages(self, query: str = "") -> List[NotionPageSummary]:
        """Lists all pages explicitly shared with this integration."""
        body: Dict[str, Any] = {
            "filter": {"value": "page", "property": "object"},
            "page_size": 100,
        }
        if query:
            body["query"] = query

        data = self._request("POST", "/search", json_data=body)
        results = data.get("results", [])

        pages: List[NotionPageSummary] = []
        for item in results:
            if item.get("object") != "page":
                continue

            page_id = item.get("id", "")
            url = item.get("url", "")
            last_edited = item.get("last_edited_time")

            # Extract title from properties
            title = self._extract_page_title(item)
            pages.append(NotionPageSummary(
                id=page_id,
                title=title or "Untitled Page",
                url=url,
                last_edited_time=last_edited,
            ))

        return pages

    def get_page_title(self, page_id: str) -> str:
        clean_id = page_id.replace("-", "").strip()
        data = self._request("GET", f"/pages/{clean_id}")
        title = self._extract_page_title(data)
        return title or f"Page {clean_id[:8]}"

    def get_all_page_blocks(self, page_id: str, recursive: bool = True) -> List[Dict[str, Any]]:
        """Retrieves all blocks for a page, following cursor pagination and traversing child blocks."""
        clean_id = page_id.replace("-", "").strip()
        return self._fetch_block_children(clean_id, recursive=recursive)

    def _fetch_block_children(self, block_id: str, recursive: bool = True) -> List[Dict[str, Any]]:
        all_blocks: List[Dict[str, Any]] = []
        start_cursor: Optional[str] = None
        has_more = True

        while has_more:
            params: Dict[str, Any] = {"page_size": 100}
            if start_cursor:
                params["start_cursor"] = start_cursor

            data = self._request("GET", f"/blocks/{block_id}/children", params=params)
            blocks = data.get("results", [])
            all_blocks.extend(blocks)

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        if recursive:
            for b in all_blocks:
                if b.get("has_children", False):
                    child_id = b.get("id")
                    if child_id:
                        b["children"] = self._fetch_block_children(child_id, recursive=True)

        return all_blocks

    def _extract_page_title(self, page_obj: Dict[str, Any]) -> str:
        props = page_obj.get("properties", {})
        for prop_name, prop_data in props.items():
            if prop_data.get("type") == "title":
                title_list = prop_data.get("title", [])
                if title_list:
                    return "".join(t.get("plain_text", "") for t in title_list).strip()
        return ""
