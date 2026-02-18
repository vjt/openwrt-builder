import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path

from builder import PackageBuilder


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


def test_clone_new_repo(repo_dir):
    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_manager=MagicMock(),
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
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_manager=MagicMock(),
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
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_manager=MagicMock(),
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
