import logging
import subprocess
from pathlib import Path

from config import TARGET_ARCH_MAP

logger = logging.getLogger(__name__)

# ARM targets that need the _eabi suffix
EABI_TARGETS = {"bcm27xx/bcm2709"}

GCC_VERSION = "13.3.0"


def sdk_url(version: str, target: str) -> str:
    """Build the download URL for an OpenWrt SDK."""
    dirname = sdk_dirname(version, target)
    target_path = target  # e.g., "mediatek/filogic"
    return (
        f"https://downloads.openwrt.org/releases/{version}/"
        f"targets/{target_path}/{dirname}.tar.zst"
    )


def sdk_dirname(version: str, target: str) -> str:
    """Return the expected directory name for an extracted SDK."""
    target_slug = target.replace("/", "-")
    suffix = "_eabi" if target in EABI_TARGETS else ""
    return (
        f"openwrt-sdk-{version}-{target_slug}"
        f"_gcc-{GCC_VERSION}_musl{suffix}.Linux-x86_64"
    )


class SDKManager:
    def __init__(self, version: str, cache_dir: str):
        self.version = version
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_sdk_path(self, target: str) -> str:
        """Return the path to the SDK directory for a target."""
        dirname = sdk_dirname(self.version, target)
        return str(self.cache_dir / dirname)

    def needs_download(self, target: str) -> bool:
        """Check if the SDK for a target needs to be downloaded."""
        sdk_path = Path(self.get_sdk_path(target))
        return not (sdk_path / "Makefile").exists()

    def download(self, target: str):
        """Download and extract the SDK for a target."""
        url = sdk_url(self.version, target)
        archive = self.cache_dir / f"{sdk_dirname(self.version, target)}.tar.zst"

        logger.info("Downloading SDK for %s from %s", target, url)
        subprocess.run(
            ["wget", "-q", "-O", str(archive), url],
            check=True,
        )

        logger.info("Extracting SDK for %s", target)
        subprocess.run(
            ["tar", "--zstd", "-xf", str(archive), "-C", str(self.cache_dir)],
            check=True,
        )

        # Clean up archive
        archive.unlink(missing_ok=True)
        logger.info("SDK for %s ready at %s", target, self.get_sdk_path(target))

    def ensure_downloaded(self, target: str):
        """Download the SDK if not already cached."""
        if self.needs_download(target):
            self.download(target)
