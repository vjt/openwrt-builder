from __future__ import annotations

import os
import re
from typing import Any

import yaml


TARGET_ARCH_MAP: dict[str, str] = {
    "mediatek/filogic": "aarch64_cortex-a53",
    "bcm27xx/bcm2709": "arm_cortex-a7_neon-vfpv4",
    "ramips/mt7621": "mipsel_24kc",
    "ath79/tiny": "mips_24kc",
}

VALID_TARGETS = set(TARGET_ARCH_MAP.keys()) | {"all"}

ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def resolve_env_vars(value: str) -> str:
    """Replace ${VAR} references with environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            raise ValueError(
                f"Environment variable {var_name} is not set"
            )
        return val
    return ENV_VAR_PATTERN.sub(replacer, value)


def _resolve_recursive(obj: Any) -> Any:
    """Walk a data structure and resolve env vars in all strings."""
    if isinstance(obj, str):
        return resolve_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _resolve_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_recursive(item) for item in obj]
    return obj


def _normalize_versions(value: Any, ctx: str) -> list[str]:
    """Accept either a single string or a list of strings; return a list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        if not value:
            raise ValueError(f"{ctx}: openwrt_versions must not be empty")
        return list(value)
    raise ValueError(
        f"{ctx}: openwrt_versions must be a string or list of strings"
    )


def load_config(path: str) -> dict[str, Any]:
    """Load and validate the builder config file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    config = _resolve_recursive(raw)

    default_targets = config.get("default_targets", [])

    if "openwrt_versions" not in config:
        raise ValueError("Top-level openwrt_versions is required")
    default_versions = _normalize_versions(
        config["openwrt_versions"], "top-level"
    )
    config["openwrt_versions"] = default_versions

    for t in default_targets:
        if t not in VALID_TARGETS:
            raise ValueError(f"Unknown target: {t}")

    for repo in config.get("repos", []):
        if "targets" not in repo:
            repo["targets"] = list(default_targets)
        for t in repo["targets"]:
            if t not in VALID_TARGETS:
                raise ValueError(f"Unknown target: {t}")

        versions_value = repo.get("openwrt_versions", default_versions)
        repo["openwrt_versions"] = _normalize_versions(
            versions_value, f"repo {repo.get('name')!r}"
        )

    return config
