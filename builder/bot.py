from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from state import StateManager

logger = logging.getLogger(__name__)


def format_status_message(state_data: dict[str, Any], last_poll: str | None) -> str:
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


def format_list_message(repos: list[dict[str, Any]], state_data: dict[str, Any]) -> str:
    """Format the /list response.

    State keys are `name@openwrt_version` (see _state_key in main.py) so a
    24.10 success doesn't mask a 25.12 failure. Lookup must match.
    """
    lines = []
    for repo in repos:
        name = repo["name"]
        branch = repo["branch"]
        versions = repo.get("openwrt_versions") or [None]
        for version in versions:
            key = f"{name}@{version}" if version else name
            state = state_data.get(key, {})
            commit = state.get("last_commit", "not built")[:7] if state else "not built"
            label = f"{name}@{version}" if version else name
            lines.append(f"  {label} ({branch}) @ {commit}")
    return "\n".join(lines) if lines else "No repos configured."


class OpenwrtBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        state_manager: StateManager,
        repos_config: list[dict[str, Any]],
        rebuild_callback: Callable[[str], Awaitable[None]],
        log_dir: str = "/tmp/build-logs",
    ):
        self.token = token
        self.chat_id = int(chat_id)
        self.state = state_manager
        self.repos_config = repos_config
        self.rebuild_callback = rebuild_callback
        self.log_dir = Path(log_dir)
        self.last_poll: str | None = None
        self.app: Application | None = None  # type: ignore[type-arg]

    def _check_authorized(self, update: Update) -> bool:
        """Only respond to the configured chat_id."""
        chat = update.effective_chat
        return chat is not None and chat.id == self.chat_id

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update) or not update.message:
            return
        msg = format_status_message(self.state.data, self.last_poll)
        await update.message.reply_text(msg)

    async def cmd_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update) or not update.message:
            return
        msg = format_list_message(self.repos_config, self.state.data)
        await update.message.reply_text(msg)

    async def cmd_rebuild(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update) or not update.message:
            return
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /rebuild <repo-name> or /rebuild all"
            )
            return

        target = args[0]
        await update.message.reply_text(f"Rebuild triggered for: {target}")
        await self.rebuild_callback(target)

    async def cmd_logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update) or not update.message:
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
        # Telegram message hard limit is 4096 chars. 500 used to swallow
        # everything past "Installing package …" — keep the most-recent ~3800
        # chars so real make output survives. Plain text (no markdown).
        body = error[-3800:]
        await self.notify(f"BUILD FAILED: {repo_name}\n\n{body}")

    async def notify_recovery(self, repo_name: str):
        await self.notify(f"RECOVERED: {repo_name} is building successfully again.")

    async def notify_build_start(self, repo_name: str, commit: str):
        await self.notify(f"Building {repo_name} @ {commit[:7]}")

    async def notify_build_success(self, repo_name: str, commit: str, num_ipks: int):
        await self.notify(f"OK: {repo_name} @ {commit[:7]} — {num_ipks} package(s) built")

    async def notify_clone(self, repo_name: str):
        await self.notify(f"Cloning {repo_name}...")

    async def notify_skip(self, repo_name: str, commit: str):
        await self.notify(f"No changes for {repo_name} (at {commit[:7]}), skipping")

    async def notify_cycle_start(self):
        await self.notify("Starting build cycle")

    async def notify_cycle_done(self, poll_interval: int):
        await self.notify(f"Build cycle complete, sleeping {poll_interval}s")

    async def notify_server_create(self, server_type: str, location: str):
        await self.notify(f"Creating build server ({server_type} in {location})...")

    async def notify_server_ready(self, ip: str):
        await self.notify(f"Build server ready at {ip}")

    async def notify_server_destroy(self):
        await self.notify("Destroying build server")

    def build_application(self) -> Application:
        """Build and return the telegram Application (for running)."""
        self.app = (
            Application.builder()
            .token(self.token)
            .build()
        )
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("list", self.cmd_list))
        self.app.add_handler(CommandHandler("rebuild", self.cmd_rebuild))
        self.app.add_handler(CommandHandler("logs", self.cmd_logs))
        return self.app

    async def register_commands(self) -> None:
        """Register bot commands for Telegram autocompletion. Called explicitly
        from main.py after the bot is up. Application.builder().post_init() is
        unreliable in our v20 invocation order (initialize / start /
        updater.start_polling) — the hook never fires and the autocomplete
        dropdown ends up empty."""
        if not self.app:
            logger.warning("register_commands called before build_application")
            return
        logger.info("Registering Telegram bot commands")
        try:
            await self.app.bot.set_my_commands([
                BotCommand("status", "Show build status for all repos"),
                BotCommand("list",   "List configured repos and their last commits"),
                BotCommand("rebuild","Rebuild a repo: /rebuild <name|all>"),
                BotCommand("logs",   "Show last build log: /logs <name>"),
            ])
            logger.info("Telegram bot commands registered")
        except Exception:
            logger.exception("Failed to register Telegram bot commands")
