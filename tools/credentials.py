"""A secret by name: from the environment (the Mac, where `wo ingest` loads `.env`), else from the
service's systemd credential directory (the VPS, where `wo-scan.service` imports encrypted credentials
so they never enter any process environment). Values are returned, never printed or logged."""
from __future__ import annotations

import os
from pathlib import Path


def get(name: str, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    value = (env.get(name) or "").strip()
    if value:
        return value
    folder = env.get("CREDENTIALS_DIRECTORY")
    if folder:
        try:
            return (Path(folder) / name).read_text().strip()
        except OSError:
            pass
    return ""
