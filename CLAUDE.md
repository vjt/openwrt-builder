# OpenWrt Automated Package Builder

## Project Overview

Docker-based system that polls git repos and automatically rebuilds OpenWrt packages (Lua/config and compiled C) when changes are detected. Serves packages via an nginx feed server. Controlled via a Telegram bot.

## Architecture

Two Docker containers sharing a `runtime/` directory:
- **feed-server**: `nginx:alpine`, read-only mount on `/feed/`, serves packages on port 8080
- **builder**: Debian-based, runs Python app that combines a Telegram bot (long-polling) with a periodic build loop

## Key Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Defines both services + `runtime/` bind mounts (feed, sdk, repos) |
| `nginx.conf` | Minimal autoindex config for the feed directory |
| `config.example.yaml` | Example config — copy to `runtime/config.yaml` to use |
| `builder/config.py` | Loads YAML config, resolves `${ENV_VAR}` syntax, validates targets |
| `builder/state.py` | JSON state persistence — tracks commits, build status, notification flags |
| `builder/sdk.py` | Downloads/caches OpenWrt SDKs per architecture target |
| `builder/builder.py` | Core build logic — clone/fetch repos, compile with SDK, reindex feed |
| `builder/bot.py` | Telegram bot — `/status`, `/rebuild`, `/list`, `/logs` + failure/recovery notifications |
| `builder/main.py` | Async entrypoint — ties config, SDK, builder, and bot together in a poll loop |
| `builder/Dockerfile` | Debian bookworm-slim + build-essential + SDK deps + opkg-utils |

## Supported Targets

| Config Target | CPU Architecture | SDK |
|---|---|---|
| `mediatek/filogic` | `aarch64_cortex-a53` | Covers MT7981, MT7986, MT7988 |
| `bcm27xx/bcm2709` | `arm_cortex-a7_neon-vfpv4` | Raspberry Pi 2 |
| `ramips/mt7621` | `mipsel_24kc` | MT7621 MIPS devices |
| `ath79/tiny` | `mips_24kc` | Atheros AR71xx/AR93xx |

Note: There is no separate `mediatek/mt7981` subtarget in OpenWrt 24.10.0 — MT7981 devices are under `mediatek/filogic`.

## Development

### Running Tests

```bash
cd builder && ../.venv/bin/python3 -m pytest tests/ -v
```

### Dependencies (local dev)

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pyyaml python-telegram-bot
```

### Test Structure

- `tests/test_config.py` — 8 tests: env var resolution, target validation, config loading
- `tests/test_state.py` — 9 tests: state persistence, notification tracking, change detection
- `tests/test_sdk.py` — 8 tests: URL generation, dirname computation, cache detection
- `tests/test_builder.py` — 4 tests: clone/fetch logic, commit parsing, ipk collection
- `tests/test_bot.py` — 3 tests: status/list message formatting

### Deploying

1. Copy `config.example.yaml` to `runtime/config.yaml`, fill in repo URLs
2. Create `.env` with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
3. `docker compose build && docker compose up -d`
4. On each OpenWrt AP: `echo "src/gz custom http://<server>:8080/<arch>" >> /etc/opkg/customfeeds.conf`

First run downloads 4 SDKs (~500MB-1GB each), cached in `runtime/sdk/`.

## Design Documents

- `docs/plans/2026-02-16-openwrt-auto-builder-design.md` — Full architecture design
- `docs/plans/2026-02-16-openwrt-auto-builder-plan.md` — Implementation plan (10 tasks)

## What's Left / Future Work

- **Integration test with Docker**: `docker compose build && docker compose up` with a real config — not yet tested end-to-end
- **SSH keys for private repos**: Mount as Docker secret or volume
- **OpenWrt version upgrades**: Update `openwrt_version` in config and delete `runtime/sdk/`
- **Additional architectures**: Add to `TARGET_ARCH_MAP` in `config.py` and `EABI_TARGETS` in `sdk.py` if ARM
