# OpenWrt Automated Package Builder — Design

## Problem

Building and serving custom OpenWrt packages (both Lua/config and compiled C) is a
manual process: build locally, copy `.ipk` files to a server, regenerate the index.
We want a system that watches git repos and automatically rebuilds when they change.

## Architecture

Two Docker containers sharing a volume:

```
┌──────────────────────────────┐      ┌──────────────────┐
│           builder            │      │   feed-server    │
│                              │      │                  │
│  cron (configurable)         │      │   nginx:alpine   │
│  → poll git repos            │      │   serves /feed/  │
│  → build changed packages    │      │                  │
│  → reindex feed directories  │      │                  │
│                              │      │                  │
│  telegram bot (long-polling) │      │                  │
│  → /status, /rebuild, etc.   │      │                  │
│  → failure/recovery alerts   │      │                  │
└────────────┬─────────────────┘      └────────┬─────────┘
             │                                 │
             └────────── shared volume ────────┘
                         /feed/
                         ├── all/
                         ├── aarch64_cortex-a53/
                         ├── arm_cortex-a7_neon-vfpv4/
                         ├── mipsel_24kc/
                         └── mips_24kc/
```

- **feed-server**: `nginx:alpine` with `autoindex on`. Read-only mount. No custom logic.
- **builder**: Debian-based. Holds OpenWrt SDKs, build script, cron, Telegram bot.

## Configuration

Single YAML file mounted into the builder:

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
  - name: wifi-dethrash-collector
    url: git@github.com:you/wifi-dethrash-collector.git
    branch: main
    targets: [all]              # arch-independent

  - name: my-custom-dns-tool
    url: git@github.com:you/my-custom-dns-tool.git
    branch: main
    # inherits default_targets

  - name: some-c-package
    url: git@github.com:you/some-c-package.git
    branch: develop
    targets:
      - mediatek/filogic
      - ramips/mt7621
```

Sensitive values (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, SSH keys) come from
environment variables or Docker secrets.

## Target-to-Architecture Mapping

OpenWrt targets map to CPU architectures (which become feed directory names):

| Config Target        | Arch (feed directory)            |
|----------------------|----------------------------------|
| `mediatek/filogic`   | `aarch64_cortex-a53`             |
| `bcm27xx/bcm2709`    | `arm_cortex-a7_neon-vfpv4`       |
| `ramips/mt7621`      | `mipsel_24kc`                    |
| `ath79/tiny`         | `mips_24kc`                      |

4 targets, 4 SDKs. Note: `mediatek/filogic` covers MT7981, MT7986, and MT7988
devices — there is no separate `mediatek/mt7981` subtarget in OpenWrt.

## Build Flow

Runs every poll cycle or on-demand via `/rebuild`:

```
For each repo:
  1. git clone (first run) or git fetch
  2. Compare local HEAD vs remote HEAD
     → unchanged and not forced: skip
  3. git checkout latest
  4. For each target:
     a. target == "all":
        → opkg-utils to build arch-independent .ipk
        → output to /feed/all/
     b. else:
        → OpenWrt SDK for that arch
        → copy package source into SDK's package/ directory
        → make package/<name>/compile V=s
        → copy .ipk to /feed/<arch>/
  5. Record new HEAD in state file
  6. Regenerate Packages + Packages.gz for each modified arch dir
```

Builds run sequentially (one repo, one target at a time).

## SDK Management

- Downloaded on first startup from `downloads.openwrt.org`
- Cached in a named Docker volume (`sdk-cache`)
- One SDK per unique architecture (4 total)
- URL pattern: `https://downloads.openwrt.org/releases/<ver>/targets/<target>/openwrt-sdk-*.tar.zst`

## Telegram Bot

Long-polling bot running alongside cron in the builder container.

### Commands (you → bot)

| Command           | Description                                          |
|-------------------|------------------------------------------------------|
| `/status`         | Last poll time, build results per repo, SDK versions |
| `/rebuild <repo>` | Force rebuild of a specific repo                     |
| `/rebuild all`    | Force rebuild of everything                          |
| `/list`           | List repos and their last known commit               |
| `/logs <repo>`    | Send last build log (file attachment if large)       |

### Notifications (bot → you)

- **Build failure**: first occurrence only per repo, with error summary
- **Recovery**: when a previously-failed repo succeeds again
- **Rebuild triggered**: confirmation when `/rebuild` is issued

State tracking prevents notification spam: a failed repo only notifies once until
it either recovers or is rebuilt.

## State File

Persisted at `/feed/.builder-state.json`:

```json
{
  "wifi-dethrash-collector": {
    "last_commit": "abc123",
    "last_build": "2025-01-15T10:30:00Z",
    "status": "ok"
  },
  "some-c-package": {
    "last_commit": "def456",
    "last_build": "2025-01-15T10:31:00Z",
    "status": "failed",
    "error": "make: *** No rule to make target..."
  }
}
```

## Docker Compose

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
      - ./config.yaml:/etc/openwrt-builder/config.yaml:ro
    environment:
      - TELEGRAM_BOT_TOKEN
      - TELEGRAM_CHAT_ID
    restart: unless-stopped

volumes:
  feed-data:
  sdk-cache:
```

## AP-Side Setup (one-time)

```bash
echo "src/gz custom http://<server>:8080/<arch>" \
  >> /etc/opkg/customfeeds.conf
opkg update
```

Where `<arch>` is the feed directory name for that device (e.g.,
`aarch64_cortex-a53` for Filogic).

## Key Design Decisions

1. **Custom script over CI tool** — cron + shell/Python is simpler, more debuggable,
   and has zero external dependencies beyond git and the SDK.
2. **Two containers over one** — nginx stays up even if the builder crashes or is
   mid-build. Clean separation of concerns.
3. **Sequential builds** — predictable resource usage, readable logs. Parallelism
   unnecessary for <10 repos.
4. **SDK cached in a volume** — survives container rebuilds, avoids re-downloading
   ~2-4GB on every restart.
5. **Telegram bot over webhook** — long-polling needs no inbound ports or TLS certs.
   Simpler for a home/internal server.
