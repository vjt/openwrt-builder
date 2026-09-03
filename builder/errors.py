from __future__ import annotations


class PackageBuildError(RuntimeError):
    """The build ran and returned a verdict on this commit's sources.

    A compile error is a property of the commit, not of the weather: the
    same commit will fail the same way on the next cycle, so the caller must
    not retry it — each retry rents a fresh build server to reproduce an
    identical failure.

    Everything else (server creation, SSH, rsync, SDK download) stays a
    plain RuntimeError and is treated as possibly transient, hence worth a
    bounded number of retries.
    """
