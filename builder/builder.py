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


def _is_openwrt_makefile(path: Path) -> bool:
    """Check if a Makefile is an OpenWrt package Makefile."""
    try:
        content = path.read_text()
        return "include $(INCLUDE_DIR)/package.mk" in content
    except Exception:
        return False


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
        sdk_force: bool = False,
    ):
        self.name = repo_config["name"]
        self.url = repo_config["url"]
        self.branch = repo_config["branch"]
        self.targets = repo_config.get("targets", [])
        self.repo_dir = Path(repo_cache_dir) / self.name
        self.feed_dir = Path(feed_dir)
        self.sdk_manager = sdk_manager
        self.sdk_force = sdk_force

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
        method, makefile_dir = self._detect_build_method()

        if method == "skip":
            logger.warning(
                "No recognized build method for %s (no OpenWrt Makefile, "
                "no build-ipk.sh, no CONTROL/). Skipping.",
                self.name,
            )
            return []

        if method == "script":
            return self._build_with_script(target)

        if method == "opkg-build":
            return self._build_with_opkg(target)

        # method == "sdk"
        return self._build_with_sdk(target, makefile_dir=makefile_dir)

    def _detect_build_method(self) -> tuple[str, Path | None]:
        """Detect how to build this repo based on its contents.

        Returns (method, makefile_dir) where method is one of:
          "sdk", "script", "opkg-build", "skip"
        """
        for candidate in [self.repo_dir / "Makefile",
                          self.repo_dir / "openwrt" / "Makefile"]:
            if candidate.exists() and _is_openwrt_makefile(candidate):
                return ("sdk", candidate.parent)

        if (self.repo_dir / "build-ipk.sh").exists():
            return ("script", None)

        if (self.repo_dir / "CONTROL").is_dir():
            return ("opkg-build", None)

        return ("skip", None)

    def _get_arch_dir(self, target: str) -> Path:
        """Return the feed directory for a target."""
        if target == "all":
            return self.feed_dir / "all"
        return self.feed_dir / TARGET_ARCH_MAP[target]

    def _collect_and_copy(self, search_dir: Path, dest_dir: Path,
                          name_filter: str | None = None) -> list[Path]:
        """Find .ipk files under search_dir and copy them to dest_dir."""
        ipks = _collect_ipk_files(str(search_dir))
        copied = []
        for ipk in ipks:
            if name_filter and name_filter not in ipk.name:
                continue
            dest = dest_dir / ipk.name
            shutil.copy2(str(ipk), str(dest))
            copied.append(dest)
            logger.info("Copied %s to %s", ipk.name, dest_dir)
        return copied

    def _build_with_script(self, target: str) -> list[Path]:
        """Build using the repo's build-ipk.sh script."""
        arch_dir = self._get_arch_dir(target)
        arch_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Running build-ipk.sh for %s", self.name)
        subprocess.run(
            ["bash", str(self.repo_dir / "build-ipk.sh")],
            cwd=str(self.repo_dir),
            check=True,
        )

        return self._collect_and_copy(self.repo_dir, arch_dir)

    def _build_with_opkg(self, target: str) -> list[Path]:
        """Build using opkg-build (expects CONTROL/ directory)."""
        arch_dir = self._get_arch_dir(target)
        arch_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Running opkg-build for %s", self.name)
        subprocess.run(
            ["opkg-build", str(self.repo_dir)],
            cwd=str(arch_dir),
            check=True,
        )

        return self._collect_and_copy(self.repo_dir, arch_dir)

    def _pick_any_sdk_target(self) -> str:
        """Pick any available SDK target for arch-independent builds."""
        for target in TARGET_ARCH_MAP:
            if not self.sdk_manager.needs_download(target):
                logger.info("Using cached SDK %s for arch-independent build", target)
                return target
        first_target = next(iter(TARGET_ARCH_MAP))
        logger.info("No cached SDK, will download %s for arch-independent build",
                     first_target)
        return first_target

    def _build_with_sdk(self, target: str,
                        makefile_dir: Path | None = None) -> list[Path]:
        """Build the package using the OpenWrt SDK."""
        arch_dir = self._get_arch_dir(target)
        arch_dir.mkdir(parents=True, exist_ok=True)

        actual_target = target
        if target == "all":
            actual_target = self._pick_any_sdk_target()

        self.sdk_manager.ensure_downloaded(actual_target)
        sdk_path = Path(self.sdk_manager.get_sdk_path(actual_target))

        source_dir = makefile_dir if makefile_dir else self.repo_dir

        sdk_pkg_dir = sdk_path / "package" / self.name
        if sdk_pkg_dir.exists() or sdk_pkg_dir.is_symlink():
            sdk_pkg_dir.unlink() if sdk_pkg_dir.is_symlink() else shutil.rmtree(str(sdk_pkg_dir))
        sdk_pkg_dir.symlink_to(source_dir)

        logger.info("Compiling %s for %s (SDK target: %s)", self.name, target,
                     actual_target)
        make_cmd = ["make", f"package/{self.name}/compile", "V=s",
                    f"-j{_nproc()}"]
        if self.sdk_force:
            make_cmd.append("FORCE=1")
        result = subprocess.run(
            make_cmd,
            cwd=str(sdk_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Combine stdout+stderr and filter out make cascade noise
            all_output = (result.stdout or "") + (result.stderr or "")
            error_lines = [
                line for line in all_output.splitlines()
                if line.strip()
                and not line.startswith("make[")
                and not line.startswith("make:")
                and not line.startswith("Checking ")
            ]
            stderr_tail = "\n".join(error_lines[-30:])
            raise RuntimeError(
                f"SDK build failed for {self.name}:\n{stderr_tail}"
            )

        return self._collect_and_copy(
            sdk_path / "bin", arch_dir, name_filter=self.name
        )

    def build_all_targets(self) -> dict[str, list[Path]]:
        """Build for all configured targets. Returns {target: [ipk_paths]}."""
        results = {}
        modified_dirs = set()

        for target in self.targets:
            ipks = self.build_for_target(target)
            results[target] = ipks
            modified_dirs.add(str(self._get_arch_dir(target)))

        for d in modified_dirs:
            reindex_feed(d)

        return results


def _nproc() -> int:
    """Return number of CPUs available."""
    import os
    return os.cpu_count() or 1
