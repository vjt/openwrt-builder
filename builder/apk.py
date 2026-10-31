"""Read metadata out of apk-tools 3 (.apk, ADB format) packages.

Debian ships no apk-tools, and the only field the feed logic needs is the
package architecture, so read it straight from the ADB structure instead
of shelling out to the SDK's apk on the remote.

Layout (apk-tools src/adb.h): "ADBd" + raw deflate stream, which inflates
to "ADB." + schema + blocks. The first block holds the package object; its
values are u32s with the type in the top 4 bits and an offset into the
block payload in the low 28.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

NOARCH = "noarch"

_ADB_MAGIC = b"ADB."
_ADB_DEFLATE_MAGIC = b"ADBd"
_SCHEMA_PACKAGE = b"pckg"

_TYPE_OBJECT = 0xE
_TYPE_BLOB_8 = 0x8
_TYPE_BLOB_16 = 0x9
_TYPE_BLOB_32 = 0xA

_PKG_INFO = 1   # ADBI_PKG_PKGINFO
_PI_ARCH = 5    # ADBI_PI_ARCH


def apk_arch(path: Path) -> str:
    """Return the architecture an .apk declares ("noarch" if independent)."""
    raw = path.read_bytes()
    if raw[:4] == _ADB_DEFLATE_MAGIC:
        raw = zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw[4:])
    if raw[:4] != _ADB_MAGIC or raw[4:8] != _SCHEMA_PACKAGE:
        raise ValueError(f"{path}: not an uncompressed or deflated ADB package")

    (type_size,) = struct.unpack_from("<I", raw, 8)
    if type_size >> 30 != 0:
        raise ValueError(f"{path}: first ADB block is not the package object")
    payload = raw[12:8 + (type_size & 0x3FFFFFFF)]

    def obj(val: int) -> tuple[int, ...]:
        if val >> 28 != _TYPE_OBJECT:
            raise ValueError(f"{path}: expected an ADB object, got {val:#x}")
        off = val & 0xFFFFFFF
        (count,) = struct.unpack_from("<I", payload, off)
        return struct.unpack_from(f"<{count}I", payload, off)

    def blob(val: int) -> bytes:
        kind, off = val >> 28, val & 0xFFFFFFF
        fmt = {_TYPE_BLOB_8: "<B", _TYPE_BLOB_16: "<H", _TYPE_BLOB_32: "<I"}.get(kind)
        if fmt is None:
            raise ValueError(f"{path}: expected an ADB blob, got {val:#x}")
        (length,) = struct.unpack_from(fmt, payload, off)
        start = off + struct.calcsize(fmt)
        return payload[start:start + length]

    (root,) = struct.unpack_from("<I", payload, 4)  # after adb_hdr compat/ver
    info = obj(obj(root)[_PKG_INFO])
    if len(info) <= _PI_ARCH:
        raise ValueError(f"{path}: package info carries no arch")
    return blob(info[_PI_ARCH]).decode()
