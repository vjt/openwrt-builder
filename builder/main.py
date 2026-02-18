from __future__ import annotations

import asyncio
import logging
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

            was_failed = (
                state.get_repo(name) is not None
                and state.get_repo(name).get("status") == "failed"
            )

            state.record_success(name, commit)

            if was_failed and bot:
                asyncio.get_event_loop().run_until_complete(
                    bot.notify_recovery(name)
                )

            log_file.write_text(
                f"Build successful at {datetime.now(timezone.utc).isoformat()}\n"
                f"Commit: {commit}\n"
                f"Packages: {total_ipks}\n"
            )

        except Exception as e:
            error_msg = str(e)
            logger.error("Build failed for %s: %s", name, error_msg)

            log_file.write_text(
                f"Build FAILED at {datetime.now(timezone.utc).isoformat()}\n"
                f"Error: {error_msg}\n"
            )

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

    for target in config["default_targets"]:
        if target == "all":
            (Path(FEED_DIR) / "all").mkdir(parents=True, exist_ok=True)
        else:
            arch = TARGET_ARCH_MAP[target]
            (Path(FEED_DIR) / arch).mkdir(parents=True, exist_ok=True)
    (Path(FEED_DIR) / "all").mkdir(parents=True, exist_ok=True)

    logger.info("Ensuring SDKs are downloaded...")
    unique_targets = [
        t for t in config["default_targets"] if t != "all"
    ]
    for target in unique_targets:
        sdk_mgr.ensure_downloaded(target)

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
        bot.rebuild_callback = rebuild_callback_factory(config, state, sdk_mgr, bot)

        app = bot.build_application()

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Telegram bot started")

    poll_interval = config.get("poll_interval", 3600)
    logger.info("Poll interval: %d seconds", poll_interval)

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
