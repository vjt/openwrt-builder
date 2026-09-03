import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from state import StateManager, MAX_BUILD_ATTEMPTS


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


def _fail_n_times(sm: StateManager, name: str, commit: str, n: int):
    for _ in range(n):
        sm.record_failure(name, commit, "boom")


def test_failed_build_is_retried_up_to_the_attempt_limit(state_file):
    sm = StateManager(str(state_file))
    for attempt in range(1, MAX_BUILD_ATTEMPTS):
        sm.record_failure("pkg", "abc", "boom")
        assert sm.has_changed("pkg", "abc") is True, f"attempt {attempt}"


def test_failed_build_stops_retrying_after_the_attempt_limit(state_file):
    sm = StateManager(str(state_file))
    _fail_n_times(sm, "pkg", "abc", MAX_BUILD_ATTEMPTS)
    assert sm.has_changed("pkg", "abc") is False
    assert sm.retries_exhausted("pkg") is True


def test_new_commit_rebuilds_even_after_exhausted_retries(state_file):
    sm = StateManager(str(state_file))
    _fail_n_times(sm, "pkg", "abc", MAX_BUILD_ATTEMPTS)
    assert sm.has_changed("pkg", "def") is True


def test_new_commit_resets_the_failure_budget(state_file):
    sm = StateManager(str(state_file))
    _fail_n_times(sm, "pkg", "abc", MAX_BUILD_ATTEMPTS)
    sm.record_failure("pkg", "def", "boom")
    assert sm.has_changed("pkg", "def") is True
    assert sm.retries_exhausted("pkg") is False


def test_success_resets_the_failure_budget(state_file):
    sm = StateManager(str(state_file))
    _fail_n_times(sm, "pkg", "abc", MAX_BUILD_ATTEMPTS)
    sm.record_success("pkg", "abc")
    sm.record_failure("pkg", "abc", "boom")
    assert sm.has_changed("pkg", "abc") is True


def test_build_verdict_is_not_retried_at_all(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("pkg", "abc", "compile error", retriable=False)
    assert sm.has_changed("pkg", "abc") is False
    assert sm.retries_exhausted("pkg") is True


def test_build_verdict_still_rebuilds_on_a_new_commit(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("pkg", "abc", "compile error", retriable=False)
    assert sm.has_changed("pkg", "def") is True


def test_failures_are_retriable_by_default(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("pkg", "abc", "ssh timeout")
    assert sm.has_changed("pkg", "abc") is True


def test_pre_existing_entries_without_the_flag_stay_retriable(state_file):
    sm = StateManager(str(state_file))
    sm.data["pkg"] = {"last_commit": "abc", "status": "failed"}
    assert sm.has_changed("pkg", "abc") is True


def test_retries_exhausted_is_false_for_healthy_repo(state_file):
    sm = StateManager(str(state_file))
    assert sm.retries_exhausted("pkg") is False
    sm.record_success("pkg", "abc")
    assert sm.retries_exhausted("pkg") is False


def test_poll_failure_does_not_trigger_a_rebuild(state_file):
    sm = StateManager(str(state_file))
    sm.record_success("pkg", "abc")
    sm.record_poll_failure("pkg", "github unreachable")
    assert sm.has_changed("pkg", "abc") is False
    assert sm.get_repo("pkg")["status"] == "ok"
    assert sm.get_repo("pkg")["last_commit"] == "abc"


def test_poll_failure_notifies_once_per_distinct_error(state_file):
    sm = StateManager(str(state_file))
    assert sm.record_poll_failure("pkg", "github unreachable") is True
    assert sm.record_poll_failure("pkg", "github unreachable") is False
    assert sm.record_poll_failure("pkg", "auth denied") is True


def test_poll_failure_on_never_polled_repo_still_builds_once_healthy(state_file):
    sm = StateManager(str(state_file))
    sm.record_poll_failure("pkg", "github unreachable")
    assert sm.has_changed("pkg", "abc") is True
    assert sm.retries_exhausted("pkg") is False


def test_clear_poll_failure_rearms_the_notification(state_file):
    sm = StateManager(str(state_file))
    sm.record_poll_failure("pkg", "github unreachable")
    sm.clear_poll_failure("pkg")
    assert "poll_error" not in sm.get_repo("pkg")
    assert sm.record_poll_failure("pkg", "github unreachable") is True


def test_clear_poll_failure_is_a_noop_for_healthy_repo(state_file):
    sm = StateManager(str(state_file))
    sm.record_success("pkg", "abc")
    sm.clear_poll_failure("pkg")
    assert sm.get_repo("pkg")["status"] == "ok"
