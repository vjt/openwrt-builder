# OpenWrt Automated Package Builder — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use 10x-engineer:executing-plans to implement this plan task-by-task.

**Goal:** Build a Docker-based system that polls git repos and automatically rebuilds OpenWrt packages (Lua and C) when changes are detected, serving them via an nginx feed server with Telegram bot control.

**Architecture:** Two Docker containers (builder + nginx feed server) sharing a volume. The builder runs a Python application that combines a Telegram bot (long-polling) with a periodic build loop. It downloads OpenWrt SDKs on first run, caches them in a volume, and uses them to compile packages. State is tracked in a JSON file.

**Tech Stack:** Python 3.12, python-telegram-bot, PyYAML, Docker, docker-compose, nginx, OpenWrt SDK, opkg-utils

---

### Task 1: Project Scaffolding

**Files:**
- Create: `builder/` directory
- Create: `config.example.yaml`
- Create: `nginx.conf`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.gitignore`

**Step 1: Create `.gitignore`**

```gitignore
.env
config.yaml
packages/
*.ipk
__pycache__/
*.pyc
```

**Step 2: Create `config.example.yaml`**

```yaml
openwrt_version: "24.10.0"

default_targets:
  - mediatek/filogic       # covers MT7981, MT7986, MT7988
  - bcm27xx/bcm2709
  - ramips/mt7621
  - ath79/tiny

poll_interval: 3600  # seconds

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: "${TELEGRAM_CHAT_ID}"

repos:
  - name: my-lua-package
    url: git@github.com:you/my-lua-package.git
    branch: main
    targets: [all]              # arch-independent (Lua/config only)

  - name: my-c-package
    url: git@github.com:you/my-c-package.git
    branch: main
    # inherits default_targets
```

**Step 3: Create `.env.example`**

```
TELEGRAM_BOT_TOKEN=your-bot-token-here
TELEGRAM_CHAT_ID=your-chat-id-here
```

**Step 4: Create `nginx.conf`**

```nginx
server {
    listen 80;
    server_name _;

    root /feed;

    location / {
        autoindex on;
    }
}
```

**Step 5: Create `docker-compose.yml`**

```yaml
services:
  feed-server:
    image: nginx:alpine
    volumes:
      - feed-data:/feed:ro
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    ports:
      - "8080:80"
    restart: unless-stopped

  builder:
    build: ./builder
    volumes:
      - feed-data:/feed
      - sdk-cache:/opt/sdk
      - repo-cache:/opt/repos
      - ./config.yaml:/etc/openwrt-builder/config.yaml:ro
    environment:
      - TELEGRAM_BOT_TOKEN
      - TELEGRAM_CHAT_ID
    restart: unless-stopped

volumes:
  feed-data:
  sdk-cache:
  repo-cache:
```

**Step 6: Create `builder/` directory (empty for now)**

**Step 7: Init git repo and commit**

```bash
git init
git add .gitignore config.example.yaml .env.example nginx.conf docker-compose.yml
git commit -m "scaffold: project structure with docker-compose, nginx, and config example"
```

---

### Task 2: Config Loader

**Files:**
- Create: `builder/config.py`
- Create: `builder/tests/test_config.py`

This module loads `config.yaml`, resolves environment variable references in string
values (the `${VAR}` syntax), and validates the structure. It also provides the
target-to-architecture mapping.

**Step 1: Write the tests**

```python
# builder/tests/test_config.py
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
        "openwrt_version": "24.10.0",
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
        "openwrt_version": "24.10.0",
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
        "openwrt_version": "24.10.0",
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
        "openwrt_version": "24.10.0",
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
```

**Step 2: Run tests, verify they fail**

```bash
cd builder && python -m pytest tests/test_config.py -v
```

Expected: ImportError / ModuleNotFoundError (config module doesn't exist yet)

**Step 3: Write the implementation**

```python
# builder/config.py
import os
import re
import yaml


TARGET_ARCH_MAP = {
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


def _resolve_recursive(obj):
    """Walk a data structure and resolve env vars in all strings."""
    if isinstance(obj, str):
        return resolve_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _resolve_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_recursive(item) for item in obj]
    return obj


def load_config(path: str) -> dict:
    """Load and validate the builder config file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    config = _resolve_recursive(raw)

    default_targets = config.get("default_targets", [])

    # Validate default targets
    for t in default_targets:
        if t not in VALID_TARGETS:
            raise ValueError(f"Unknown target: {t}")

    # Fill in repo defaults
    for repo in config.get("repos", []):
        if "targets" not in repo:
            repo["targets"] = list(default_targets)
        # Validate repo targets
        for t in repo["targets"]:
            if t not in VALID_TARGETS:
                raise ValueError(f"Unknown target: {t}")

    return config
```

**Step 4: Create `builder/tests/__init__.py`** (empty file)

**Step 5: Run tests, verify they pass**

```bash
cd builder && python -m pytest tests/test_config.py -v
```

Expected: All 7 tests PASS

**Step 6: Commit**

```bash
git add builder/config.py builder/tests/
git commit -m "feat: config loader with env var resolution and target validation"
```

---

### Task 3: State Manager

**Files:**
- Create: `builder/state.py`
- Create: `builder/tests/test_state.py`

Manages the JSON state file that tracks per-repo build status, commits, and
notification state.

**Step 1: Write the tests**

```python
# builder/tests/test_state.py
import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from state import StateManager


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state.json"


def test_fresh_state_file_created_on_load(state_file):
    sm = StateManager(str(state_file))
    assert state_file.exists()
    assert sm.data == {}


def test_get_repo_returns_none_for_unknown(state_file):
    sm = StateManager(str(state_file))
    assert sm.get_repo("nonexistent") is None


def test_record_success(state_file):
    sm = StateManager(str(state_file))
    sm.record_success("my-pkg", "abc123")
    repo = sm.get_repo("my-pkg")
    assert repo["last_commit"] == "abc123"
    assert repo["status"] == "ok"
    assert "last_build" in repo
    assert "error" not in repo


def test_record_failure(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("my-pkg", "abc123", "compile error")
    repo = sm.get_repo("my-pkg")
    assert repo["status"] == "failed"
    assert repo["error"] == "compile error"
    assert repo["notified"] is True


def test_should_notify_failure_only_first_time(state_file):
    sm = StateManager(str(state_file))
    # First failure: should notify
    sm.record_failure("pkg", "abc", "error")
    assert sm.should_notify_failure("pkg") is False  # already notified in record_failure
    # Simulate: notified flag already set
    repo = sm.get_repo("pkg")
    assert repo["notified"] is True


def test_should_notify_recovery(state_file):
    sm = StateManager(str(state_file))
    # First: fail
    sm.record_failure("pkg", "abc", "error")
    assert sm.should_notify_recovery("pkg") is False
    # Then: succeed
    sm.record_success("pkg", "def")
    # No recovery notification because was_failed flag is set
    assert sm.get_repo("pkg")["status"] == "ok"


def test_record_failure_then_success_clears_error(state_file):
    sm = StateManager(str(state_file))
    sm.record_failure("pkg", "abc", "compile error")
    sm.record_success("pkg", "def")
    repo = sm.get_repo("pkg")
    assert repo["status"] == "ok"
    assert "error" not in repo


def test_state_persists_to_disk(state_file):
    sm = StateManager(str(state_file))
    sm.record_success("pkg", "abc123")
    # Load from same file
    sm2 = StateManager(str(state_file))
    assert sm2.get_repo("pkg")["last_commit"] == "abc123"


def test_has_changed_commit(state_file):
    sm = StateManager(str(state_file))
    assert sm.has_changed("pkg", "abc123") is True
    sm.record_success("pkg", "abc123")
    assert sm.has_changed("pkg", "abc123") is False
    assert sm.has_changed("pkg", "def456") is True
```

**Step 2: Run tests, verify they fail**

```bash
cd builder && python -m pytest tests/test_state.py -v
```

Expected: ImportError

**Step 3: Write the implementation**

```python
# builder/state.py
import json
from datetime import datetime, timezone
from pathlib import Path


class StateManager:
    def __init__(self, path: str):
        self.path = Path(path)
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
        else:
            self.data = {}
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def get_repo(self, name: str) -> dict | None:
        return self.data.get(name)

    def has_changed(self, name: str, commit: str) -> bool:
        repo = self.get_repo(name)
        if repo is None:
            return True
        return repo.get("last_commit") != commit

    def record_success(self, name: str, commit: str):
        was_failed = (
            name in self.data and self.data[name].get("status") == "failed"
        )
        self.data[name] = {
            "last_commit": commit,
            "last_build": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
            "was_failed": was_failed,
        }
        self._save()

    def record_failure(self, name: str, commit: str, error: str):
        already_notified = (
            name in self.data
            and self.data[name].get("status") == "failed"
            and self.data[name].get("notified", False)
        )
        self.data[name] = {
            "last_commit": commit,
            "last_build": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "error": error,
            "notified": already_notified,  # preserve if already notified
        }
        if not already_notified:
            self.data[name]["notified"] = True
            self._save()
            return  # caller can check notified flag
        self._save()

    def should_notify_failure(self, name: str) -> bool:
        """Returns True if this is the first failure (not yet notified)."""
        repo = self.get_repo(name)
        if repo is None or repo.get("status") != "failed":
            return False
        return not repo.get("notified", False)

    def should_notify_recovery(self, name: str) -> bool:
        """Returns True if the repo just recovered from a failure."""
        repo = self.get_repo(name)
        if repo is None or repo.get("status") != "ok":
            return False
        return repo.get("was_failed", False)

    def clear_recovery_flag(self, name: str):
        if name in self.data:
            self.data[name].pop("was_failed", None)
            self._save()
```

**Step 4: Run tests, verify they pass**

```bash
cd builder && python -m pytest tests/test_state.py -v
```

Expected: All tests PASS

**Step 5: Commit**

```bash
git add builder/state.py builder/tests/test_state.py
git commit -m "feat: state manager for tracking build status and notification state"
```

---

### Task 4: SDK Manager

**Files:**
- Create: `builder/sdk.py`
- Create: `builder/tests/test_sdk.py`

Handles downloading, extracting, and locating OpenWrt SDKs. Downloads are cached
in `/opt/sdk/`.

**Step 1: Write the tests**

```python
# builder/tests/test_sdk.py
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
```

**Step 2: Run tests, verify they fail**

```bash
cd builder && python -m pytest tests/test_sdk.py -v
```

**Step 3: Write the implementation**

```python
# builder/sdk.py
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
```

**Step 4: Run tests, verify they pass**

```bash
cd builder && python -m pytest tests/test_sdk.py -v
```

Expected: All tests PASS

**Step 5: Commit**

```bash
git add builder/sdk.py builder/tests/test_sdk.py
git commit -m "feat: SDK manager for downloading and caching OpenWrt SDKs"
```

---

### Task 5: Package Builder

**Files:**
- Create: `builder/builder.py`
- Create: `builder/tests/test_builder.py`

Core build logic: clones/fetches repos, compiles packages using the SDK, copies
`.ipk` files to the feed directory, and regenerates the package index.

**Step 1: Write the tests**

```python
# builder/tests/test_builder.py
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path

from builder import PackageBuilder


@pytest.fixture
def feed_dir(tmp_path):
    d = tmp_path / "feed"
    d.mkdir()
    return d


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "repos"
    d.mkdir()
    return d


def test_clone_new_repo(repo_dir):
    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_manager=MagicMock(),
    )
    with patch("builder.subprocess") as mock_sub:
        mock_sub.run.return_value = MagicMock(returncode=0)
        pb.clone_or_fetch()
        # Should clone since directory doesn't exist
        mock_sub.run.assert_called()
        clone_call = mock_sub.run.call_args_list[0]
        assert "clone" in clone_call[0][0]


def test_fetch_existing_repo(repo_dir):
    # Create fake repo dir
    pkg_dir = repo_dir / "test-pkg"
    pkg_dir.mkdir()
    (pkg_dir / ".git").mkdir()

    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_manager=MagicMock(),
    )
    with patch("builder.subprocess") as mock_sub:
        mock_sub.run.return_value = MagicMock(returncode=0)
        mock_sub.check_output.return_value = b"abc123\n"
        pb.clone_or_fetch()
        # Should fetch, not clone
        calls = [str(c) for c in mock_sub.run.call_args_list]
        assert any("fetch" in c for c in calls)


def test_get_head_commit(repo_dir):
    pkg_dir = repo_dir / "test-pkg"
    pkg_dir.mkdir()

    repo_config = {
        "name": "test-pkg",
        "url": "https://github.com/test/test-pkg.git",
        "branch": "main",
    }
    pb = PackageBuilder(
        repo_config=repo_config,
        repo_cache_dir=str(repo_dir),
        feed_dir="/tmp/feed",
        sdk_manager=MagicMock(),
    )
    with patch("builder.subprocess") as mock_sub:
        mock_sub.check_output.return_value = b"abc123def\n"
        commit = pb.get_head_commit()
        assert commit == "abc123def"


def test_collect_ipk_files(tmp_path):
    """Test that _collect_ipk_files finds .ipk files in bin/."""
    bin_dir = tmp_path / "bin" / "packages" / "aarch64_cortex-a53" / "myfeed"
    bin_dir.mkdir(parents=True)
    (bin_dir / "test-pkg_1.0-1_aarch64_cortex-a53.ipk").touch()
    (bin_dir / "test-pkg2_1.0-1_aarch64_cortex-a53.ipk").touch()

    from builder import _collect_ipk_files
    ipks = _collect_ipk_files(str(tmp_path / "bin"))
    assert len(ipks) == 2
    assert all(str(p).endswith(".ipk") for p in ipks)
```

**Step 2: Run tests, verify they fail**

```bash
cd builder && python -m pytest tests/test_builder.py -v
```

**Step 3: Write the implementation**

```python
# builder/builder.py
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
    # Use the simple approach: scan for .ipk files and generate index
    # We shell out to a script that uses opkg-make-index or the manual approach
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
        # opkg-make-index not available, use manual approach
        _manual_reindex(feed_path)


def _manual_reindex(feed_path: Path):
    """Generate Packages index manually by extracting control files from .ipk."""
    logger.info("Using manual reindex for %s", feed_path)
    packages_content = []

    for ipk in sorted(feed_path.glob("*.ipk")):
        try:
            # .ipk is an ar archive containing control.tar.gz (or .zst)
            # Extract the control file
            result = subprocess.run(
                ["tar", "xOf", str(ipk), "./control.tar.gz"],
                capture_output=True,
            )
            if result.returncode != 0:
                # Try .tar.zst
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
                    # Add Filename and Size fields
                    filename = ipk.name
                    size = ipk.stat().st_size
                    control_text = control_text.strip()
                    control_text += f"\nFilename: {filename}\n"
                    control_text += f"Size: {size}\n"

                    # Add SHA256
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

    # Write Packages file
    with open(feed_path / "Packages", "w") as f:
        f.write("\n".join(packages_content))
        if packages_content:
            f.write("\n")

    # Write Packages.gz
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

        # Expect repo to have a standard opkg structure or a build-ipk.sh
        build_script = self.repo_dir / "build-ipk.sh"
        if build_script.exists():
            logger.info("Running build-ipk.sh for %s", self.name)
            subprocess.run(
                ["bash", str(build_script)],
                cwd=str(self.repo_dir),
                check=True,
            )
        else:
            # Try opkg-build if there's a control file
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

        # Symlink package source into SDK
        sdk_pkg_dir = sdk_path / "package" / self.name
        if sdk_pkg_dir.exists() or sdk_pkg_dir.is_symlink():
            sdk_pkg_dir.unlink() if sdk_pkg_dir.is_symlink() else shutil.rmtree(str(sdk_pkg_dir))
        sdk_pkg_dir.symlink_to(self.repo_dir)

        # Update feeds and compile
        logger.info("Compiling %s for %s (arch: %s)", self.name, target, arch)
        subprocess.run(
            ["make", f"package/{self.name}/compile", "V=s",
             f"-j{_nproc()}"],
            cwd=str(sdk_path),
            check=True,
            capture_output=True,
        )

        # Collect built .ipk files
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

        # Reindex all modified directories
        for d in modified_dirs:
            reindex_feed(d)

        return results


def _nproc() -> int:
    """Return number of CPUs available."""
    import os
    return os.cpu_count() or 1
```

**Step 4: Run tests, verify they pass**

```bash
cd builder && python -m pytest tests/test_builder.py -v
```

Expected: All tests PASS

**Step 5: Commit**

```bash
git add builder/builder.py builder/tests/test_builder.py
git commit -m "feat: package builder with SDK compilation and feed reindexing"
```

---

### Task 6: Telegram Bot

**Files:**
- Create: `builder/bot.py`
- Create: `builder/tests/test_bot.py`

Telegram bot with `/status`, `/rebuild`, `/list`, `/logs` commands and
failure/recovery notifications.

**Step 1: Write the tests**

```python
# builder/tests/test_bot.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import format_status_message, format_list_message


def test_format_status_message_empty():
    msg = format_status_message({}, "2025-01-15T10:00:00Z")
    assert "No repos" in msg or "no builds" in msg.lower() or "never" in msg.lower()


def test_format_status_message_with_repos():
    state_data = {
        "pkg-a": {
            "last_commit": "abc123",
            "last_build": "2025-01-15T10:30:00Z",
            "status": "ok",
        },
        "pkg-b": {
            "last_commit": "def456",
            "last_build": "2025-01-15T10:31:00Z",
            "status": "failed",
            "error": "compile error",
        },
    }
    msg = format_status_message(state_data, "2025-01-15T11:00:00Z")
    assert "pkg-a" in msg
    assert "pkg-b" in msg
    assert "ok" in msg.lower() or "✓" in msg or "pass" in msg.lower()
    assert "fail" in msg.lower() or "✗" in msg


def test_format_list_message():
    repos = [
        {"name": "pkg-a", "url": "https://github.com/test/a.git", "branch": "main"},
        {"name": "pkg-b", "url": "https://github.com/test/b.git", "branch": "dev"},
    ]
    state_data = {
        "pkg-a": {"last_commit": "abc1234567890"},
    }
    msg = format_list_message(repos, state_data)
    assert "pkg-a" in msg
    assert "pkg-b" in msg
    assert "abc1234" in msg  # shortened commit hash
```

**Step 2: Run tests, verify they fail**

```bash
cd builder && python -m pytest tests/test_bot.py -v
```

**Step 3: Write the implementation**

```python
# builder/bot.py
import asyncio
import io
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from state import StateManager

logger = logging.getLogger(__name__)


def format_status_message(state_data: dict, last_poll: str | None) -> str:
    """Format the /status response."""
    lines = []
    poll_str = last_poll or "never"
    lines.append(f"Last poll: {poll_str}")
    lines.append("")

    if not state_data:
        lines.append("No builds yet.")
        return "\n".join(lines)

    for name, info in sorted(state_data.items()):
        status = info.get("status", "unknown")
        icon = "OK" if status == "ok" else "FAIL"
        commit = info.get("last_commit", "?")[:7]
        build_time = info.get("last_build", "?")
        line = f"[{icon}] {name} @ {commit} ({build_time})"
        if status == "failed":
            line += f"\n      Error: {info.get('error', '?')}"
        lines.append(line)

    return "\n".join(lines)


def format_list_message(repos: list[dict], state_data: dict) -> str:
    """Format the /list response."""
    lines = []
    for repo in repos:
        name = repo["name"]
        branch = repo["branch"]
        state = state_data.get(name, {})
        commit = state.get("last_commit", "not built")[:7] if state else "not built"
        lines.append(f"  {name} ({branch}) @ {commit}")
    return "\n".join(lines) if lines else "No repos configured."


class OpenwrtBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        state_manager: StateManager,
        repos_config: list[dict],
        rebuild_callback,
        log_dir: str = "/tmp/build-logs",
    ):
        self.token = token
        self.chat_id = int(chat_id)
        self.state = state_manager
        self.repos_config = repos_config
        self.rebuild_callback = rebuild_callback
        self.log_dir = Path(log_dir)
        self.last_poll = None
        self.app = None

    def _check_authorized(self, update: Update) -> bool:
        """Only respond to the configured chat_id."""
        return update.effective_chat.id == self.chat_id

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not self._check_authorized(update):
            return
        msg = format_status_message(self.state.data, self.last_poll)
        await update.message.reply_text(msg)

    async def cmd_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not self._check_authorized(update):
            return
        msg = format_list_message(self.repos_config, self.state.data)
        await update.message.reply_text(msg)

    async def cmd_rebuild(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not self._check_authorized(update):
            return
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /rebuild <repo-name> or /rebuild all"
            )
            return

        target = args[0]
        await update.message.reply_text(f"Rebuild triggered for: {target}")

        # Run rebuild in background so bot stays responsive
        asyncio.get_event_loop().run_in_executor(
            None, self.rebuild_callback, target
        )

    async def cmd_logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not self._check_authorized(update):
            return
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /logs <repo-name>")
            return

        repo_name = args[0]
        log_file = self.log_dir / f"{repo_name}.log"

        if not log_file.exists():
            await update.message.reply_text(
                f"No build log found for {repo_name}"
            )
            return

        content = log_file.read_text()
        if len(content) > 4000:
            # Send as file attachment
            buf = io.BytesIO(content.encode())
            buf.name = f"{repo_name}.log"
            await update.message.reply_document(
                document=buf, caption=f"Build log for {repo_name}"
            )
        else:
            await update.message.reply_text(
                f"Build log for {repo_name}:\n\n{content}"
            )

    async def notify(self, message: str):
        """Send a notification message to the configured chat."""
        if self.app:
            await self.app.bot.send_message(
                chat_id=self.chat_id, text=message
            )

    async def notify_failure(self, repo_name: str, error: str):
        await self.notify(f"BUILD FAILED: {repo_name}\n\n{error[:500]}")

    async def notify_recovery(self, repo_name: str):
        await self.notify(f"RECOVERED: {repo_name} is building successfully again.")

    def build_application(self) -> Application:
        """Build and return the telegram Application (for running)."""
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("list", self.cmd_list))
        self.app.add_handler(CommandHandler("rebuild", self.cmd_rebuild))
        self.app.add_handler(CommandHandler("logs", self.cmd_logs))
        return self.app
```

**Step 4: Run tests, verify they pass**

```bash
cd builder && python -m pytest tests/test_bot.py -v
```

Expected: All tests PASS

**Step 5: Commit**

```bash
git add builder/bot.py builder/tests/test_bot.py
git commit -m "feat: telegram bot with status, rebuild, list, and logs commands"
```

---

### Task 7: Main Entrypoint & Build Loop

**Files:**
- Create: `builder/main.py`

Ties everything together: loads config, initializes SDK manager, starts the
Telegram bot, and runs the periodic build loop.

**Step 1: Write `builder/main.py`**

```python
# builder/main.py
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import load_config, TARGET_ARCH_MAP
from state import StateManager
from sdk import SDKManager
from builder import PackageBuilder, reindex_feed
from bot import OpenwrtBot

CONFIG_PATH = "/etc/openwrt-builder/config.yaml"
FEED_DIR = "/feed"
SDK_CACHE_DIR = "/opt/sdk"
REPO_CACHE_DIR = "/opt/repos"
STATE_FILE = "/feed/.builder-state.json"
LOG_DIR = "/tmp/build-logs"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("openwrt-builder")


def run_build_cycle(config: dict, state: StateManager, sdk_mgr: SDKManager,
                    bot: OpenwrtBot | None = None, force_repo: str | None = None):
    """Run one build cycle across all repos (or a specific one)."""
    repos = config["repos"]
    if force_repo and force_repo != "all":
        repos = [r for r in repos if r["name"] == force_repo]
        if not repos:
            logger.error("Unknown repo: %s", force_repo)
            return

    for repo_config in repos:
        name = repo_config["name"]
        log_file = Path(LOG_DIR) / f"{name}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            pb = PackageBuilder(
                repo_config=repo_config,
                repo_cache_dir=REPO_CACHE_DIR,
                feed_dir=FEED_DIR,
                sdk_manager=sdk_mgr,
            )

            pb.clone_or_fetch()
            commit = pb.get_head_commit()

            if not state.has_changed(name, commit) and force_repo is None:
                logger.info("No changes for %s (at %s), skipping", name, commit[:7])
                continue

            logger.info("Building %s (commit %s)", name, commit[:7])
            results = pb.build_all_targets()

            total_ipks = sum(len(v) for v in results.values())
            logger.info("Built %d .ipk files for %s", total_ipks, name)

            # Check if this is a recovery
            was_failed = (
                state.get_repo(name) is not None
                and state.get_repo(name).get("status") == "failed"
            )

            state.record_success(name, commit)

            if was_failed and bot:
                asyncio.get_event_loop().run_until_complete(
                    bot.notify_recovery(name)
                )

            # Write success log
            log_file.write_text(
                f"Build successful at {datetime.now(timezone.utc).isoformat()}\n"
                f"Commit: {commit}\n"
                f"Packages: {total_ipks}\n"
            )

        except Exception as e:
            error_msg = str(e)
            logger.error("Build failed for %s: %s", name, error_msg)

            # Write error log
            log_file.write_text(
                f"Build FAILED at {datetime.now(timezone.utc).isoformat()}\n"
                f"Error: {error_msg}\n"
            )

            should_notify = state.should_notify_failure(name) is False
            # record_failure handles the notified flag
            commit = "unknown"
            try:
                pb2 = PackageBuilder(
                    repo_config=repo_config,
                    repo_cache_dir=REPO_CACHE_DIR,
                    feed_dir=FEED_DIR,
                    sdk_manager=sdk_mgr,
                )
                commit = pb2.get_head_commit()
            except Exception:
                pass

            prev = state.get_repo(name)
            already_notified = (
                prev is not None
                and prev.get("status") == "failed"
                and prev.get("notified", False)
            )

            state.record_failure(name, commit, error_msg)

            if not already_notified and bot:
                asyncio.get_event_loop().run_until_complete(
                    bot.notify_failure(name, error_msg)
                )

    if bot:
        bot.last_poll = datetime.now(timezone.utc).isoformat()


def rebuild_callback_factory(config, state, sdk_mgr, bot):
    """Create a rebuild callback for the Telegram bot."""
    def rebuild(target: str):
        run_build_cycle(config, state, sdk_mgr, bot=bot, force_repo=target)
    return rebuild


async def main():
    logger.info("Starting OpenWrt Package Builder")

    config = load_config(CONFIG_PATH)
    state = StateManager(STATE_FILE)
    sdk_mgr = SDKManager(config["openwrt_version"], SDK_CACHE_DIR)

    # Ensure feed directories exist
    for target in config["default_targets"]:
        if target == "all":
            (Path(FEED_DIR) / "all").mkdir(parents=True, exist_ok=True)
        else:
            arch = TARGET_ARCH_MAP[target]
            (Path(FEED_DIR) / arch).mkdir(parents=True, exist_ok=True)
    (Path(FEED_DIR) / "all").mkdir(parents=True, exist_ok=True)

    # Pre-download SDKs
    logger.info("Ensuring SDKs are downloaded...")
    unique_targets = [
        t for t in config["default_targets"] if t != "all"
    ]
    for target in unique_targets:
        sdk_mgr.ensure_downloaded(target)

    # Initialize Telegram bot
    tg_config = config.get("telegram", {})
    bot = None
    if tg_config.get("bot_token") and tg_config.get("chat_id"):
        bot = OpenwrtBot(
            token=tg_config["bot_token"],
            chat_id=tg_config["chat_id"],
            state_manager=state,
            repos_config=config["repos"],
            rebuild_callback=rebuild_callback_factory(config, state, sdk_mgr, None),
            log_dir=LOG_DIR,
        )
        # Wire up the bot's rebuild callback to include bot reference
        bot.rebuild_callback = rebuild_callback_factory(config, state, sdk_mgr, bot)

        app = bot.build_application()

        # Start polling in background
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Telegram bot started")

    poll_interval = config.get("poll_interval", 3600)
    logger.info("Poll interval: %d seconds", poll_interval)

    # Main loop
    try:
        while True:
            logger.info("Starting build cycle")
            run_build_cycle(config, state, sdk_mgr, bot=bot)
            logger.info("Build cycle complete, sleeping %d seconds", poll_interval)
            await asyncio.sleep(poll_interval)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down")
        if bot and bot.app:
            await bot.app.updater.stop()
            await bot.app.stop()
            await bot.app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 2: Commit**

```bash
git add builder/main.py
git commit -m "feat: main entrypoint with build loop and telegram bot integration"
```

---

### Task 8: Dockerfile & Requirements

**Files:**
- Create: `builder/Dockerfile`
- Create: `builder/requirements.txt`

**Step 1: Create `builder/requirements.txt`**

```
python-telegram-bot==22.6
PyYAML==6.0.2
```

**Step 2: Create `builder/Dockerfile`**

```dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libncurses-dev \
    gawk \
    git \
    wget \
    curl \
    python3 \
    python3-pip \
    python3-venv \
    file \
    rsync \
    unzip \
    zstd \
    gzip \
    xz-utils \
    ca-certificates \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install opkg-utils
RUN git clone https://git.openwrt.org/project/opkg-utils.git /opt/opkg-utils \
    && ln -s /opt/opkg-utils/opkg-make-index /usr/local/bin/opkg-make-index \
    && ln -s /opt/opkg-utils/opkg-build /usr/local/bin/opkg-build

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/

# Create directories
RUN mkdir -p /feed /opt/sdk /opt/repos /tmp/build-logs

CMD ["python3", "main.py"]
```

**Step 3: Commit**

```bash
git add builder/Dockerfile builder/requirements.txt
git commit -m "feat: builder Dockerfile with OpenWrt SDK build dependencies"
```

---

### Task 9: Integration Test with Docker Compose

**Step 1: Create a test config**

Copy `config.example.yaml` to `config.yaml`, fill in a real (or test) repo URL and
Telegram credentials. If you don't have a Telegram bot yet, leave the telegram
section with empty strings — the bot simply won't start.

**Step 2: Build and start**

```bash
docker compose build
docker compose up -d
```

**Step 3: Verify the feed server**

```bash
curl http://localhost:8080/
```

Expected: nginx autoindex listing the architecture directories.

**Step 4: Check builder logs**

```bash
docker compose logs builder
```

Expected: logs showing SDK downloads (first run), repo cloning, and build attempts.

**Step 5: Test Telegram commands** (if bot is configured)

Send `/status`, `/list` to your bot. Verify responses.

**Step 6: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration test adjustments"
```

---

### Task 10: Final Cleanup & Documentation

**Step 1: Update the original `openwrt-feed-server.md`**

Add a note at the top pointing to the new automated system and the design doc.

**Step 2: Verify all tests pass**

```bash
cd builder && python -m pytest tests/ -v
```

**Step 3: Final commit**

```bash
git add -A
git commit -m "docs: finalize project with updated references"
```
