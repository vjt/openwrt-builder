import zlib
from pathlib import Path

import pytest

from apk import apk_arch

FIXTURES = Path(__file__).parent / "fixtures"
# Real SDK output: a PKGARCH:=all package, and one built from a
# `targets: [all]` repo that still came out arch-specific.
NOARCH_APK = FIXTURES / "wifi-dethrash-collector-0.4.0-r1.apk"
ARCH_APK = FIXTURES / "quectel-5g-tools-1.2.0-r0.apk"


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
