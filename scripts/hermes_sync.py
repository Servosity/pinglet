#!/usr/bin/env python3
"""Run git-sync for Hermes with the existing fallback commit message."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


GIT_SYNC_DIR = Path(os.environ.get("GIT_SYNC_DIR", "../git-sync")).expanduser()
FALLBACK_COMMIT_MESSAGE = "Update Obsidian vault"


def _load_sync_module():
    sync_path = GIT_SYNC_DIR / "sync.py"
    sys.path.insert(0, str(GIT_SYNC_DIR))
    spec = importlib.util.spec_from_file_location("git_sync_sync", sync_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {sync_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    sync_module = _load_sync_module()

    class VaultSyncWithFallback(sync_module.VaultSync):
        def generate_commit_message(self, diff_summary: str):
            return super().generate_commit_message(diff_summary) or FALLBACK_COMMIT_MESSAGE

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config-hermes.yaml"
    syncer = VaultSyncWithFallback(config_path)
    success, _message = syncer.sync()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
