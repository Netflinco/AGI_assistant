"""Private Office extraction and new-artifact generation domain."""

from .assets import OfficeAssetService, OfficePolicyError
from .jobs import OfficeJobService

__all__ = ["OfficeAssetService", "OfficeJobService", "OfficePolicyError"]
