#!/usr/bin/env python3
"""Run git-sync for Hermes with Pinglet's configured commit-message agent."""

from __future__ import annotations

from git_sync_common import run_sync


def main() -> int:
    return run_sync("hermes-sync", "config-hermes.yaml")


if __name__ == "__main__":
    raise SystemExit(main())
