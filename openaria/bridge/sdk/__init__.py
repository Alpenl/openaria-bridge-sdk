"""Integrated Open Aria recording discovery and verified export SDK.

The ordinary entry points are intentionally small::

    from openaria.bridge.sdk import OpenAriaSDK

    OpenAriaSDK(mode="lan").export()
    OpenAriaSDK(mode="card").export()
"""

from .client import OpenAriaSDK
from .errors import (
    ContractError,
    DeleteError,
    DiscoveryError,
    ExportError,
    MultipleSourcesError,
    OpenAriaError,
)
from .models import (
    DeleteFailure,
    DeleteResult,
    ExportedSession,
    ExportFailure,
    ExportResult,
    SessionInfo,
    Source,
    SourceMode,
)
from .options import ExportOptions

__all__ = [
    "ContractError",
    "DeleteError",
    "DeleteFailure",
    "DeleteResult",
    "DiscoveryError",
    "ExportError",
    "ExportFailure",
    "ExportOptions",
    "ExportResult",
    "ExportedSession",
    "MultipleSourcesError",
    "OpenAriaError",
    "OpenAriaSDK",
    "SessionInfo",
    "Source",
    "SourceMode",
]
