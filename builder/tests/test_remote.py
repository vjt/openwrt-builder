import json
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path

from remote import RemoteBuilder


@pytest.fixture
def builder():
    return RemoteBuilder(
        api_token="test-token-123",
        ssh_key_name="openwrt-builder",
        ssh_key_path="/runtime/ssh.key",
        server_type="cx22",
        locations=["fsn1"],
    )


def _mock_hcloud_create_response(ip="1.2.3.4"):
    return json.dumps({
        "server": {
            "public_net": {
                "ipv4": {"ip": ip}
            }
        }
    })


class TestEnsureServer:
    def test_creates_server_via_hcloud(self, builder):
        with patch.object(builder, "_hcloud") as mock_hcloud, \
             patch.object(builder, "_wait_for_ssh"):
            mock_hcloud.return_value = MagicMock(
                returncode=0,
                stdout=_mock_hcloud_create_response("5.6.7.8"),
            )

            ip = builder.ensure_server()

            assert ip == "5.6.7.8"
            assert builder._server_ip == "5.6.7.8"
            assert builder._server_name is not None
            assert builder._server_name.startswith("openwrt-builder-")

            # Verify hcloud was called with correct args
            call_args = mock_hcloud.call_args[0]
            assert "server" in call_args
            assert "create" in call_args
            assert "--type" in call_args
            assert "cx22" in call_args

    def test_reuses_existing_server(self, builder):
        builder._server_ip = "1.1.1.1"
        builder._server_name = "existing"

        with patch.object(builder, "_hcloud") as mock_hcloud:
            ip = builder.ensure_server()
            assert ip == "1.1.1.1"
            mock_hcloud.assert_not_called()

    def test_raises_on_create_failure(self, builder):
        with patch.object(builder, "_hcloud") as mock_hcloud:
            mock_hcloud.return_value = MagicMock(
                returncode=1,
                stderr="quota exceeded",
            )

            with pytest.raises(RuntimeError, match="Failed to create server"):
                builder.ensure_server()


class TestDestroyServer:
    def test_destroys_server(self, builder):
        builder._server_name = "openwrt-builder-12345"
        builder._server_ip = "1.2.3.4"
        builder._setup_done.add("mediatek/filogic")

        with patch.object(builder, "_hcloud") as mock_hcloud:
            mock_hcloud.return_value = MagicMock(returncode=0)
            builder.destroy_server()

            mock_hcloud.assert_called_once_with(
                "server", "delete", "openwrt-builder-12345"
            )
            assert builder._server_name is None
            assert builder._server_ip is None
            assert len(builder._setup_done) == 0

    def test_noop_when_no_server(self, builder):
        with patch.object(builder, "_hcloud") as mock_hcloud:
            builder.destroy_server()
            mock_hcloud.assert_not_called()


class TestSSH:
    def test_ssh_raises_without_server(self, builder):
        with pytest.raises(RuntimeError, match="No server running"):
            builder._ssh("ls")

    def test_ssh_uses_correct_options(self, builder):
        builder._server_ip = "1.2.3.4"

        with patch("remote.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            builder._ssh("echo", "hello", check=False)

            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "ssh"
            assert "-i" in cmd
            assert "/runtime/ssh.key" in cmd
            assert "root@1.2.3.4" in cmd
            assert "echo" in cmd
            assert "hello" in cmd


class TestSCP:
    def test_scp_raises_without_server(self, builder):
        with pytest.raises(RuntimeError, match="No server running"):
            builder._scp_from("/remote/file", "/local/file")


class TestSyncRepo:
    def test_sync_repo_rsyncs_local_clone(self, builder, tmp_path):
        builder._server_ip = "1.2.3.4"

        with patch.object(builder, "_ssh") as mock_ssh, \
             patch("remote.subprocess.run") as mock_run:
            mock_ssh.return_value = MagicMock(returncode=0)
            mock_run.return_value = MagicMock(returncode=0)

            builder.sync_repo(str(tmp_path / "myrepo"), "myrepo")

            # rsync should be called
            rsync_call = mock_run.call_args
            cmd = rsync_call[0][0]
            assert cmd[0] == "rsync"
            assert "root@1.2.3.4:/tmp/src/myrepo/" in cmd[-1]


class TestSetupSDK:
    def test_setup_sdk_runs_script(self, builder):
        builder._server_ip = "1.2.3.4"

        with patch.object(builder, "_ssh_script") as mock_ssh:
            mock_ssh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            path = builder.setup_sdk("mediatek/filogic", "24.10.0")

            assert "openwrt-sdk-24.10.0-mediatek-filogic" in path
            assert ("mediatek/filogic", "24.10.0") in builder._setup_done
            mock_ssh.assert_called_once()

    def test_setup_sdk_skips_if_done(self, builder):
        builder._server_ip = "1.2.3.4"
        builder._setup_done.add(("mediatek/filogic", "24.10.0"))

        with patch.object(builder, "_ssh_script") as mock_ssh:
            builder.setup_sdk("mediatek/filogic", "24.10.0")
            mock_ssh.assert_not_called()

    def test_setup_sdk_all_uses_first_target(self, builder):
        builder._server_ip = "1.2.3.4"

        with patch.object(builder, "_ssh_script") as mock_ssh:
            mock_ssh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            path = builder.setup_sdk("all", "24.10.0")

            # Should use the first target from TARGET_ARCH_MAP
            assert "mediatek-filogic" in path


class TestBuildPackage:
    def test_build_returns_package_paths(self, builder):
        builder._server_ip = "1.2.3.4"
        builder._setup_done.add(("mediatek/filogic", "24.10.0"))

        with patch.object(builder, "_ssh_script") as mock_ssh:
            mock_ssh.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "===PKG_LIST_START===\n"
                    "/opt/sdk/xxx/bin/packages/aarch64/base/test-pkg_1.0_all.ipk\n"
                    "/opt/sdk/xxx/bin/packages/aarch64/base/test-pkg_1.0_all.apk\n"
                    "===PKG_LIST_END===\n"
                ),
                stderr="",
            )
            paths = builder.build_package(
                name="test-pkg",
                makefile_subdir=None,
                target="mediatek/filogic",
                openwrt_version="24.10.0",
            )

            assert len(paths) == 2
            assert any(p.endswith(".ipk") for p in paths)
            assert any(p.endswith(".apk") for p in paths)

    def test_build_raises_on_failure(self, builder):
        builder._server_ip = "1.2.3.4"
        builder._setup_done.add(("mediatek/filogic", "24.10.0"))

        with patch.object(builder, "_ssh_script") as mock_ssh:
            mock_ssh.return_value = MagicMock(
                returncode=2,
                stdout="",
                stderr="fatal: something broke",
            )
            with pytest.raises(RuntimeError, match="Remote SDK build failed"):
                builder.build_package(
                    name="test-pkg",
                    makefile_subdir=None,
                    target="mediatek/filogic",
                    openwrt_version="24.10.0",
                )


class TestDownloadIPKs:
    def test_download_copies_files(self, builder, tmp_path):
        builder._server_ip = "1.2.3.4"

        with patch.object(builder, "_scp_from") as mock_scp:
            result = builder.download_ipks(
                ["/remote/test-pkg_1.0_all.ipk"],
                str(tmp_path / "feed"),
            )

            assert len(result) == 1
            assert result[0].name == "test-pkg_1.0_all.ipk"
            mock_scp.assert_called_once()


class TestHcloudEnv:
    def test_hcloud_passes_token_in_env(self, builder):
        with patch("remote.subprocess.run") as mock_run, \
             patch("remote.os.environ", {"PATH": "/usr/bin"}):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            builder._hcloud("server", "list")

            _, kwargs = mock_run.call_args
            assert kwargs["env"]["HCLOUD_TOKEN"] == "test-token-123"
            assert kwargs["env"]["PATH"] == "/usr/bin"
