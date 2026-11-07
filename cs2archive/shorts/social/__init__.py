"""Cross-platform CS2 Shorts evidence sweep (collection, reporting, experiments).

Public/authorized evidence only. No browser automation, no login bypass. Every
platform adapter declares an explicit status (``supported`` / ``partial`` /
``blocked`` / ``unavailable``) so missing coverage can never read as real coverage.

Entry point:
    python -m cs2archive.shorts.social_sweep collect --config sources.json
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION"]

SCHEMA_VERSION = 1
