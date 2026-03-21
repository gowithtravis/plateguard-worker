"""
Portals that require a known ticket number — never used for plate-only batch checks.

Manual reports and scheduled rechecks use these labels; see ``MonitorService``.
"""

from __future__ import annotations

from typing import FrozenSet

MANUAL_TICKET_PORTAL_LABELS: FrozenSet[str] = frozenset({"kelley_ryan", "somerville_chs"})
