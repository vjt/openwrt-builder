import os
import shutil
from pathlib import Path

import pytest

import main
from state import StateManager


class FakeBuilder:
    """Stand-in for PackageBuilder: records what the cycle asked it to do.

    `poll_error` / `build_error` let a test choose which stage blows up,
    mirroring the two real failure classes (git fetch vs SDK compile).
    """

    def __init__(self, commit: str, poll_error: str | None = None,
                 build_error: str | Exception | None = None):
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
                if isinstance(outer.build_error, Exception):
                    raise outer.build_error
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


@pytest.mark.asyncio
async def test_build_failure_stops_retrying_the_same_commit(config, state,
                                                            monkeypatch):
    from state import MAX_BUILD_ATTEMPTS

    attempts = 0
    for _ in range(MAX_BUILD_ATTEMPTS + 2):
        fb = FakeBuilder("abc123", build_error="compile error").install(
            monkeypatch)
        await run(config, state)
        attempts += fb.builds

    assert attempts == MAX_BUILD_ATTEMPTS


@pytest.mark.asyncio
async def test_a_build_verdict_is_not_retried_on_the_same_commit(
        config, state, monkeypatch):
    """A compile error is deterministic: retrying it just rents another
    build server to reproduce the same failure."""
    from errors import PackageBuildError

    first = FakeBuilder("abc123", build_error=PackageBuildError("no")).install(
        monkeypatch)
    await run(config, state)
    assert first.builds == 1

    second = FakeBuilder("abc123",
                         build_error=PackageBuildError("no")).install(monkeypatch)
    await run(config, state)
    assert second.builds == 0


@pytest.mark.asyncio
async def test_infrastructure_failures_are_still_retried(config, state,
                                                         monkeypatch):
    first = FakeBuilder("abc123", build_error="ssh timeout").install(monkeypatch)
    await run(config, state)
    assert first.builds == 1

    second = FakeBuilder("abc123", build_error="ssh timeout").install(monkeypatch)
    await run(config, state)
    assert second.builds == 1


@pytest.mark.asyncio
async def test_new_commit_rebuilds_after_exhausted_retries(config, state,
                                                           monkeypatch):
    from state import MAX_BUILD_ATTEMPTS

    for _ in range(MAX_BUILD_ATTEMPTS):
        FakeBuilder("abc123", build_error="compile error").install(monkeypatch)
        await run(config, state)

    fixed = FakeBuilder("def456").install(monkeypatch)
    await run(config, state)

    assert fixed.builds == 1
    assert state.get_repo("pkg@24.10.0")["status"] == "ok"


FIXTURES = Path(__file__).parent / "fixtures"
NOARCH_APK = "wifi-dethrash-collector-0.4.0-r1.apk"
AARCH64_APK = "quectel-5g-tools-1.2.0-r0.apk"  # arch-specific, from targets: [all]


def make_feed(tmp_path, *apks):
    """A feed with all/ holding `apks` and empty aarch64 + mipsel dirs,
    queued for reindex the way a cycle that built an all-target repo does."""
    feed = tmp_path / "feed"
    (feed / "all").mkdir(parents=True)
    for name in apks:
        shutil.copy(FIXTURES / name, feed / "all" / name)
    for arch in ("aarch64_cortex-a53", "mipsel_24kc"):
        (feed / arch).mkdir()
    reindex = {str(feed / "all"): ("25.12.0", "all")}
    return feed, reindex


def test_noarch_apk_is_mirrored_into_every_arch_feed(tmp_path):
    feed, reindex = make_feed(tmp_path, NOARCH_APK)

    main.mirror_all_apks(feed, reindex)

    for arch in ("aarch64_cortex-a53", "mipsel_24kc"):
        assert (feed / arch / NOARCH_APK).exists()
        assert str(feed / arch) in reindex


def test_arch_specific_apk_only_lands_in_its_own_arch_feed(tmp_path):
    feed, reindex = make_feed(tmp_path, AARCH64_APK)

    main.mirror_all_apks(feed, reindex)

    assert (feed / "aarch64_cortex-a53" / AARCH64_APK).exists()
    assert not (feed / "mipsel_24kc" / AARCH64_APK).exists()


def test_stale_wrong_arch_mirror_is_removed_and_reindexed(tmp_path):
    feed, reindex = make_feed(tmp_path, AARCH64_APK)
    stale = feed / "mipsel_24kc" / AARCH64_APK
    os.link(feed / "all" / AARCH64_APK, stale)  # left by an earlier cycle

    main.mirror_all_apks(feed, reindex)

    assert not stale.exists()
    assert str(feed / "mipsel_24kc") in reindex


def test_same_named_apk_built_for_that_arch_is_left_alone(tmp_path):
    feed, reindex = make_feed(tmp_path, AARCH64_APK)
    own_build = feed / "mipsel_24kc" / AARCH64_APK
    own_build.write_bytes(b"a real per-arch build, not our mirror")

    main.mirror_all_apks(feed, reindex)

    assert own_build.read_bytes() == b"a real per-arch build, not our mirror"
