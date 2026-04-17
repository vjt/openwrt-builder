import os
import pytest
import tempfile
import yaml
from pathlib import Path

from config import load_config, resolve_env_vars, TARGET_ARCH_MAP


def test_target_arch_map_has_all_supported_targets():
    assert TARGET_ARCH_MAP["mediatek/filogic"] == "aarch64_cortex-a53"
    assert TARGET_ARCH_MAP["bcm27xx/bcm2709"] == "arm_cortex-a7_neon-vfpv4"
    assert TARGET_ARCH_MAP["ramips/mt7621"] == "mipsel_24kc"
    assert TARGET_ARCH_MAP["ath79/tiny"] == "mips_24kc"


def test_resolve_env_vars_substitutes_values(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    assert resolve_env_vars("${MY_TOKEN}") == "secret123"
    assert resolve_env_vars("prefix_${MY_TOKEN}_suffix") == "prefix_secret123_suffix"


def test_resolve_env_vars_raises_on_missing(monkeypatch):
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
    with pytest.raises(ValueError, match="NONEXISTENT_VAR"):
        resolve_env_vars("${NONEXISTENT_VAR}")


def test_resolve_env_vars_no_substitution():
    assert resolve_env_vars("plain string") == "plain string"


def test_load_config_minimal(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    config_data = {
        "openwrt_versions": "24.10.0",
        "default_targets": ["mediatek/filogic"],
        "poll_interval": 3600,
        "telegram": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
        },
        "repos": [
            {
                "name": "test-pkg",
                "url": "https://github.com/test/test-pkg.git",
                "branch": "main",
            }
        ],
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_data))

    cfg = load_config(str(config_file))
    assert cfg["telegram"]["bot_token"] == "tok123"
    assert cfg["telegram"]["chat_id"] == "456"
    assert cfg["repos"][0]["targets"] == ["mediatek/filogic"]


def test_load_config_repo_inherits_default_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config_data = {
        "openwrt_versions": "24.10.0",
        "default_targets": ["mediatek/filogic", "ramips/mt7621"],
        "poll_interval": 3600,
        "telegram": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
        },
        "repos": [
            {
                "name": "no-targets",
                "url": "https://github.com/test/pkg.git",
                "branch": "main",
            }
        ],
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_data))

    cfg = load_config(str(config_file))
    assert cfg["repos"][0]["targets"] == ["mediatek/filogic", "ramips/mt7621"]


def test_load_config_repo_overrides_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config_data = {
        "openwrt_versions": "24.10.0",
        "default_targets": ["mediatek/filogic", "ramips/mt7621"],
        "poll_interval": 3600,
        "telegram": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
        },
        "repos": [
            {
                "name": "override",
                "url": "https://github.com/test/pkg.git",
                "branch": "main",
                "targets": ["ath79/tiny"],
            }
        ],
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_data))

    cfg = load_config(str(config_file))
    assert cfg["repos"][0]["targets"] == ["ath79/tiny"]


def test_load_config_validates_unknown_target(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config_data = {
        "openwrt_versions": "24.10.0",
        "default_targets": ["fake/target"],
        "poll_interval": 3600,
        "telegram": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
        },
        "repos": [
            {
                "name": "pkg",
                "url": "https://github.com/test/pkg.git",
                "branch": "main",
            }
        ],
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_data))

    with pytest.raises(ValueError, match="fake/target"):
        load_config(str(config_file))
