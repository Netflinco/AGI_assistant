"""LibreOffice render adapter.  No preview is delivered when rendering fails."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

from .policy import OfficePolicyError


def render_pptx(pptx_path: str | Path, output_dir: str | Path, *, soffice_path: str | None = None, timeout_seconds: int = 45) -> Path:
    source = Path(pptx_path)
    out = Path(output_dir)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    soffice = soffice_path or shutil.which("soffice")
    if not soffice:
        raise OfficePolicyError("OFFICE_RENDER_RUNTIME_UNAVAILABLE")
    profile_dir = Path(tempfile.mkdtemp(prefix="lo-profile-", dir=out))
    try:
        subprocess.run([soffice, f"-env:UserInstallation={profile_dir.as_uri()}", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_seconds)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise OfficePolicyError("OFFICE_RENDER_TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "OFFICE_RENDER_FAILED") from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    pdf = out / f"{source.stem}.pdf"
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise OfficePolicyError("OFFICE_RENDER_FAILED")
    return pdf
