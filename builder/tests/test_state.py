import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from state import StateManager


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state.json"


def test_fresh_state_file_created_on_load(state_file):
    sm = StateManager(str(state_file))
    assert state_file.exists()
    assert sm.data == {}


def test_get_repo_returns_none_for_unknown(state_file):
    sm = StateManager(str(state_file))
    assert sm.get_repo("nonexistent") is None


def test_record_success(state_file):
    sm = StateManager(str(state_file))
    sm.record_success("my-pkg", "abc123")
    repo = sm.get_repo("my-pkg")
    assert repo["last_commit"] == "abc123"
    assert repo["status"] == "ok"
    assert "last_build" in repo
    assert "error" not in repo


def test_record_failure(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("my-pkg", "abc123", "compile error")
    repo = sm.get_repo("my-pkg")
    assert repo["status"] == "failed"
    assert repo["error"] == "compile error"
    assert repo["notified"] is True


def test_should_notify_failure_only_first_time(state_file):
    sm = StateManager(str(state_file))
    # First failure: should notify
    sm.record_failure("pkg", "abc", "error")
    assert sm.should_notify_failure("pkg") is False  # already notified in record_failure
    # Simulate: notified flag already set
    repo = sm.get_repo("pkg")
    assert repo["notified"] is True


def test_should_notify_recovery(state_file):
    sm = StateManager(str(state_file))
    # First: fail
    sm.record_failure("pkg", "abc", "error")
    assert sm.should_notify_recovery("pkg") is False
    # Then: succeed
    sm.record_success("pkg", "def")
    # No recovery notification because was_failed flag is set
    assert sm.get_repo("pkg")["status"] == "ok"


def test_record_failure_then_success_clears_error(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("pkg", "abc", "compile error")
    sm.record_success("pkg", "def")
    repo = sm.get_repo("pkg")
    assert repo["status"] == "ok"
    assert "error" not in repo


def test_state_persists_to_disk(state_file):
    sm = StateManager(str(state_file))
    sm.record_success("pkg", "abc123")
    # Load from same file
    sm2 = StateManager(str(state_file))
    assert sm2.get_repo("pkg")["last_commit"] == "abc123"


def test_has_changed_commit(state_file):
    sm = StateManager(str(state_file))
    assert sm.has_changed("pkg", "abc123") is True
    sm.record_success("pkg", "abc123")
    assert sm.has_changed("pkg", "abc123") is False
    assert sm.has_changed("pkg", "def456") is True
