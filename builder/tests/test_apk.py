import zlib
from pathlib import Path

import pytest

from apk import apk_arch, index_json

FIXTURES = Path(__file__).parent / "fixtures"
# Real SDK output: a PKGARCH:=all package, and one built from a
# `targets: [all]` repo that still came out arch-specific.
NOARCH_APK = FIXTURES / "wifi-dethrash-collector-0.4.0-r1.apk"
ARCH_APK = FIXTURES / "quectel-5g-tools-1.2.0-r0.apk"
# `apk adbdump --format json` of the live aarch64_cortex-a53 feed index,
# trimmed to the packages the index.json tests look at.
ADBDUMP = (FIXTURES / "adbdump-aarch64_cortex-a53.json").read_text()


def test_arch_independent_package_reads_noarch():
    assert apk_arch(NOARCH_APK) == "noarch"


def test_arch_specific_package_reads_its_arch():
    assert apk_arch(ARCH_APK) == "aarch64_cortex-a53"


def test_uncompressed_adb_package_reads_its_arch(tmp_path):
    raw = zlib.decompressobj(-zlib.MAX_WBITS).decompress(
        ARCH_APK.read_bytes()[4:])
    plain = tmp_path / "plain.apk"
    plain.write_bytes(raw)
    assert apk_arch(plain) == "aarch64_cortex-a53"


def test_ipk_is_rejected_not_guessed(tmp_path):
    ipk = tmp_path / "pkg_1.0_all.ipk"
    ipk.write_bytes(b"\x1f\x8b\x08\x00not an apk")
    with pytest.raises(ValueError, match="not an uncompressed or deflated"):
        apk_arch(ipk)


def test_index_json_has_upstream_v2_shape():
    index = index_json(ADBDUMP, "aarch64_cortex-a53")
    assert index["version"] == 2
    assert index["architecture"] == "aarch64_cortex-a53"
    assert index["packages"]["wifi-dethrash-collector"] == "0.4.0-r1"
    assert index["packages"]["zlib"] == "1.3.1-r1"


def test_index_json_strips_abi_version_from_names():
    packages = index_json(ADBDUMP, "aarch64_cortex-a53")["packages"]
    assert packages["liblz4"] == "1.10.0-r1"
    assert packages["libusb-1.0"] == "1.0.29-r1"
    assert "liblz4-1" not in packages


def test_index_json_keeps_the_newest_of_several_versions():
    # 1.10.2 sorts after 1.9.1 in apk order, not lexically.
    packages = index_json(ADBDUMP, "aarch64_cortex-a53")["packages"]
    assert packages["quectel-5g-tools"] == "1.10.2-r1"
