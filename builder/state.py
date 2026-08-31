from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


# How many times the same commit is rebuilt after a failure before we stop.
# Covers transient infrastructure failures (SSH timeout, mirror hiccup)
# without spending a Hetzner server every poll interval on a deterministic
# compile error that no amount of retrying will fix.
MAX_BUILD_ATTEMPTS = 3


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
        """Return True if the repo should be built this cycle.

        Triggers on: never-built, new commit, or a failed build whose
        retry budget isn't spent yet — a transient failure (SSH timeout,
        network blip) would otherwise stay unresolved until the upstream
        commit happens to change.
        """
        repo = self.get_repo(name)
        if repo is None:
            return True
        if repo.get("last_commit") != commit:
            return True
        if repo.get("status") == "failed":
            return not self._budget_spent(repo)
        return False

    @staticmethod
    def _budget_spent(repo: dict) -> bool:
        # Entries written before `failures` existed count as one attempt.
        return repo.get("failures", 1) >= MAX_BUILD_ATTEMPTS

    def retries_exhausted(self, name: str) -> bool:
        """True when a repo keeps failing on the same commit and we've
        stopped retrying it — the caller can say so instead of logging a
        misleading "no changes"."""
        repo = self.get_repo(name)
        if repo is None or repo.get("status") != "failed":
            return False
        return self._budget_spent(repo)

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
        prev = self.data.get(name, {})
        already_notified = (
            prev.get("status") == "failed"
            and prev.get("notified", False)
        )
        # The retry budget is per-commit: a new commit is a new build, so it
        # gets a fresh one even if the previous one burned through its own.
        failures = (
            prev.get("failures", 0) + 1
            if prev.get("last_commit") == commit
            else 1
        )
        self.data[name] = {
            "last_commit": commit,
            "last_build": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "error": error,
            "failures": failures,
            "notified": already_notified,  # preserve if already notified
        }
        if not already_notified:
            self.data[name]["notified"] = True
            self._save()
            return  # caller can check notified flag
        self._save()

    def record_poll_failure(self, name: str, error: str) -> bool:
        """Record that clone/fetch failed, leaving the *build* state alone.

        A poll failure means no build was attempted, so the repo's commit
        and build status are still the last thing we actually know to be
        true. Writing a build failure here would make the next cycle treat
        every unreachable repo as needing a rebuild.

        Returns True when the failure is worth notifying about (first one,
        or a different error than last time).
        """
        repo = self.data.setdefault(name, {})
        is_new = repo.get("poll_error") != error
        repo["poll_error"] = error
        repo["last_poll_failure"] = datetime.now(timezone.utc).isoformat()
        self._save()
        return is_new

    def clear_poll_failure(self, name: str):
        """Drop a recorded poll failure once the repo is reachable again."""
        repo = self.data.get(name)
        if repo is None or "poll_error" not in repo:
            return
        repo.pop("poll_error")
        repo.pop("last_poll_failure", None)
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
