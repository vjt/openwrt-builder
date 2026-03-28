# OpenWrt Package Builder

Docker-based system that polls git repos and automatically rebuilds OpenWrt
packages when changes are detected. Serves packages via an nginx feed server.
Controlled via a Telegram bot. SDK builds run on on-demand Hetzner x86_64
instances for native compilation speed.

## Architecture

Two Docker containers on the host (e.g. Raspberry Pi):

- **feed-server** -- `nginx:alpine`, serves `.ipk` packages over HTTP
- **builder** -- Debian trixie-slim, runs a Python app combining a Telegram
  bot (long-polling) with a periodic build loop

For SDK builds, the builder spins up an on-demand Hetzner CX instance:

```
Host (ARM/x86)                       Hetzner (x86_64)
+--------------+   hcloud create     +--------------+
| builder      | ------------------> | cx23 on-demand|
|  git poll    |   rsync repos       | SDK + make    |
|  telegram    |   scp .ipk back     | .ipk output   |
| feed-server  | <------------------ |               |
+--------------+   hcloud delete     +--------------+
```

Per build cycle: create server (~30s) -> install deps + SDK (~35s) -> build
all packages (~15s each) -> SCP .ipks back -> destroy server. ~2 min total,
~EUR 0.001 per cycle.

## Build method detection

The builder auto-detects how to build each repo (priority order):

1. **SDK** -- has OpenWrt `Makefile` containing `include $(INCLUDE_DIR)/package.mk`
2. **Script** -- has `build-ipk.sh` at repo root
3. **opkg-build** -- has `CONTROL/` directory at repo root
4. **Skip** -- none of the above

## Setup

```bash
# 1. Configure
cp config.example.yaml runtime/config.yaml
# Edit: set repo URLs, telegram credentials

# 2. SSH key for private repos
cp ~/.ssh/id_ed25519 runtime/ssh.key
chmod 600 runtime/ssh.key

# 3. For remote builds (optional)
echo "your-hetzner-api-token" > runtime/hetzner.token

# 4. Custom networking (optional)
cp docker-compose.override.example.yml docker-compose.override.yml

# 5. Run
docker compose build && docker compose up -d
```

## AP configuration

On each OpenWrt device:

```bash
echo "src/gz custom http://<server>:8081/all" >> /etc/opkg/customfeeds.conf
opkg update
```

## Telegram commands

| Command | Description |
|---------|-------------|
| `/status` | Show build status for all repos |
| `/list` | List configured repos |
| `/rebuild <name>` | Trigger rebuild for a repo (or `all`) |
| `/logs <name>` | Show build log for a repo |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio pyyaml python-telegram-bot pyright

# Tests
cd builder && ../.venv/bin/python3 -m pytest tests/ -v

# Type checking
.venv/bin/pyright
```

## Supported targets

| Config Target | CPU Architecture | Use Case |
|---|---|---|
| `mediatek/filogic` | `aarch64_cortex-a53` | MT7981, MT7986, MT7988 |
| `bcm27xx/bcm2709` | `arm_cortex-a7_neon-vfpv4` | Raspberry Pi 2 |
| `ramips/mt7621` | `mipsel_24kc` | MT7621 MIPS devices |
| `ath79/tiny` | `mips_24kc` | Atheros AR71xx/AR93xx |

Use `targets: [all]` for pure Lua/config packages (architecture-independent).

## License

MIT
