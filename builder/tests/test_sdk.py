import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from sdk import SDKManager, sdk_url, sdk_dirname


def test_sdk_url_mediatek_filogic():
    url = sdk_url("24.10.0", "mediatek/filogic")
    assert url == (
        "https://downloads.openwrt.org/releases/24.10.0/"
        "targets/mediatek/filogic/"
        "openwrt-sdk-24.10.0-mediatek-filogic_gcc-13.3.0_musl"
        ".Linux-x86_64.tar.zst"
    )


def test_sdk_url_bcm27xx():
    url = sdk_url("24.10.0", "bcm27xx/bcm2709")
    assert url == (
        "https://downloads.openwrt.org/releases/24.10.0/"
        "targets/bcm27xx/bcm2709/"
        "openwrt-sdk-24.10.0-bcm27xx-bcm2709_gcc-13.3.0_musl_eabi"
        ".Linux-x86_64.tar.zst"
    )


def test_sdk_url_ramips():
    url = sdk_url("24.10.0", "ramips/mt7621")
    assert url == (
        "https://downloads.openwrt.org/releases/24.10.0/"
        "targets/ramips/mt7621/"
        "openwrt-sdk-24.10.0-ramips-mt7621_gcc-13.3.0_musl"
        ".Linux-x86_64.tar.zst"
    )


def test_sdk_url_ath79():
    url = sdk_url("24.10.0", "ath79/tiny")
    assert url == (
        "https://downloads.openwrt.org/releases/24.10.0/"
        "targets/ath79/tiny/"
        "openwrt-sdk-24.10.0-ath79-tiny_gcc-13.3.0_musl"
        ".Linux-x86_64.tar.zst"
    )


def test_sdk_dirname():
    assert sdk_dirname("24.10.0", "mediatek/filogic") == (
        "openwrt-sdk-24.10.0-mediatek-filogic_gcc-13.3.0_musl.Linux-x86_64"
    )
    assert sdk_dirname("24.10.0", "bcm27xx/bcm2709") == (
        "openwrt-sdk-24.10.0-bcm27xx-bcm2709_gcc-13.3.0_musl_eabi.Linux-x86_64"
    )


def test_sdk_manager_get_path_returns_cached(tmp_path):
    # Simulate an already-extracted SDK
    target = "mediatek/filogic"
    dirname = sdk_dirname("24.10.0", target)
    sdk_dir = tmp_path / dirname
    sdk_dir.mkdir()
    (sdk_dir / "Makefile").touch()  # marker that it's a real SDK dir

    mgr = SDKManager("24.10.0", str(tmp_path))
    path = mgr.get_sdk_path(target)
    assert path == str(sdk_dir)


def test_sdk_manager_needs_download_when_missing(tmp_path):
    mgr = SDKManager("24.10.0", str(tmp_path))
    assert mgr.needs_download("mediatek/filogic") is True


def test_sdk_manager_no_download_when_present(tmp_path):
    target = "mediatek/filogic"
    dirname = sdk_dirname("24.10.0", target)
    sdk_dir = tmp_path / dirname
    sdk_dir.mkdir()
    (sdk_dir / "Makefile").touch()

    mgr = SDKManager("24.10.0", str(tmp_path))
    assert mgr.needs_download(target) is False
