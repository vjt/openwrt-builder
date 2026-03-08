# OpenWrt Automated Package Builder

## Project Overview

Docker-based system that polls git repos and automatically rebuilds OpenWrt packages (Lua/config and compiled C) when changes are detected. Serves packages via an nginx feed server. Controlled via a Telegram bot. SDK builds run on on-demand Hetzner x86_64 instances for native compilation speed.

## Architecture

Two Docker containers on the host (e.g. Raspberry Pi):
- **feed-server**: `nginx:alpine`, read-only bind mount on `/feed/`, serves packages
- **builder**: Debian trixie-slim, runs Python app combining a Telegram bot (long-polling) with a periodic build loop

For SDK builds, the builder spins up an on-demand Hetzner CX instance:
```
Host (ARM/x86)                       Hetzner (x86_64)
┌──────────────┐   hcloud create     ┌──────────────┐
│ builder      │ ──────────────────> │ cx23 on-demand│
│  git poll    │   rsync repos       │ SDK + make    │
│  telegram    │   scp .ipk back     │ .ipk output   │
│ feed-server  │ <────────────────── │               │
└──────────────┘   hcloud delete     └──────────────┘
```

Per build cycle: create server (~30s) → install deps + SDK (~35s) → build all packages (~15s each) → SCP .ipks back → destroy server. ~2 min total, ~€0.001 per cycle.

Storage:
- **Bind mounts** (`runtime/`): feed output, config, SSH key, Hetzner token — inspectable and backupable
- **Docker volumes** (`cache-sdk`, `cache-repos`): disposable local SDK cache and cloned repos — exclude from backups with `/var/lib/docker/volumes/*cache-*`

## Key Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Both services, bind mounts for feed/config/ssh/hetzner, `cache-*` volumes |
| `docker-compose.override.example.yml` | Example per-host overrides (macvlan networking etc.) |
| `nginx.conf` | Minimal autoindex config for the feed directory |
| `config.example.yaml` | Example config — copy to `runtime/config.yaml` to use |
| `builder/config.py` | Loads YAML config, resolves `${ENV_VAR}` syntax, validates targets |
| `builder/state.py` | JSON state persistence — tracks commits, build status, notification flags |
| `builder/sdk.py` | Downloads/caches OpenWrt SDKs per architecture target |
| `builder/builder.py` | Core build logic — auto-detects build method, delegates to local or remote SDK |
| `builder/remote.py` | Hetzner on-demand builder — server lifecycle, SSH, rsync, SCP |
| `builder/bot.py` | Telegram bot — `/status`, `/rebuild`, `/list`, `/logs` + lifecycle notifications |
| `builder/main.py` | Async entrypoint — ties config, SDK, builder, remote, and bot together in a poll loop |
| `builder/Dockerfile` | Debian trixie-slim + build deps + hcloud CLI |

## Build Method Detection

The builder auto-detects how to build each repo (priority order):
1. **SDK** — has OpenWrt `Makefile` (at root or `openwrt/` subdir) containing `include $(INCLUDE_DIR)/package.mk`
2. **Script** — has `build-ipk.sh` at repo root
3. **opkg-build** — has `CONTROL/` directory at repo root
4. **Skip** — none of the above (logs warning, doesn't crash)

For `targets: [all]` with SDK builds, picks any available SDK — architecture doesn't matter for pure Lua packages.

## Remote Build Flow (Hetzner)

When `hetzner` is configured in `config.yaml` and `runtime/hetzner.token` exists:

1. Builder clones/fetches repos **locally** (handles private repos via local SSH key)
2. On first SDK build in cycle → creates Hetzner server via `hcloud` CLI
3. Installs build deps and downloads SDK on the remote server
4. Rsyncs local repo clone to the remote server
5. Runs `make package/xxx/compile` natively on x86_64
6. SCPs `.ipk` files back to local feed directory
7. Reuses same server for all repos in the cycle
8. Destroys server after cycle completes

Fallback: if Hetzner is not configured, SDK builds run locally (slow on ARM under QEMU).

## Supported Targets

| Config Target | CPU Architecture | SDK |
|---|---|---|
| `mediatek/filogic` | `aarch64_cortex-a53` | Covers MT7981, MT7986, MT7988 |
| `bcm27xx/bcm2709` | `arm_cortex-a7_neon-vfpv4` | Raspberry Pi 2 |
| `ramips/mt7621` | `mipsel_24kc` | MT7621 MIPS devices |
| `ath79/tiny` | `mips_24kc` | Atheros AR71xx/AR93xx |

## Development

### Running Tests

```bash
cd builder && ../.venv/bin/python3 -m pytest tests/ -v
```

### Dependencies (local dev)

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio pyyaml python-telegram-bot
```

### Test Structure

- `tests/test_config.py` — 8 tests: env var resolution, target validation, config loading
- `tests/test_state.py` — 9 tests: state persistence, notification tracking, change detection
- `tests/test_sdk.py` — 8 tests: URL generation, dirname computation, cache detection
- `tests/test_builder.py` — 15 tests: clone/fetch, commit parsing, ipk collection, build method detection
- `tests/test_bot.py` — 3 tests: status/list message formatting
- `tests/test_remote.py` — 16 tests: server lifecycle, SSH, rsync, SDK setup, build, download

### Deploying

1. Copy `config.example.yaml` to `runtime/config.yaml`, fill in repo URLs and telegram credentials
2. Place SSH private key at `runtime/ssh.key` with `chmod 600`
3. For remote builds: place Hetzner API token in `runtime/hetzner.token`, register the SSH key in Hetzner Cloud console
4. Optionally copy `docker-compose.override.example.yml` to `docker-compose.override.yml` for custom networking
5. `docker compose build && docker compose up -d`
6. On each OpenWrt AP: `echo "src/gz custom http://<server>/all" >> /etc/opkg/customfeeds.conf`

### Config Options

| Key | Default | Description |
|---|---|---|
| `openwrt_version` | — | SDK version to download (e.g. `"24.10.0"`) |
| `default_targets` | — | List of targets for repos without explicit targets |
| `poll_interval` | `3600` | Seconds between build cycles |
| `sdk_force` | `false` | Pass `FORCE=1` to SDK make (for case-insensitive filesystems) |
| `telegram.bot_token` | — | Telegram bot token |
| `telegram.chat_id` | — | Telegram chat ID for notifications |
| `hetzner.ssh_key_name` | `"openwrt-builder"` | Name of SSH key registered in Hetzner Cloud |
| `hetzner.server_type` | `"cx23"` | Hetzner server type (x86 shared) |
| `hetzner.location` | `"fsn1"` | Hetzner datacenter location |

## What's Left / Future Work

- **OpenWrt version upgrades**: Update `openwrt_version` in config and `docker volume rm` the `cache-sdk` volume
- **Additional architectures**: Add to `TARGET_ARCH_MAP` in `config.py` and `EABI_TARGETS` in `sdk.py` if ARM
