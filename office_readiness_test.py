#!/usr/bin/env python3
"""F0 fail-closed readiness contracts for the Office deployment."""

from office_agent.readiness import production_readiness_errors


assert production_readiness_errors({}) == []
errors = production_readiness_errors({"AGI_OFFICE_PRODUCTION": "true"})
for required in (
    "OFFICE_F0_VIRUS_SCANNER_REQUIRED",
    "OFFICE_F0_WORKER_REQUIRED",
    "OFFICE_F0_EXTERNAL_WORKER_REQUIRED",
    "OFFICE_F0_OBJECT_STORAGE_REQUIRED",
    "OFFICE_F0_FONT_CHECK_REQUIRED",
    "OFFICE_F0_OBJECT_STORAGE_ADAPTER_NOT_IMPLEMENTED",
    "OFFICE_F0_VIRUS_SCANNER_ADAPTER_NOT_IMPLEMENTED",
    "OFFICE_F0_EXTERNAL_QUEUE_ADAPTER_NOT_IMPLEMENTED",
):
    assert required in errors

print("PASS office readiness tests: production Office remains fail-closed without F0 dependencies")
