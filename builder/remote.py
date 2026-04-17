from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from config import TARGET_ARCH_MAP
from sdk import sdk_url

logger = logging.getLogger(__name__)

# Packages needed on the remote Hetzner instance for SDK builds
REMOTE_DEPS = (
    "build-essential libncurses-dev gawk git wget curl file rsync "
    "unzip zstd gzip xz-utils ca-certificates python3 python3-dev "
    "python3-setuptools make"
)

# How long to wait for SSH to become available after server creation.
# Hetzner CX instances occasionally take 2+ minutes to boot + run cloud-init
# before sshd accepts connections, so 300s is the safe floor.
SSH_WAIT_TIMEOUT = 300
SSH_WAIT_INTERVAL = 5

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
]


class RemoteBuilder:
    """Manages on-demand Hetzner instances for native x86_64 SDK builds."""

    def __init__(
        self,
        api_token: str,
        ssh_key_name: str,
        ssh_key_path: str = "/runtime/ssh.key",
        server_type: str = "cx23",
        location: str = "fsn1",
    ):
        self.api_token = api_token
        self.ssh_key_name = ssh_key_name
        self.ssh_key_path = ssh_key_path
        self.server_type = server_type
        self.location = location

        self._server_name: str | None = None
        self._server_ip: str | None = None
        # Cache keyed by (target, openwrt_version) — the same target under
        # different OpenWrt versions needs distinct SDKs on the remote host.
        self._setup_done: set[tuple[str, str]] = set()

    def _hcloud(self, *args: str) -> subprocess.CompletedProcess:
        """Run an hcloud CLI command with the API token."""
        env = {**os.environ, "HCLOUD_TOKEN": self.api_token}
        result = subprocess.run(
            ["hcloud", *args],
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def _ssh_script(self, script: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a bash script on the remote server via SSH (piped to stdin)."""
        if not self._server_ip:
            raise RuntimeError("No server running — call ensure_server() first")

        return subprocess.run(
            ["ssh", *SSH_OPTS, "-i", self.ssh_key_path,
             f"root@{self._server_ip}", "bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            check=check,
        )

    def _ssh(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a simple command on the remote server via SSH."""
        if not self._server_ip:
            raise RuntimeError("No server running — call ensure_server() first")

        return subprocess.run(
            ["ssh", *SSH_OPTS, "-i", self.ssh_key_path,
             f"root@{self._server_ip}", *cmd],
            capture_output=True,
            text=True,
            check=check,
        )

    def _scp_from(self, remote_path: str, local_path: str) -> None:
        """Copy a file from the remote server to local."""
        if not self._server_ip:
            raise RuntimeError("No server running — call ensure_server() first")

        subprocess.run(
            ["scp", *SSH_OPTS, "-i", self.ssh_key_path,
             f"root@{self._server_ip}:{remote_path}", local_path],
            check=True,
        )

    def ensure_server(self) -> str:
        """Create a Hetzner server if not already running. Returns the IP."""
        if self._server_ip:
            logger.info("Reusing existing build server %s (%s)",
                        self._server_name, self._server_ip)
            return self._server_ip

        self._server_name = f"openwrt-builder-{int(time.time())}"
        logger.info("Creating build server %s (%s in %s)",
                     self._server_name, self.server_type, self.location)

        result = self._hcloud(
            "server", "create",
            "--name", self._server_name,
            "--type", self.server_type,
            "--image", "debian-12",
            "--ssh-key", self.ssh_key_name,
            "--location", self.location,
            "-o", "json",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create server: {result.stderr.strip()}"
            )

        data = json.loads(result.stdout)
        self._server_ip = data["server"]["public_net"]["ipv4"]["ip"]
        logger.info("Server %s created at %s", self._server_name, self._server_ip)

        # If SSH never comes up (flaky instance, firewall weirdness), the
        # server is useless — tear it down so we don't leak €/day and so the
        # next ensure_server() call starts from a clean slate instead of
        # short-circuiting on this stale _server_ip.
        try:
            self._wait_for_ssh()
        except Exception:
            logger.warning("SSH wait failed for %s (%s); destroying before re-raise",
                           self._server_name, self._server_ip)
            try:
                self.destroy_server()
            except Exception as destroy_exc:
                logger.warning("destroy_server after SSH wait failure also failed: %s",
                               destroy_exc)
            raise

        assert self._server_ip is not None
        return self._server_ip

    def _wait_for_ssh(self) -> None:
        """Wait until SSH is available on the server."""
        logger.info("Waiting for SSH on %s...", self._server_ip)
        deadline = time.time() + SSH_WAIT_TIMEOUT

        while time.time() < deadline:
            result = self._ssh("true", check=False)
            if result.returncode == 0:
                logger.info("SSH is ready on %s", self._server_ip)
                return
            time.sleep(SSH_WAIT_INTERVAL)

        raise RuntimeError(
            f"SSH not available on {self._server_ip} after {SSH_WAIT_TIMEOUT}s"
        )

    def sync_repo(self, local_path: str, name: str) -> None:
        """Rsync a local repo clone to the remote server."""
        if not self._server_ip:
            raise RuntimeError("No server running — call ensure_server() first")

        remote_dir = f"/tmp/src/{name}"
        logger.info("Syncing %s to remote:%s", name, remote_dir)

        self._ssh("mkdir", "-p", remote_dir)
        subprocess.run(
            ["rsync", "-az", "--delete",
             "-e", f"ssh {' '.join(SSH_OPTS)} -i {self.ssh_key_path}",
             f"{local_path}/", f"root@{self._server_ip}:{remote_dir}/"],
            check=True,
        )

    def setup_sdk(self, target: str, openwrt_version: str) -> str:
        """Install build deps and download SDK on the remote server.

        Returns the remote SDK path.
        """
        version = openwrt_version
        actual_target = target
        if target == "all":
            actual_target = next(iter(TARGET_ARCH_MAP))

        cache_key = (actual_target, version)
        if cache_key in self._setup_done:
            logger.info("SDK for %s (owrt %s) already set up on remote",
                         actual_target, version)
            return self._remote_sdk_path(actual_target, version)

        url = sdk_url(version, actual_target)
        sdk_path = self._remote_sdk_path(actual_target, version)

        setup_script = f"""set -e
# Install deps (idempotent)
if [ ! -f /tmp/.deps-installed ]; then
    apt-get update -qq
    apt-get install -y -qq {REMOTE_DEPS}
    touch /tmp/.deps-installed
fi
# Download and extract SDK if needed
if [ ! -d {sdk_path} ]; then
    echo "Downloading SDK from {url}"
    wget -q -O /tmp/sdk.tar.zst '{url}'
    mkdir -p /opt/sdk
    tar --zstd -xf /tmp/sdk.tar.zst -C /opt/sdk/
    rm -f /tmp/sdk.tar.zst
fi
echo "SDK ready at {sdk_path}"
"""
        logger.info("Setting up SDK for %s (owrt %s) on remote...",
                     actual_target, version)
        result = self._ssh_script(setup_script, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"Remote SDK setup failed: {result.stderr.strip()}"
            )

        self._setup_done.add(cache_key)
        logger.info("SDK for %s (owrt %s) ready on remote",
                     actual_target, version)
        return sdk_path

    def _remote_sdk_path(self, target: str, openwrt_version: str) -> str:
        """Return the expected SDK directory path on the remote server."""
        from sdk import sdk_dirname
        return f"/opt/sdk/{sdk_dirname(openwrt_version, target)}"

    def build_package(
        self,
        name: str,
        makefile_subdir: str | None,
        target: str,
        openwrt_version: str,
        sdk_force: bool = False,
    ) -> list[str]:
        """Build a package on the remote server.

        Expects the source to already be synced to /tmp/src/{name} via sync_repo().
        Returns list of remote .ipk paths.
        """
        actual_target = target
        if target == "all":
            actual_target = next(iter(TARGET_ARCH_MAP))

        sdk_path = self.setup_sdk(actual_target, openwrt_version=openwrt_version)

        # Determine what to symlink into the SDK
        source_dir = f"/tmp/src/{name}"
        if makefile_subdir:
            link_target = f"{source_dir}/{makefile_subdir}"
        else:
            link_target = source_dir

        force_flag = "FORCE=1" if sdk_force else ""

        build_script = f"""set -e
# Symlink into SDK
rm -f {sdk_path}/package/{name}
ln -s {link_target} {sdk_path}/package/{name}

# Clean previous build output
rm -rf {sdk_path}/bin/packages

cd {sdk_path}

# Ensure .config exists
if [ ! -f .config ]; then
    make defconfig {force_flag} > /dev/null 2>&1
fi

# Build (capture output to file for error reporting)
if ! make package/{name}/compile V=s -j$(nproc) {force_flag} > /tmp/build.log 2>&1; then
    echo "===BUILD_FAILED==="
    tail -50 /tmp/build.log
    exit 1
fi

# List built package files (.ipk for <= 24.10, .apk for >= 25.12)
echo "===PKG_LIST_START==="
find {sdk_path}/bin/packages \\( -name '*.ipk' -o -name '*.apk' \\) 2>/dev/null || true
echo "===PKG_LIST_END==="
"""

        logger.info("Building %s for %s on remote...", name, target)
        result = self._ssh_script(build_script, check=False)

        if result.returncode != 0:
            # Filter noise from error output
            all_output = (result.stdout or "") + (result.stderr or "")
            error_lines = [
                line for line in all_output.splitlines()
                if line.strip()
                and not line.startswith("make[")
                and not line.startswith("make:")
                and not line.startswith("Checking ")
                and not line.startswith("WARNING:")
                and "warning:" not in line
            ]
            stderr_tail = "\n".join(error_lines[-30:])
            raise RuntimeError(
                f"Remote SDK build failed for {name}:\n{stderr_tail}"
            )

        in_list = False
        pkg_paths = []
        for line in result.stdout.splitlines():
            if line.strip() == "===PKG_LIST_START===":
                in_list = True
                continue
            if line.strip() == "===PKG_LIST_END===":
                break
            if in_list and (line.strip().endswith(".ipk")
                            or line.strip().endswith(".apk")):
                pkg_paths.append(line.strip())

        logger.info("Remote build produced %d package(s) for %s",
                     len(pkg_paths), name)
        return pkg_paths

    def download_ipks(self, remote_paths: list[str], local_dir: str) -> list[Path]:
        """Download package files (.ipk or .apk) from remote to local feed dir."""
        local = Path(local_dir)
        local.mkdir(parents=True, exist_ok=True)
        downloaded = []

        for remote_path in remote_paths:
            filename = Path(remote_path).name
            local_path = local / filename
            logger.info("Downloading %s from remote", filename)
            self._scp_from(remote_path, str(local_path))
            downloaded.append(local_path)

        return downloaded

    def reindex_apk_feed(self, local_dir: str, openwrt_version: str,
                         target: str) -> Path | None:
        """Regenerate APKINDEX.tar.gz for an apk feed directory.

        apk-tools 3.x is required and not shipped on Debian; we reuse the apk
        binary from the OpenWrt SDK (`staging_dir/host/bin/apk`) on the already
        provisioned Hetzner server. Uploads every .apk in local_dir to the
        remote, invokes `apk mkndx`, and downloads the produced
        APKINDEX.tar.gz back. Returns the local index path on success.
        """
        local = Path(local_dir)
        apks = sorted(local.glob("*.apk"))
        if not apks:
            return None

        sdk_path = self._remote_sdk_path(
            target if target != "all" else next(iter(TARGET_ARCH_MAP)),
            openwrt_version,
        )
        apk_bin = f"{sdk_path}/staging_dir/host/bin/apk"

        remote_feed = f"/tmp/apkindex/{local.name}"
        logger.info("Reindexing apk feed %s (%d apks)", local, len(apks))

        self._ssh_script(f"rm -rf {remote_feed} && mkdir -p {remote_feed}")

        subprocess.run(
            ["rsync", "-a",
             "-e", f"ssh {' '.join(SSH_OPTS)} -i {self.ssh_key_path}",
             *[str(a) for a in apks],
             f"root@{self._server_ip}:{remote_feed}/"],
            check=True,
        )

        # --allow-untrusted: every .apk produced by the SDK is signed with an
        # ephemeral build-time key whose public half is NOT in the remote
        # server's apk keyring, so strict verification always fails. We built
        # these packages ourselves one step earlier in this same process, so
        # skipping the verify gate is safe. Router-side trust is a separate
        # concern tracked via the usign-equivalent apk signing of the index.
        index_script = f"""set -e
cd {remote_feed}
if [ ! -x {apk_bin} ]; then
    echo "apk binary missing at {apk_bin}" >&2
    exit 1
fi
{apk_bin} --allow-untrusted mkndx -o APKINDEX.tar.gz *.apk
"""
        result = self._ssh_script(index_script, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"apk mkndx failed for {local_dir}:\n"
                f"{(result.stdout or '') + (result.stderr or '')}"
            )

        local_index = local / "APKINDEX.tar.gz"
        self._scp_from(f"{remote_feed}/APKINDEX.tar.gz", str(local_index))
        logger.info("apk reindex complete for %s", local)
        return local_index

    def destroy_server(self) -> None:
        """Delete the Hetzner server."""
        if not self._server_name:
            logger.debug("No server to destroy")
            return

        logger.info("Destroying build server %s", self._server_name)
        result = self._hcloud("server", "delete", self._server_name)
        if result.returncode != 0:
            logger.warning("Failed to destroy server %s: %s",
                           self._server_name, result.stderr.strip())
        else:
            logger.info("Server %s destroyed", self._server_name)

        self._server_name = None
        self._server_ip = None
        self._setup_done.clear()
