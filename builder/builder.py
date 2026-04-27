from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import TARGET_ARCH_MAP
from sdk import SDKManager

if TYPE_CHECKING:
    from bot import OpenwrtBot
    from remote import RemoteBuilder

logger = logging.getLogger(__name__)


PKG_EXTENSIONS = ("*.ipk", "*.apk")


def _collect_package_files(bin_dir: str) -> list[Path]:
    """Recursively find all package files (.ipk or .apk) under a directory."""
    base = Path(bin_dir)
    return [p for ext in PKG_EXTENSIONS for p in base.rglob(ext)]


def _is_openwrt_makefile(path: Path) -> bool:
    """Check if a Makefile is an OpenWrt package Makefile."""
    try:
        content = path.read_text()
        return "include $(INCLUDE_DIR)/package.mk" in content
    except Exception:
        return False


def _register_sdk_feed(sdk_path: Path, name: str, src_dir: Path) -> None:
    """Idempotently add a `src-link` custom feed to an SDK's feeds.conf.

    OpenWrt SDKs ship with `feeds.conf.default` (read-only template); if the
    user provides `feeds.conf` it replaces the default entirely. We seed
    `feeds.conf` from the default on first call, then append/replace the
    `src-link <name> <src_dir>` line so re-runs are no-ops.
    """
    feeds_conf = sdk_path / "feeds.conf"
    feeds_default = sdk_path / "feeds.conf.default"
    if not feeds_conf.exists() and feeds_default.exists():
        feeds_conf.write_text(feeds_default.read_text())

    line = f"src-link {name} {src_dir}\n"
    existing = feeds_conf.read_text() if feeds_conf.exists() else ""
    kept = [
        ln for ln in existing.splitlines(keepends=True)
        if not ln.startswith(f"src-link {name} ")
    ]
    feeds_conf.write_text("".join(kept) + line)


def _extract_pkg_name(makefile: Path) -> str | None:
    """Read PKG_NAME from an OpenWrt package Makefile."""
    try:
        for line in makefile.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("PKG_NAME:="):
                return stripped.split(":=", 1)[1].strip()
        return None
    except Exception:
        return None


def reindex_feed(feed_arch_dir: str):
    """Regenerate the opkg (ipk) Packages index in a feed arch directory.

    The apk (OpenWrt >= 25.12) feed index is regenerated remotely on the
    Hetzner builder because apk-tools is not packaged on Debian — see
    RemoteBuilder.reindex_apk_feed.
    """
    feed_path = Path(feed_arch_dir)
    if not any(feed_path.glob("*.ipk")):
        return

    logger.info("Reindexing ipk feed %s", feed_arch_dir)
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


def sign_feed(feed_arch_dir: str, signing_key: str):
    """Sign the Packages file with usign, producing Packages.sig."""
    packages_file = Path(feed_arch_dir) / "Packages"
    if not packages_file.exists():
        return
    if not Path(signing_key).exists():
        logger.warning("Signing key %s not found, skipping feed signing", signing_key)
        return
    logger.info("Signing %s", packages_file)
    subprocess.run(
        ["usign", "-S", "-m", str(packages_file), "-s", signing_key],
        check=True,
    )


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
        repo_config: dict[str, Any],
        repo_cache_dir: str,
        feed_dir: str,
        sdk_managers: dict[str, SDKManager],
        sdk_force: bool = False,
        remote_builder: RemoteBuilder | None = None,
        bot: OpenwrtBot | None = None,
        signing_key: str | None = None,
    ):
        self.name = repo_config["name"]
        self.url = repo_config["url"]
        self.branch = repo_config["branch"]
        self.targets = repo_config.get("targets", [])
        self.openwrt_version = repo_config["openwrt_version"]
        self.repo_dir = Path(repo_cache_dir) / self.name
        self.feed_dir = Path(feed_dir)
        if self.openwrt_version not in sdk_managers:
            raise RuntimeError(
                f"No SDKManager registered for openwrt_version "
                f"{self.openwrt_version!r} (repo {self.name})"
            )
        self.sdk_manager = sdk_managers[self.openwrt_version]
        self.sdk_force = sdk_force
        self.remote_builder = remote_builder
        self.bot = bot
        self.signing_key = signing_key

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

    async def build_for_target(self, target: str) -> list[Path]:
        """Build the package for a specific target. Returns list of .ipk paths."""
        method, source_dir, pkg_names = self._detect_build_method()

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

        if method == "sdk-feed":
            assert source_dir is not None
            if self.remote_builder:
                return await self._build_remote(
                    target, feed_dir=source_dir, pkg_names=pkg_names)
            return self._build_with_sdk_feed(target, source_dir, pkg_names)

        # method == "sdk"
        if self.remote_builder:
            return await self._build_remote(target, makefile_dir=source_dir)
        return self._build_with_sdk(target, makefile_dir=source_dir)

    def _detect_build_method(self) -> tuple[str, Path | None, list[str]]:
        """Detect how to build this repo based on its contents.

        Returns (method, source_dir, pkg_names) where method is one of:
          "sdk", "sdk-feed", "script", "opkg-build", "skip"

        For "sdk", source_dir is the Makefile directory and pkg_names is empty
        (the repo is symlinked into the SDK as `package/<repo-name>` and built
        with `make package/<repo-name>/compile`).

        For "sdk-feed", source_dir is the openwrt/ root containing 2+ package
        subdirs and pkg_names lists the actual PKG_NAMEs. The openwrt/ dir is
        registered as a `src-link` custom feed in the SDK so inter-package
        DEPENDS resolve naturally (e.g. android-tools → libbrotli{...}).

        SDK Makefile lookup order:
          1. <repo>/Makefile                — single-package repo at root
          2. <repo>/openwrt/Makefile        — legacy layout (single package)
          3. <repo>/openwrt/<pkgname>/Makefile — single-package src-link layout
          4. <repo>/openwrt/<pkgA>/Makefile + <pkgB>/Makefile — multi-package
        """
        candidates = [self.repo_dir / "Makefile",
                      self.repo_dir / "openwrt" / "Makefile"]
        for candidate in candidates:
            if candidate.exists() and _is_openwrt_makefile(candidate):
                return ("sdk", candidate.parent, [])

        openwrt_dir = self.repo_dir / "openwrt"
        if openwrt_dir.is_dir():
            pkg_makefiles = []
            for subdir in sorted(openwrt_dir.iterdir()):
                if subdir.is_dir():
                    mk = subdir / "Makefile"
                    if mk.exists() and _is_openwrt_makefile(mk):
                        pkg_makefiles.append(mk)
            if len(pkg_makefiles) == 1:
                return ("sdk", pkg_makefiles[0].parent, [])
            if len(pkg_makefiles) >= 2:
                pkg_names = [
                    n for n in (_extract_pkg_name(m) for m in pkg_makefiles)
                    if n
                ]
                return ("sdk-feed", openwrt_dir, pkg_names)

        if (self.repo_dir / "build-ipk.sh").exists():
            return ("script", None, [])

        if (self.repo_dir / "CONTROL").is_dir():
            return ("opkg-build", None, [])

        return ("skip", None, [])

    def _get_arch_dir(self, target: str) -> Path:
        """Return the feed directory for a target."""
        if target == "all":
            return self.feed_dir / "all"
        return self.feed_dir / TARGET_ARCH_MAP[target]

    def _collect_and_copy(self, search_dir: Path, dest_dir: Path,
                          name_filter: str | None = None) -> list[Path]:
        """Find .ipk / .apk files under search_dir and copy them to dest_dir."""
        pkgs = _collect_package_files(str(search_dir))
        copied = []
        for pkg in pkgs:
            if name_filter and name_filter not in pkg.name:
                continue
            dest = dest_dir / pkg.name
            shutil.copy2(str(pkg), str(dest))
            copied.append(dest)
            logger.info("Copied %s to %s", pkg.name, dest_dir)
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

    async def _build_remote(
        self,
        target: str,
        makefile_dir: Path | None = None,
        feed_dir: Path | None = None,
        pkg_names: list[str] | None = None,
    ) -> list[Path]:
        """Build the package on a remote Hetzner instance via SSH."""
        assert self.remote_builder is not None

        arch_dir = self._get_arch_dir(target)
        arch_dir.mkdir(parents=True, exist_ok=True)

        created = self.remote_builder._server_ip is None
        if created and self.bot:
            await self.bot.notify_server_create(
                self.remote_builder.server_type,
                ", ".join(self.remote_builder.locations))

        ip = self.remote_builder.ensure_server()

        if created and self.bot:
            await self.bot.notify_server_ready(ip)

        # Sync the local clone to the remote server
        self.remote_builder.sync_repo(str(self.repo_dir), self.name)

        if feed_dir is not None:
            assert pkg_names, "feed mode requires non-empty pkg_names"
            feed_subdir = str(feed_dir.relative_to(self.repo_dir))
            remote_ipks = self.remote_builder.build_packages_via_feed(
                name=self.name,
                feed_subdir=feed_subdir,
                pkg_names=pkg_names,
                target=target,
                sdk_force=self.sdk_force,
                openwrt_version=self.openwrt_version,
            )
        else:
            # Determine makefile subdir relative to repo root (e.g., "openwrt")
            makefile_subdir = None
            if makefile_dir and makefile_dir != self.repo_dir:
                makefile_subdir = str(makefile_dir.relative_to(self.repo_dir))
            remote_ipks = self.remote_builder.build_package(
                name=self.name,
                makefile_subdir=makefile_subdir,
                target=target,
                sdk_force=self.sdk_force,
                openwrt_version=self.openwrt_version,
            )

        return self.remote_builder.download_ipks(remote_ipks, str(arch_dir))

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

        # Clean previous build output so we only collect this package's ipks
        bin_packages = sdk_path / "bin" / "packages"
        if bin_packages.exists():
            shutil.rmtree(str(bin_packages))

        logger.info("Compiling %s for %s (SDK target: %s)", self.name, target,
                     actual_target)

        # Ensure .config exists so make doesn't launch interactive menuconfig
        defconfig_cmd = ["make", "defconfig"]
        if self.sdk_force:
            defconfig_cmd.append("FORCE=1")
        if not (sdk_path / ".config").exists():
            logger.info("Running make defconfig for SDK %s", actual_target)
            subprocess.run(
                defconfig_cmd,
                cwd=str(sdk_path),
                capture_output=True,
            )

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
            # Combine stdout+stderr and filter out noise
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
                f"SDK build failed for {self.name}:\n{stderr_tail}"
            )

        return self._collect_and_copy(
            sdk_path / "bin" / "packages", arch_dir
        )

    def _build_with_sdk_feed(
        self,
        target: str,
        feed_dir: Path,
        pkg_names: list[str],
    ) -> list[Path]:
        """Build a multi-package repo by registering its openwrt/ as a custom
        feed in the SDK and compiling each package. Inter-package DEPENDS
        resolve via the SDK's normal feed machinery."""
        if not pkg_names:
            raise RuntimeError(
                f"sdk-feed build of {self.name}: no PKG_NAMEs discovered "
                f"in {feed_dir}"
            )

        arch_dir = self._get_arch_dir(target)
        arch_dir.mkdir(parents=True, exist_ok=True)

        actual_target = target
        if target == "all":
            actual_target = self._pick_any_sdk_target()

        self.sdk_manager.ensure_downloaded(actual_target)
        sdk_path = Path(self.sdk_manager.get_sdk_path(actual_target))

        # OpenWrt scripts/feeds parses feed names as \w+ (no hyphens) — turn
        # the repo name into a legal identifier.
        feed_name = self.name.replace("-", "_")
        _register_sdk_feed(sdk_path, feed_name, feed_dir)

        # Clean previous build output so we only collect this repo's packages
        bin_packages = sdk_path / "bin" / "packages"
        if bin_packages.exists():
            shutil.rmtree(str(bin_packages))

        force_flag = ["FORCE=1"] if self.sdk_force else []

        logger.info("Updating + installing custom feed %s for %s",
                     feed_name, self.name)
        subprocess.run(
            [str(sdk_path / "scripts/feeds"), "update", feed_name],
            cwd=str(sdk_path), check=True,
        )
        subprocess.run(
            [str(sdk_path / "scripts/feeds"), "install", "-p", feed_name, "-a"],
            cwd=str(sdk_path), check=True,
        )

        if not (sdk_path / ".config").exists():
            logger.info("Running make defconfig for SDK %s", actual_target)
            subprocess.run(
                ["make", "defconfig", *force_flag],
                cwd=str(sdk_path), capture_output=True,
            )

        compile_targets = [f"package/{p}/compile" for p in pkg_names]
        logger.info("Compiling %s for %s (SDK target: %s)",
                     ", ".join(pkg_names), target, actual_target)
        result = subprocess.run(
            ["make", *compile_targets, "V=s", f"-j{_nproc()}", *force_flag],
            cwd=str(sdk_path), capture_output=True, text=True,
        )
        if result.returncode != 0:
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
            raise RuntimeError(
                f"SDK build failed for {self.name}:\n"
                + "\n".join(error_lines[-30:])
            )

        return self._collect_and_copy(
            sdk_path / "bin" / "packages", arch_dir
        )

    async def build_all_targets(self) -> dict[str, list[Path]]:
        """Build for all configured targets.

        Returns {target: [package_paths]} — paths may be .ipk or .apk depending
        on the OpenWrt version used. The caller is responsible for reindexing
        the apk side (see RemoteBuilder.reindex_apk_feed); we reindex ipk here
        because that only needs `opkg-make-index` or the tarball fallback.
        """
        results = {}
        modified_dirs = set()

        for target in self.targets:
            pkgs = await self.build_for_target(target)
            results[target] = pkgs
            modified_dirs.add(str(self._get_arch_dir(target)))

        for d in modified_dirs:
            reindex_feed(d)
            if self.signing_key:
                sign_feed(d, self.signing_key)

        return results


def _nproc() -> int:
    """Return number of CPUs available."""
    return os.cpu_count() or 1
