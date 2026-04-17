# OpenWrt Package Builder

Docker-based system that polls git repos and automatically rebuilds OpenWrt
packages when changes are detected. Serves packages via an nginx feed server.
Controlled via a Telegram bot. SDK builds run on on-demand Hetzner x86_64
instances for native compilation speed.

## Architecture

Two Docker containers on the host (e.g. Raspberry Pi):

- **feed-server** -- `nginx:alpine`, serves `.ipk`/`.apk` packages over HTTP
- **builder** -- Debian trixie-slim, runs a Python app combining a Telegram
  bot (long-polling) with a periodic build loop. Ships as UID 1000
  non-root by default.

OpenWrt changed package format at 25.12: `<= 24.10` emits `.ipk`
(opkg/Packages.gz), `>= 25.12` emits `.apk` (apk-tools 3.x /
APKINDEX.tar.gz). The builder handles both — each repo can target one or
more OpenWrt versions via `openwrt_versions:` in config, and every arch
dir gets both an opkg `Packages` index and an `APKINDEX.tar.gz` when the
format is present.

For SDK builds, the builder spins up an on-demand Hetzner CX instance:

```
Host (ARM/x86)                       Hetzner (x86_64)
+--------------+   hcloud create     +---------------+
| builder      | ------------------> | cx23 on-demand|
|  git poll    |   rsync repos       | SDK + make    |
|  telegram    |   scp packages back | .ipk / .apk   |
| feed-server  | <------------------ |               |
+--------------+   hcloud delete     +---------------+
```

Per build cycle: create server (~30s) -> install deps + SDK (~35s) -> build
all packages (~15s each) -> SCP packages back -> `apk mkndx` for any
`.apk` arches -> destroy server. ~2-3 min total, ~EUR 0.001 per cycle.

## Build method detection

The builder auto-detects how to build each repo (priority order):

1. **SDK** -- has OpenWrt `Makefile` containing `include $(INCLUDE_DIR)/package.mk`
2. **Script** -- has `build-ipk.sh` at repo root
3. **opkg-build** -- has `CONTROL/` directory at repo root
4. **Skip** -- none of the above

## Setup

The `builder-app` container runs as UID/GID 1000 (baked into the image at
build time). The bind-mounted `runtime/` and `runtime/feed/` directories
must be writable by that UID — `chown -R 1000:1000 runtime` once after
cloning. If your host user is not 1000, override the **build args** (not
just runtime `user:`) and rebuild — see `docker-compose.override.example.yml`.

```bash
# 1. Configure
cp config.example.yaml runtime/config.yaml
# Edit: set repo URLs, telegram credentials

# 2. SSH key for private repos
cp ~/.ssh/id_ed25519 runtime/ssh.key
chmod 600 runtime/ssh.key

# 3. For remote builds (optional)
echo "your-hetzner-api-token" > runtime/hetzner.token

# 4. Custom networking / non-default UID (optional)
cp docker-compose.override.example.yml docker-compose.override.yml

# 5. Ensure runtime/ is writable by UID 1000
sudo chown -R 1000:1000 runtime

# 6. Run
docker compose build && docker compose up -d
```

## AP configuration

On OpenWrt <= 24.10 (opkg):

```bash
echo "src/gz custom http://<server>:8081/all" >> /etc/opkg/customfeeds.conf
opkg update
```

On OpenWrt >= 25.12 (apk):

```bash
echo "http://<server>:8081/all" >> /etc/apk/repositories.d/customfeeds.list
apk update
```

Add the arch-specific dir (e.g. `/aarch64_cortex-a53`) alongside `/all`
when the package is arch-dependent. The signing public key in
`runtime/feed-signing.pub` must be installed on each device
(`/etc/opkg/keys/` for opkg, `/etc/apk/keys/` for apk) — see
`install-feed.sh` for the opkg flow.

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
