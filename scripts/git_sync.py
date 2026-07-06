#!/usr/bin/env python3
"""Run git-sync with Pinglet's configured commit-message agent."""

from __future__ import annotations

from git_sync_common import run_sync


def main() -> int:
    return run_sync("git-sync", "config.yaml")


if __name__ == "__main__":
    raise SystemExit(main())
