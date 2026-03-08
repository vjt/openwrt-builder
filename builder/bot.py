from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from telegram import BotCommand, Update
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
        await self.rebuild_callback(target)

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

    async def notify_server_destroy(self):
        await self.notify("Destroying build server")

    def build_application(self) -> Application:
        """Build and return the telegram Application (for running)."""
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("list", self.cmd_list))
        self.app.add_handler(CommandHandler("rebuild", self.cmd_rebuild))
        self.app.add_handler(CommandHandler("logs", self.cmd_logs))
        self.app.post_init = self._post_init
        return self.app

    async def _post_init(self, application: Application) -> None:
        """Register bot commands for Telegram autocompletion."""
        await application.bot.set_my_commands([
            BotCommand("status", "Show build status for all repos"),
            BotCommand("list", "List configured repos"),
            BotCommand("rebuild", "Rebuild a repo or all repos"),
            BotCommand("logs", "Show build log for a repo"),
        ])
