"""Single-variant POV output: overlay-only, or raw-only for debug.

There is no dual-upload cube. One youtube dir per run.
"""
from __future__ import annotations


def resolve_skip_overlay(*, raw_only: bool, state: dict | None) -> bool:
    """True when this run should skip overlay and write youtube/{run_id}/.

    Default is overlay (False). ``--raw-only`` always wins. Resume reads
    sticky ``skip_overlay`` from state, with legacy dual_upload/overlay_only
    keys still recognized so old .pipeline files resume on the same variant.
    """
    if raw_only:
        return True
    data = (state or {}).get("data") or {}
    if "skip_overlay" in data:
        return bool(data["skip_overlay"])
    if data.get("overlay_only"):
        return False
    if "dual_upload" in data and not data.get("dual_upload"):
        return True
    return False


def youtube_dir_name(run_id: str, *, skip_overlay: bool) -> str:
    return run_id if skip_overlay else f"{run_id}_overlay"
