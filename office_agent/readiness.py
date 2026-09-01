"""F0 deployment readiness checks; development mode stays explicitly local."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Mapping


def production_readiness_errors(env: Mapping[str, str] | None = None) -> list[str]:
    values = os.environ if env is None else env
    enabled = str(values.get("AGI_OFFICE_PRODUCTION") or "").lower() in {"1", "true", "yes"}
    if not enabled:
        return []
    errors: list[str] = []
    if str(values.get("AGI_OFFICE_REQUIRE_VIRUS_SCANNER") or "").lower() not in {"1", "true", "yes"}:
        errors.append("OFFICE_F0_VIRUS_SCANNER_REQUIRED")
    if str(values.get("AGI_OFFICE_WORKER_ENABLED") or "").lower() not in {"1", "true", "yes"}:
        errors.append("OFFICE_F0_WORKER_REQUIRED")
    if str(values.get("AGI_OFFICE_WORKER_MODE") or "").lower() != "external":
        errors.append("OFFICE_F0_EXTERNAL_WORKER_REQUIRED")
    if str(values.get("AGI_OFFICE_OBJECT_STORAGE_BACKEND") or "").lower() in {"", "local", "filesystem"}:
        errors.append("OFFICE_F0_OBJECT_STORAGE_REQUIRED")
    # This repository currently ships only the local-filesystem, no-op scanner
    # and DB-polling development adapters.  Configuration must never turn
    # those into a misleading production green light; real adapters are an F0
    # deployment deliverable and must replace these guards with contract tests.
    errors.extend([
        "OFFICE_F0_OBJECT_STORAGE_ADAPTER_NOT_IMPLEMENTED",
        "OFFICE_F0_VIRUS_SCANNER_ADAPTER_NOT_IMPLEMENTED",
        "OFFICE_F0_EXTERNAL_QUEUE_ADAPTER_NOT_IMPLEMENTED",
    ])
    # An environment boolean is not a font self-check.  Deployment must pass
    # the installed Alibaba PuHuiTi font file mounted in the immutable image.
    font_path = str(values.get("AGI_OFFICE_FONT_PATH") or "").strip()
    if not font_path or not os.path.isfile(font_path):
        errors.append("OFFICE_F0_FONT_CHECK_REQUIRED")
    elif "alibaba" not in os.path.basename(font_path).lower() and "puhui" not in os.path.basename(font_path).lower():
        errors.append("OFFICE_F0_FONT_FAMILY_MISMATCH")
    soffice = shutil.which("soffice")
    if not soffice:
        errors.append("OFFICE_F0_LIBREOFFICE_MISSING")
    else:
        try:
            version = subprocess.check_output([soffice, "--version"], text=True, stderr=subprocess.STDOUT, timeout=5).strip().lower()
        except (OSError, subprocess.SubprocessError):
            errors.append("OFFICE_F0_LIBREOFFICE_VERSION_UNKNOWN")
        else:
            if any(marker in version for marker in ("dev", "alpha", "beta", "rc")):
                errors.append("OFFICE_F0_LIBREOFFICE_UNSTABLE")
    return errors
