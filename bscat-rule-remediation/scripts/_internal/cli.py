#!/usr/bin/env python3
"""Shared helpers for the top-level command scripts.

Only the small, behaviour-neutral pieces live here.  The stage scripts keep
their own argument parsing and subprocess wrappers; this module exists so the
"review directory must stay outside the target repository" gate has a single
implementation.
"""

from __future__ import annotations

from pathlib import Path


def ensure_outside_repo(
    repo: Path,
    target: Path,
    label: str = "审核目录",
) -> None:
    """Refuse a review/backup/history directory inside the target repo."""
    try:
        target.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"{label}不能位于目标仓库内: {target}\n"
        "请改到仓库之外的目录，避免产物被当成规则修改。"
    )
