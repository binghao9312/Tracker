"""Runtime configuration locations independent of package installation paths."""

from __future__ import annotations

import os
from pathlib import Path


def universe_path() -> Path:
    configured = os.environ.get("UNIVERSE_PATH")
    if configured:
        return Path(configured)
    for candidate in (
        Path.cwd() / "config" / "universe.json",
        Path(__file__).resolve().parents[2] / "config" / "universe.json",
        Path.cwd().parent / "config" / "universe.json",
    ):
        if candidate.is_file():
            return candidate
    return Path.cwd() / "config" / "universe.json"


def scoring_path() -> Path:
    configured = os.environ.get("SCORING_PATH")
    if configured:
        return Path(configured)
    for candidate in (
        Path.cwd() / "config" / "scoring.yaml",
        Path(__file__).resolve().parents[2] / "config" / "scoring.yaml",
        Path.cwd().parent / "config" / "scoring.yaml",
    ):
        if candidate.is_file():
            return candidate
    return Path.cwd() / "config" / "scoring.yaml"
