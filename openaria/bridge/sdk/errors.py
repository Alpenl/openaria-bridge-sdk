"""Public SDK exceptions."""

from __future__ import annotations

from collections.abc import Sequence


class OpenAriaError(RuntimeError):
    """Base class for errors callers can present without a traceback."""


class DiscoveryError(OpenAriaError):
    """No usable LAN device or recording card could be discovered."""


class ContractError(OpenAriaError):
    """A source claimed an unsupported or internally inconsistent contract."""


class ExportError(OpenAriaError):
    """A session could not be exported without losing integrity."""


class MultipleSourcesError(DiscoveryError):
    """More than one source was found and no selector chose one."""

    def __init__(self, locations: Sequence[str]) -> None:
        self.locations = tuple(locations)
        super().__init__(
            "multiple Open Aria sources found; choose one by device id, label, or location: "
            + ", ".join(self.locations)
        )
