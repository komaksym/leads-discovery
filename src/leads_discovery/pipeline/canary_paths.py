"""Path validation shared by canary restart and report persistence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def canary_run_dir(
    data_root: Path,
    run_id: str,
    *,
    require_existing: bool = False,
) -> Path:
    """Resolve one validated run directory without following an untrusted final symlink."""
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    expanded = data_root.expanduser()
    if expanded.is_symlink():
        raise ValueError("data_root must not be a symlink")
    root = expanded.resolve()
    if require_existing and not root.is_dir():
        raise ValueError("canary data_root must exist")
    if not require_existing:
        root.mkdir(parents=True, exist_ok=True)
    candidate = root / run_id
    if candidate.is_symlink():
        raise ValueError("canary run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root:
        raise ValueError("canary run directory must remain directly beneath data_root")
    if require_existing and not run_dir.is_dir():
        raise ValueError("canary run directory must exist")
    if not require_existing:
        run_dir.mkdir(exist_ok=True)
    return run_dir


__all__ = ["canary_run_dir"]
