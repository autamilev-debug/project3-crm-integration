"""External integration adapters."""

from project3_crm.integrations.hubspot import (
    CreateContactResult,
    CreateResultCategory,
    HubSpotAdapter,
    HubSpotContactInput,
    HubSpotDiagnostics,
    ReadContactResult,
    ReadResultCategory,
)

__all__ = [
    "CreateContactResult",
    "CreateResultCategory",
    "HubSpotAdapter",
    "HubSpotContactInput",
    "HubSpotDiagnostics",
    "ReadContactResult",
    "ReadResultCategory",
]
