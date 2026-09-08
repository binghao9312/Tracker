"""Runtime configuration locations independent of package installation paths."""

from __future__ import annotations

import os
from pathlib import Path


def universe_path() -> Path:
    configured = os.environ.get("UNIVERSE_PATH")
    if configured:
        return Path(configured)
    return Path.cwd() / "config" / "universe.json"
