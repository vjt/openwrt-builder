from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from config import TARGET_ARCH_MAP
from sdk import SDKManager

logger = logging.getLogger(__name__)


def _collect_ipk_files(bin_dir: str) -> list[Path]:
    """Recursively find all .ipk files under a directory."""
    return list(Path(bin_dir).rglob("*.ipk"))


def reindex_feed(feed_arch_dir: str):
    """Regenerate Packages and Packages.gz in a feed architecture directory."""
    logger.info("Reindexing %s", feed_arch_dir)
    feed_path = Path(feed_arch_dir)
    if not any(feed_path.glob("*.ipk")):
        logger.info("No .ipk files in %s, skipping reindex", feed_arch_dir)
        return

    try:
        subprocess.run(
            ["opkg-make-index", str(feed_path)],
            stdout=open(feed_path / "Packages", "w"),
            check=True,
        )
        subprocess.run(
            ["gzip", "-9c", str(feed_path / "Packages")],
            stdout=open(feed_path / "Packages.gz", "wb"),
            check=True,
        )
        logger.info("Reindex complete for %s", feed_arch_dir)
    except FileNotFoundError:
        _manual_reindex(feed_path)


def _manual_reindex(feed_path: Path):
    """Generate Packages index manually by extracting control files from .ipk."""
    logger.info("Using manual reindex for %s", feed_path)
    packages_content = []

    for ipk in sorted(feed_path.glob("*.ipk")):
        try:
            result = subprocess.run(
                ["tar", "xOf", str(ipk), "./control.tar.gz"],
                capture_output=True,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["tar", "xOf", str(ipk), "./control.tar.zst"],
                    capture_output=True,
                )

            if result.returncode == 0:
                ctrl = subprocess.run(
                    ["tar", "xzOf", "-", "./control"],
                    input=result.stdout,
                    capture_output=True,
                )
                if ctrl.returncode == 0:
                    control_text = ctrl.stdout.decode()
                    filename = ipk.name
                    size = ipk.stat().st_size
                    control_text = control_text.strip()
                    control_text += f"\nFilename: {filename}\n"
                    control_text += f"Size: {size}\n"

                    sha_result = subprocess.run(
                        ["sha256sum", str(ipk)],
                        capture_output=True,
                        text=True,
                    )
                    if sha_result.returncode == 0:
                        sha = sha_result.stdout.split()[0]
                        control_text += f"SHA256sum: {sha}\n"

                    packages_content.append(control_text)
        except Exception as e:
            logger.warning("Failed to extract control from %s: %s", ipk, e)

    with open(feed_path / "Packages", "w") as f:
        f.write("\n".join(packages_content))
        if packages_content:
            f.write("\n")

    subprocess.run(
        ["gzip", "-9kf", str(feed_path / "Packages")],
        check=True,
    )
    logger.info("Manual reindex complete for %s", feed_path)


class PackageBuilder:
    def __init__(
        self,
        repo_config: dict,
        repo_cache_dir: str,
        feed_dir: str,
        sdk_manager: SDKManager,
    ):
        self.name = repo_config["name"]
        self.url = repo_config["url"]
        self.branch = repo_config["branch"]
        self.targets = repo_config.get("targets", [])
        self.repo_dir = Path(repo_cache_dir) / self.name
        self.feed_dir = Path(feed_dir)
        self.sdk_manager = sdk_manager

    def clone_or_fetch(self):
        """Clone the repo if new, or fetch latest changes."""
        if (self.repo_dir / ".git").is_dir():
            logger.info("Fetching %s", self.name)
            subprocess.run(
                ["git", "-C", str(self.repo_dir), "fetch", "--all"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(self.repo_dir), "reset", "--hard",
                 f"origin/{self.branch}"],
                check=True,
            )
        else:
            logger.info("Cloning %s from %s", self.name, self.url)
            subprocess.run(
                ["git", "clone", "-b", self.branch, self.url,
                 str(self.repo_dir)],
                check=True,
            )

    def get_head_commit(self) -> str:
        """Return the current HEAD commit hash."""
        result = subprocess.check_output(
            ["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"],
        )
        return result.decode().strip()

    def build_for_target(self, target: str) -> list[Path]:
        """Build the package for a specific target. Returns list of .ipk paths."""
        if target == "all":
            return self._build_arch_independent()
        return self._build_with_sdk(target)

    def _build_arch_independent(self) -> list[Path]:
        """Build an architecture-independent package using opkg-build."""
        arch_dir = self.feed_dir / "all"
        arch_dir.mkdir(parents=True, exist_ok=True)

        build_script = self.repo_dir / "build-ipk.sh"
        if build_script.exists():
            logger.info("Running build-ipk.sh for %s", self.name)
            subprocess.run(
                ["bash", str(build_script)],
                cwd=str(self.repo_dir),
                check=True,
            )
        else:
            logger.info("Running opkg-build for %s", self.name)
            subprocess.run(
                ["opkg-build", str(self.repo_dir)],
                cwd=str(arch_dir),
                check=True,
            )

        ipks = _collect_ipk_files(str(self.repo_dir))
        copied = []
        for ipk in ipks:
            dest = arch_dir / ipk.name
            shutil.copy2(str(ipk), str(dest))
            copied.append(dest)
            logger.info("Copied %s to %s", ipk.name, arch_dir)

        return copied

    def _build_with_sdk(self, target: str) -> list[Path]:
        """Build the package using the OpenWrt SDK for the given target."""
        arch = TARGET_ARCH_MAP[target]
        arch_dir = self.feed_dir / arch
        arch_dir.mkdir(parents=True, exist_ok=True)

        self.sdk_manager.ensure_downloaded(target)
        sdk_path = Path(self.sdk_manager.get_sdk_path(target))

        sdk_pkg_dir = sdk_path / "package" / self.name
        if sdk_pkg_dir.exists() or sdk_pkg_dir.is_symlink():
            sdk_pkg_dir.unlink() if sdk_pkg_dir.is_symlink() else shutil.rmtree(str(sdk_pkg_dir))
        sdk_pkg_dir.symlink_to(self.repo_dir)

        logger.info("Compiling %s for %s (arch: %s)", self.name, target, arch)
        subprocess.run(
            ["make", f"package/{self.name}/compile", "V=s",
             f"-j{_nproc()}"],
            cwd=str(sdk_path),
            check=True,
            capture_output=True,
        )

        ipks = _collect_ipk_files(str(sdk_path / "bin"))
        copied = []
        for ipk in ipks:
            if self.name in ipk.name:
                dest = arch_dir / ipk.name
                shutil.copy2(str(ipk), str(dest))
                copied.append(dest)
                logger.info("Copied %s to %s", ipk.name, arch_dir)

        return copied

    def build_all_targets(self) -> dict[str, list[Path]]:
        """Build for all configured targets. Returns {target: [ipk_paths]}."""
        results = {}
        modified_dirs = set()

        for target in self.targets:
            ipks = self.build_for_target(target)
            results[target] = ipks
            if target == "all":
                modified_dirs.add(str(self.feed_dir / "all"))
            else:
                modified_dirs.add(str(self.feed_dir / TARGET_ARCH_MAP[target]))

        for d in modified_dirs:
            reindex_feed(d)

        return results


def _nproc() -> int:
    """Return number of CPUs available."""
    import os
    return os.cpu_count() or 1
