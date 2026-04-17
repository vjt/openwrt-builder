import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path

from builder import PackageBuilder, _is_openwrt_makefile


OPENWRT_MAKEFILE = """
include $(TOPDIR)/rules.mk
PKG_NAME:=test-pkg
include $(INCLUDE_DIR)/package.mk
$(eval $(call BuildPackage,test-pkg))
"""


@pytest.fixture
def feed_dir(tmp_path):
    d = tmp_path / "feed"
    d.mkdir()
    return d


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "repos"
    d.mkdir()
    return d


_TEST_VERSION = "24.10.0"


def _make_builder(repo_dir, feed_dir=None, name="test-pkg"):
    repo_config = {
        "name": name,
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
        "openwrt_version": _TEST_VERSION,
    }
    pkg_dir = repo_dir / name
    pkg_dir.mkdir(exist_ok=True)
    return PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir=str(feed_dir or repo_dir / "feed"),
        sdk_managers={_TEST_VERSION: MagicMock()},
    ), pkg_dir


def test_clone_new_repo(repo_dir):
    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
        "openwrt_version": _TEST_VERSION,
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_managers={_TEST_VERSION: MagicMock()},
    )
    with patch("builder.subprocess") as mock_sub:
        mock_sub.run.return_value = MagicMock(returncode=0)
        pb.clone_or_fetch()
        mock_sub.run.assert_called()
        clone_call = mock_sub.run.call_args_list[0]
        assert "clone" in clone_call[0][0]


def test_fetch_existing_repo(repo_dir):
    pkg_dir = repo_dir / "test-pkg"
    pkg_dir.mkdir()
    (pkg_dir / ".git").mkdir()

    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
        "openwrt_version": _TEST_VERSION,
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_managers={_TEST_VERSION: MagicMock()},
    )
    with patch("builder.subprocess") as mock_sub:
        mock_sub.run.return_value = MagicMock(returncode=0)
        mock_sub.check_output.return_value = b"abc123\n"
        pb.clone_or_fetch()
        calls = [str(c) for c in mock_sub.run.call_args_list]
        assert any("fetch" in c for c in calls)


def test_get_head_commit(repo_dir):
    pkg_dir = repo_dir / "test-pkg"
    pkg_dir.mkdir()

    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
        "openwrt_version": _TEST_VERSION,
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_managers={_TEST_VERSION: MagicMock()},
    )
    with patch("builder.subprocess") as mock_sub:
        mock_sub.check_output.return_value = b"abc123def\n"
        commit = pb.get_head_commit()
        assert commit == "abc123def"


def test_collect_ipk_files(tmp_path):
    """Test that _collect_ipk_files finds .ipk files in bin/."""
    bin_dir = tmp_path / "bin" / "packages" / "aarch64_cortex-a53" / "myfeed"
    bin_dir.mkdir(parents=True)
    (bin_dir / "test-pkg_1.0-1_aarch64_cortex-a53.ipk").touch()
    (bin_dir / "test-pkg2_1.0-1_aarch64_cortex-a53.ipk").touch()

    from builder import _collect_ipk_files
    ipks = _collect_ipk_files(str(tmp_path / "bin"))
    assert len(ipks) == 2
    assert all(str(p).endswith(".ipk") for p in ipks)


# --- Build method detection tests ---

def test_is_openwrt_makefile(tmp_path):
    mf = tmp_path / "Makefile"
    mf.write_text(OPENWRT_MAKEFILE)
    assert _is_openwrt_makefile(mf) is True


def test_is_not_openwrt_makefile(tmp_path):
    mf = tmp_path / "Makefile"
    mf.write_text("all:\n\techo hello\n")
    assert _is_openwrt_makefile(mf) is False


def test_detect_openwrt_makefile_at_root(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)
    (pkg_dir / "Makefile").write_text(OPENWRT_MAKEFILE)

    method, makefile_dir = pb._detect_build_method()
    assert method == "sdk"
    assert makefile_dir == pkg_dir


def test_detect_openwrt_makefile_in_subdir(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)
    openwrt_dir = pkg_dir / "openwrt"
    openwrt_dir.mkdir()
    (openwrt_dir / "Makefile").write_text(OPENWRT_MAKEFILE)

    method, makefile_dir = pb._detect_build_method()
    assert method == "sdk"
    assert makefile_dir == openwrt_dir


def test_detect_openwrt_makefile_in_feed_layout(repo_dir):
    """Blessed src-link feed layout: <repo>/openwrt/<pkgname>/Makefile."""
    pb, pkg_dir = _make_builder(repo_dir)
    feed_pkg_dir = pkg_dir / "openwrt" / "my-package"
    feed_pkg_dir.mkdir(parents=True)
    (feed_pkg_dir / "Makefile").write_text(OPENWRT_MAKEFILE)

    method, makefile_dir = pb._detect_build_method()
    assert method == "sdk"
    assert makefile_dir == feed_pkg_dir


def test_detect_openwrt_legacy_beats_feed_layout(repo_dir):
    """If both <repo>/openwrt/Makefile and <repo>/openwrt/<pkg>/Makefile exist,
    prefer the legacy top-level one (keeps single-package repos deterministic
    when someone accidentally adds an unrelated subdir)."""
    pb, pkg_dir = _make_builder(repo_dir)
    openwrt_dir = pkg_dir / "openwrt"
    openwrt_dir.mkdir()
    (openwrt_dir / "Makefile").write_text(OPENWRT_MAKEFILE)
    sub_dir = openwrt_dir / "extra"
    sub_dir.mkdir()
    (sub_dir / "Makefile").write_text(OPENWRT_MAKEFILE)

    method, makefile_dir = pb._detect_build_method()
    assert method == "sdk"
    assert makefile_dir == openwrt_dir


def test_detect_build_script(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)
    (pkg_dir / "build-ipk.sh").write_text("#!/bin/bash\necho hi\n")

    method, makefile_dir = pb._detect_build_method()
    assert method == "script"
    assert makefile_dir is None


def test_detect_control_dir(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)
    (pkg_dir / "CONTROL").mkdir()

    method, makefile_dir = pb._detect_build_method()
    assert method == "opkg-build"
    assert makefile_dir is None


def test_detect_nothing_skips(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)

    method, makefile_dir = pb._detect_build_method()
    assert method == "skip"
    assert makefile_dir is None


def test_detect_non_openwrt_makefile_ignored(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)
    (pkg_dir / "Makefile").write_text("all:\n\techo hello\n")

    method, makefile_dir = pb._detect_build_method()
    assert method == "skip"


def test_detect_priority_sdk_over_script(repo_dir):
    pb, pkg_dir = _make_builder(repo_dir)
    (pkg_dir / "Makefile").write_text(OPENWRT_MAKEFILE)
    (pkg_dir / "build-ipk.sh").write_text("#!/bin/bash\n")

    method, _ = pb._detect_build_method()
    assert method == "sdk"


def test_pick_any_sdk_target_prefers_cached(repo_dir):
    pb, _ = _make_builder(repo_dir)
    sdk_mgr = pb.sdk_manager
    # First target needs download, second is cached
    sdk_mgr.needs_download.side_effect = lambda t: t != "ramips/mt7621"

    target = pb._pick_any_sdk_target()
    assert target == "ramips/mt7621"


@pytest.mark.asyncio
async def test_build_for_target_skip_returns_empty(repo_dir):
    pb, _ = _make_builder(repo_dir)

    result = await pb.build_for_target("all")
    assert result == []
