"""Integrated Open Aria recording discovery and verified export SDK.

The ordinary entry points are intentionally small::

    from openaria.bridge.sdk import OpenAriaSDK

    OpenAriaSDK(mode="lan").export()
    OpenAriaSDK(mode="card").export()
"""

from .client import OpenAriaSDK
from .errors import (
    ContractError,
    DiscoveryError,
    ExportError,
    MultipleSourcesError,
    OpenAriaError,
)
from .models import ExportedSession, ExportResult, SessionInfo, Source, SourceMode

__all__ = [
    "ContractError",
    "DiscoveryError",
    "ExportError",
    "ExportResult",
    "ExportedSession",
    "MultipleSourcesError",
    "OpenAriaError",
    "OpenAriaSDK",
    "SessionInfo",
    "Source",
    "SourceMode",
]
