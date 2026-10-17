import pytest
from unittest.mock import patch, MagicMock
from src.notion_client import NotionClient, NotionPageSummary


def test_notion_test_connection_success():
    client = NotionClient(api_key="secret_test_key_valid_length")
    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"name": "AutomationBot", "type": "bot"}
        mock_req.return_value = mock_resp

        ok, msg = client.test_connection()
        assert ok is True
        assert "AutomationBot" in msg


def test_notion_cursor_pagination():
    client = NotionClient(api_key="secret_test_key_valid_length")
    with patch("requests.request") as mock_req:
        # First page returns cursor
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {
            "results": [{"id": "b-1", "type": "paragraph", "has_children": False}],
            "has_more": True,
            "next_cursor": "cur-123",
        }
        # Second page terminates
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {
            "results": [{"id": "b-2", "type": "paragraph", "has_children": False}],
            "has_more": False,
            "next_cursor": None,
        }

        mock_req.side_effect = [resp1, resp2]
        blocks = client.get_all_page_blocks("page-test-id", recursive=False)

        assert len(blocks) == 2
        assert blocks[0]["id"] == "b-1"
        assert blocks[1]["id"] == "b-2"
        assert mock_req.call_count == 2


def test_notion_handles_401_auth_error():
    client = NotionClient(api_key="secret_invalid_key_length")
    with patch("requests.request") as mock_req:
        resp = MagicMock()
        resp.status_code = 401
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"code": "unauthorized", "message": "API token is invalid."}
        mock_req.return_value = resp

        with pytest.raises(PermissionError, match="Authentication Failed"):
            client._request("GET", "/users/me")

        ok, msg = client.test_connection()
        assert ok is False
        assert "Authentication Failed" in msg
