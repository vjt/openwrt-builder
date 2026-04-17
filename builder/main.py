from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import load_config, TARGET_ARCH_MAP
from state import StateManager
from sdk import SDKManager
from builder import PackageBuilder, reindex_feed
from bot import OpenwrtBot
from remote import RemoteBuilder

CONFIG_PATH = "/runtime/config.yaml"
FEED_DIR = "/feed"
SDK_CACHE_DIR = "/opt/sdk"
REPO_CACHE_DIR = "/opt/repos"
STATE_FILE = "/feed/.builder-state.json"
LOG_DIR = "/tmp/build-logs"

HETZNER_TOKEN_PATH = "/runtime/hetzner.token"
SIGNING_KEY = "/runtime/feed-signing.key"
SIGNING_PUB = "/runtime/feed-signing.pub"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("openwrt-builder")


def _state_key(name: str, version: str) -> str:
    """State/log key per (repo, openwrt version) so each matrix entry tracks
    independently — a successful 24.10 build shouldn't mask a 25.12 failure."""
    return f"{name}@{version}"


def _needs_apk_indexing(pkgs: list[Path]) -> bool:
    return any(p.suffix == ".apk" for p in pkgs)


async def run_build_cycle(config: dict[str, Any], state: StateManager,
                    sdk_mgrs: dict[str, SDKManager],
                    bot: OpenwrtBot | None = None, force_repo: str | None = None,
                    remote_builder: RemoteBuilder | None = None,
                    signing_key: str | None = None) -> None:
    """Run one build cycle across all (repo, openwrt_version) combinations.

    Feed arch directories touched by apk builds get a post-cycle reindex via
    the Hetzner server — we collect them here, then run the reindex step once
    after every repo is done so we pay the rsync/ssh roundtrip per arch, not
    per repo.
    """
    repos = config["repos"]
    if force_repo and force_repo != "all":
        repos = [r for r in repos if r["name"] == force_repo]
        if not repos:
            logger.error("Unknown repo: %s", force_repo)
            return

    apk_dirs_to_reindex: dict[str, tuple[str, str]] = {}

    for repo_config in repos:
        name = repo_config["name"]

        for version in repo_config["openwrt_versions"]:
            per_version_config = {**repo_config, "openwrt_version": version}
            key = _state_key(name, version)
            log_file = Path(LOG_DIR) / f"{key}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)

            try:
                pb = PackageBuilder(
                    repo_config=per_version_config,
                    repo_cache_dir=REPO_CACHE_DIR,
                    feed_dir=FEED_DIR,
                    sdk_managers=sdk_mgrs,
                    sdk_force=config.get("sdk_force", False),
                    remote_builder=remote_builder,
                    bot=bot,
                    signing_key=signing_key,
                )

                pb.clone_or_fetch()
                commit = pb.get_head_commit()

                if not state.has_changed(key, commit) and force_repo is None:
                    logger.info("No changes for %s (at %s), skipping",
                                key, commit[:7])
                    continue

                logger.info("Building %s (commit %s)", key, commit[:7])
                if bot:
                    await bot.notify_build_start(key, commit)
                results = await pb.build_all_targets()

                total_pkgs = sum(len(v) for v in results.values())
                logger.info("Built %d package(s) for %s", total_pkgs, key)

                for target, pkgs in results.items():
                    if _needs_apk_indexing(pkgs):
                        arch_dir = str(pb._get_arch_dir(target))
                        apk_dirs_to_reindex[arch_dir] = (version, target)

                prev_repo = state.get_repo(key)
                was_failed = (
                    prev_repo is not None
                    and prev_repo.get("status") == "failed"
                )

                state.record_success(key, commit)

                if was_failed and bot:
                    await bot.notify_recovery(key)
                if bot:
                    await bot.notify_build_success(key, commit, total_pkgs)

                log_file.write_text(
                    f"Build successful at {datetime.now(timezone.utc).isoformat()}\n"
                    f"Commit: {commit}\n"
                    f"Packages: {total_pkgs}\n"
                )

            except Exception as e:
                error_msg = str(e)
                logger.error("Build failed for %s: %s", key, error_msg)

                log_file.write_text(
                    f"Build FAILED at {datetime.now(timezone.utc).isoformat()}\n"
                    f"Error: {error_msg}\n"
                )

                commit = "unknown"
                try:
                    pb2 = PackageBuilder(
                        repo_config=per_version_config,
                        repo_cache_dir=REPO_CACHE_DIR,
                        feed_dir=FEED_DIR,
                        sdk_managers=sdk_mgrs,
                    )
                    commit = pb2.get_head_commit()
                except Exception:
                    pass

                prev = state.get_repo(key)
                already_notified = (
                    force_repo is None
                    and prev is not None
                    and prev.get("status") == "failed"
                    and prev.get("notified", False)
                )

                state.record_failure(key, commit, error_msg)

                if not already_notified and bot:
                    await bot.notify_failure(key, error_msg)

    # Reindex apk feeds once per touched arch dir. The Hetzner server is still
    # up at this point — the outer loop destroys it after this returns.
    if apk_dirs_to_reindex and remote_builder:
        for arch_dir, (version, target) in apk_dirs_to_reindex.items():
            try:
                remote_builder.reindex_apk_feed(arch_dir, version, target)
            except Exception as e:
                logger.error("apk reindex failed for %s: %s", arch_dir, e)

    if bot:
        bot.last_poll = datetime.now(timezone.utc).isoformat()


def rebuild_callback_factory(config: dict[str, Any], state: StateManager,
                             sdk_mgrs: dict[str, SDKManager],
                             bot: OpenwrtBot | None,
                             remote_builder: RemoteBuilder | None = None,
                             signing_key: str | None = None) -> Any:
    """Create a rebuild callback for the Telegram bot."""
    async def rebuild(target: str):
        await run_build_cycle(config, state, sdk_mgrs, bot=bot, force_repo=target,
                              remote_builder=remote_builder, signing_key=signing_key)
        # Destroy remote server after manual rebuild too
        if remote_builder and remote_builder._server_name:
            if bot:
                await bot.notify_server_destroy()
            try:
                remote_builder.destroy_server()
            except Exception as e:
                logger.warning("Failed to destroy build server: %s", e)
    return rebuild


async def main():
    logger.info("Starting OpenWrt Package Builder")

    config = load_config(CONFIG_PATH)
    state = StateManager(STATE_FILE)

    # Build an SDKManager per distinct openwrt version across all repos.
    versions_needed: set[str] = set(config["openwrt_versions"])
    for repo in config.get("repos", []):
        versions_needed.update(repo["openwrt_versions"])
    sdk_mgrs: dict[str, SDKManager] = {
        v: SDKManager(v, SDK_CACHE_DIR) for v in versions_needed
    }

    for target in config["default_targets"]:
        if target == "all":
            (Path(FEED_DIR) / "all").mkdir(parents=True, exist_ok=True)
        else:
            arch = TARGET_ARCH_MAP[target]
            (Path(FEED_DIR) / arch).mkdir(parents=True, exist_ok=True)
    (Path(FEED_DIR) / "all").mkdir(parents=True, exist_ok=True)

    # Generate signing keypair if it doesn't exist
    signing_key = None
    if not Path(SIGNING_KEY).exists():
        logger.info("Generating feed signing keypair")
        subprocess.run(
            ["usign", "-G", "-s", SIGNING_KEY, "-p", SIGNING_PUB,
             "-c", "openwrt-builder"],
            check=True,
        )
    if Path(SIGNING_KEY).exists():
        signing_key = SIGNING_KEY
        logger.info("Feed signing enabled")

    # Set up remote builder if Hetzner is configured
    remote_builder = None
    hetzner_config = config.get("hetzner", {})
    if hetzner_config and Path(HETZNER_TOKEN_PATH).exists():
        api_token = Path(HETZNER_TOKEN_PATH).read_text().strip()
        if api_token:
            remote_builder = RemoteBuilder(
                api_token=api_token,
                ssh_key_name=hetzner_config.get("ssh_key_name", "openwrt-builder"),
                server_type=hetzner_config.get("server_type", "cx22"),
                location=hetzner_config.get("location", "fsn1"),
            )
            logger.info("Hetzner remote builder configured (%s in %s)",
                         remote_builder.server_type, remote_builder.location)
        else:
            logger.warning("Hetzner token file is empty, using local builds")
    elif hetzner_config:
        logger.warning("Hetzner config present but no token file at %s",
                        HETZNER_TOKEN_PATH)

    # Only download SDKs locally if not using remote builder
    if not remote_builder:
        logger.info("Ensuring SDKs are downloaded...")

        version_targets: dict[str, set[str]] = {}
        for repo in config.get("repos", []):
            for v in repo["openwrt_versions"]:
                for t in repo.get("targets", []):
                    if t == "all":
                        continue
                    version_targets.setdefault(v, set()).add(t)

        if not version_targets:
            for default_v in config["openwrt_versions"]:
                version_targets[default_v] = {
                    t for t in config["default_targets"] if t != "all"
                }

        for version, targets in version_targets.items():
            mgr = sdk_mgrs[version]
            for target in targets:
                mgr.ensure_downloaded(target)
            if not targets and all(
                mgr.needs_download(t) for t in TARGET_ARCH_MAP
            ):
                first_target = next(iter(TARGET_ARCH_MAP))
                logger.info("Downloading %s SDK (owrt %s) for arch-independent builds",
                             first_target, version)
                mgr.ensure_downloaded(first_target)

    tg_config = config.get("telegram", {})
    bot = None
    if tg_config.get("bot_token") and tg_config.get("chat_id"):
        bot = OpenwrtBot(
            token=tg_config["bot_token"],
            chat_id=tg_config["chat_id"],
            state_manager=state,
            repos_config=config["repos"],
            rebuild_callback=rebuild_callback_factory(
                config, state, sdk_mgrs, None, remote_builder, signing_key),
            log_dir=LOG_DIR,
        )
        bot.rebuild_callback = rebuild_callback_factory(
            config, state, sdk_mgrs, bot, remote_builder, signing_key)

        app = bot.build_application()

        await app.initialize()
        await app.start()
        assert app.updater is not None
        await app.updater.start_polling()
        logger.info("Telegram bot started")

    poll_interval = config.get("poll_interval", 3600)
    logger.info("Poll interval: %d seconds", poll_interval)

    try:
        while True:
            logger.info("Starting build cycle")

            await run_build_cycle(config, state, sdk_mgrs, bot=bot,
                                  remote_builder=remote_builder,
                                  signing_key=signing_key)

            # Destroy remote server after each build cycle to save costs
            if remote_builder and remote_builder._server_name:
                if bot:
                    await bot.notify_server_destroy()
                try:
                    remote_builder.destroy_server()
                except Exception as e:
                    logger.warning("Failed to destroy build server: %s", e)

            logger.info("Build cycle complete, sleeping %d seconds", poll_interval)
            await asyncio.sleep(poll_interval)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down")
        # Clean up remote server on shutdown
        if remote_builder:
            try:
                remote_builder.destroy_server()
            except Exception:
                pass
        if bot and bot.app:
            if bot.app.updater:
                await bot.app.updater.stop()
            await bot.app.stop()
            await bot.app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
