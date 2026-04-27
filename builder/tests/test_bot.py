import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import format_status_message, format_list_message


def test_format_status_message_empty():
    msg = format_status_message({}, "2025-01-15T10:00:00Z")
    assert "No builds yet" in msg


def test_format_status_message_with_repos():
    state_data = {
        "pkg-a": {
            "last_commit": "abc123",
            "last_build": "2025-01-15T10:30:00Z",
            "status": "ok",
        },
        "pkg-b": {
            "last_commit": "def456",
            "last_build": "2025-01-15T10:31:00Z",
            "status": "failed",
            "error": "compile error",
        },
    }
    msg = format_status_message(state_data, "2025-01-15T11:00:00Z")
    assert "pkg-a" in msg
    assert "pkg-b" in msg
    assert "OK" in msg
    assert "FAIL" in msg


def test_format_list_message():
    repos = [
        {"name": "pkg-a", "url": "https://github.com/test/a.git",
         "branch": "main", "openwrt_versions": ["25.12.0"]},
        {"name": "pkg-b", "url": "https://github.com/test/b.git",
         "branch": "dev", "openwrt_versions": ["24.10.0", "25.12.0"]},
    ]
    state_data = {
        "pkg-a@25.12.0": {"last_commit": "abc1234567890"},
        "pkg-b@24.10.0": {"last_commit": "def4567890123"},
    }
    msg = format_list_message(repos, state_data)
    assert "pkg-a@25.12.0" in msg
    assert "pkg-b@24.10.0" in msg
    assert "pkg-b@25.12.0" in msg
    assert "abc1234" in msg
    assert "def4567" in msg
    assert "not built" in msg  # pkg-b@25.12.0 missing
