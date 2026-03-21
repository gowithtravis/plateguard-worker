"""
Portal integrations for parking tickets and tolls.

**Plate-only monitoring (batch / check-plate defaults):** RMC Pay cities, Cambridge eTIMS.

**Manual ticket / invoice required:** ``kelley_ryan`` (Kelley & Ryan ePay MA towns),
``somerville_chs`` (Somerville via City Hall Systems), ``ezdrivema`` (EZDriveMA Pay By Plate
MA invoice + plate). These are rechecked after batch runs when stored violations exist; they
are never queried from plate alone.
"""

from .ezdrivema_tolls import EZDRIVEMA_PORTAL
from .kelley_ryan import KELLEY_RYAN_PORTAL
from .manual_ticket_portals import MANUAL_TICKET_PORTAL_LABELS
from .somerville_chs import SOMERVILLE_CHS_PORTAL

__all__ = (
    "EZDRIVEMA_PORTAL",
    "KELLEY_RYAN_PORTAL",
    "MANUAL_TICKET_PORTAL_LABELS",
    "SOMERVILLE_CHS_PORTAL",
)

