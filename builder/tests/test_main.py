import pytest

import main
from state import StateManager


class FakeBuilder:
    """Stand-in for PackageBuilder: records what the cycle asked it to do.

    `poll_error` / `build_error` let a test choose which stage blows up,
    mirroring the two real failure classes (git fetch vs SDK compile).
    """

    def __init__(self, commit: str, poll_error: str | None = None,
                 build_error: str | None = None):
        self.commit = commit
        self.poll_error = poll_error
        self.build_error = build_error
        self.polls = 0
        self.builds = 0

    def install(self, monkeypatch):
        outer = self

        class _Factory:
            def __init__(self, **kwargs):
                pass

            def clone_or_fetch(self):
                outer.polls += 1
                if outer.poll_error:
                    raise RuntimeError(outer.poll_error)

            def get_head_commit(self):
                return outer.commit

            async def build_all_targets(self):
                outer.builds += 1
                if outer.build_error:
                    raise RuntimeError(outer.build_error)
                return {}

        monkeypatch.setattr(main, "PackageBuilder", _Factory)
        return self


@pytest.fixture
def config():
    return {"repos": [{"name": "pkg", "openwrt_versions": ["24.10.0"]}]}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path / "logs"))
    return StateManager(str(tmp_path / "state.json"))


async def run(config, state):
    await main.run_build_cycle(config, state, sdk_mgrs={})


@pytest.mark.asyncio
async def test_poll_failure_leaves_build_state_untouched(config, state,
                                                         monkeypatch):
    state.record_success("pkg@24.10.0", "abc123")
    FakeBuilder("abc123", poll_error="could not read Username").install(
        monkeypatch)

    await run(config, state)

    repo = state.get_repo("pkg@24.10.0")
    assert repo["status"] == "ok"
    assert repo["last_commit"] == "abc123"


@pytest.mark.asyncio
async def test_poll_failure_does_not_rebuild_unchanged_repo_next_cycle(
        config, state, monkeypatch):
    """The github-outage regression: an unreachable remote must not queue a
    rebuild of a repo whose commit never moved."""
    state.record_success("pkg@24.10.0", "abc123")

    outage = FakeBuilder("abc123", poll_error="could not read Username").install(
        monkeypatch)
    await run(config, state)
    assert outage.builds == 0

    recovered = FakeBuilder("abc123").install(monkeypatch)
    await run(config, state)
    assert recovered.builds == 0


@pytest.mark.asyncio
async def test_poll_failure_still_builds_once_remote_is_reachable(
        config, state, monkeypatch):
    FakeBuilder("abc123", poll_error="could not read Username").install(
        monkeypatch)
    await run(config, state)

    recovered = FakeBuilder("abc123").install(monkeypatch)
    await run(config, state)

    assert recovered.builds == 1
    assert state.get_repo("pkg@24.10.0")["status"] == "ok"


@pytest.mark.asyncio
async def test_build_failure_records_the_real_commit(config, state,
                                                     monkeypatch):
    FakeBuilder("abc123", build_error="compile error").install(monkeypatch)

    await run(config, state)

    repo = state.get_repo("pkg@24.10.0")
    assert repo["status"] == "failed"
    assert repo["last_commit"] == "abc123"
    assert repo["error"] == "compile error"
