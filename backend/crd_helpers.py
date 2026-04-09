"""Shared helpers for CRD/legacy instance boundary.

Used by main.py, sync_worker.py, and agent.py to avoid duplicating
CRD-aware filtering logic.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import database as db

logger = logging.getLogger("carher-admin")


def crd_uids(*, strict: bool = False) -> Optional[set[int]]:
    """Return the set of instance IDs managed by CRD (HerInstance).

    When *strict* is False (default), returns an empty set on failure —
    callers that only read/display data degrade gracefully.

    When *strict* is True, returns **None** on failure so callers that
    *write* state (sync_worker) can distinguish "no CRDs exist" from
    "CRD API is unreachable" and skip the current cycle.
    """
    try:
        from . import crd_ops
        return {
            inst.get("spec", {}).get("userId", 0)
            for inst in crd_ops.list_her_instances()
            if inst.get("spec", {}).get("userId", 0)
        }
    except Exception:
        if strict:
            logger.warning("CRD listing failed; returning None to signal unavailability")
            return None
        return set()


def db_instances_excluding_crds() -> list[dict]:
    """Return DB instances that are NOT managed by a CRD."""
    uids = crd_uids(strict=False)
    return [inst for inst in db.list_all() if inst.get("id") not in uids]
