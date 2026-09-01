"""Immutable private Office asset storage with pre-promotion validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import uuid
from typing import Any, Callable

from agent_governance.audit import summary_hash

from .policy import AssetInspection, OfficePolicyError, inspect_asset, validate_batch_metadata


ASSET_RETENTION_DAYS = 30


class VirusScanner:
    def scan(self, filename: str, content: bytes) -> str:  # pragma: no cover - protocol behaviour
        return "CLEAN"


class OfficeAssetService:
    def __init__(self, conn: Any, storage_root: str | Path, *, scanner: VirusScanner | None = None,
                 now: Callable[[], datetime] | None = None, require_scanner: bool = False):
        self.conn = conn
        self.root = Path(storage_root)
        self.scanner = scanner
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.require_scanner = require_scanner

    def create(self, *, tenant_id: str, user_id: str, filename: str, content: bytes) -> dict:
        inspection = inspect_asset(filename, content)
        self._scan(filename, content)
        return self._promote(tenant_id, user_id, filename, content, inspection)

    def create_batch(self, *, tenant_id: str, user_id: str, files: list[tuple[str, bytes | str | Path]]) -> list[dict]:
        """Validate a batch before first promotion without aggregating it in RAM.

        HTTP uploads may arrive as private staging paths.  Each staged object
        is read and inspected independently; no request ever creates a
        120 MB in-memory ``files`` aggregate, and every member is validated
        before the first visible asset is promoted.
        """
        materialized, declared_sizes = validate_batch_metadata(files)
        inspections: list[AssetInspection] = []
        for (filename, source), declared_size in zip(materialized, declared_sizes):
            content = self._content_bytes(source)
            inspection = inspect_asset(filename, content, declared_size=declared_size)
            self._scan(filename, content)
            inspections.append(inspection)
        # Policy validation happens before the first promotion.  A later local
        # storage/DB fault must still not leave the visible half of a batch.
        created: list[dict] = []
        try:
            for (filename, source), inspection in zip(materialized, inspections):
                content = self._content_bytes(source)
                # Staging files are private and short-lived, but bind the
                # second read to the inspected version before promotion.
                if inspect_asset(filename, content, declared_size=inspection.byte_size).sha256 != inspection.sha256:
                    raise OfficePolicyError("OFFICE_ASSET_CHANGED_DURING_UPLOAD")
                created.append(self._promote(tenant_id, user_id, filename, content, inspection))
            return created
        except Exception:
            for asset in created:
                row = self.conn.execute("SELECT storage_key FROM office_assets WHERE asset_id=?", (asset["asset_id"],)).fetchone()
                if row:
                    key = row["storage_key"] if hasattr(row, "keys") else row[0]
                    (self.root / key).unlink(missing_ok=True)
                self.conn.execute("DELETE FROM office_assets WHERE asset_id=?", (asset["asset_id"],))
            raise

    @staticmethod
    def _content_bytes(source: bytes | str | Path) -> bytes:
        if isinstance(source, (str, Path)):
            return Path(source).read_bytes()
        return source

    def get(self, *, asset_id: str, tenant_id: str, user_id: str) -> dict | None:
        row = self.conn.execute(
            """SELECT * FROM office_assets WHERE asset_id=? AND tenant_id=? AND user_id=?
               AND status='ACTIVE' AND expires_at>?""",
            (asset_id, tenant_id, user_id, self.now().isoformat(timespec="seconds")),
        ).fetchone()
        return dict(row) if row else None

    def read(self, *, asset_id: str, tenant_id: str, user_id: str) -> tuple[dict, bytes] | None:
        asset = self.get(asset_id=asset_id, tenant_id=tenant_id, user_id=user_id)
        if not asset:
            return None
        path = self.root / asset["storage_key"]
        if not path.is_file():
            return None
        return asset, path.read_bytes()

    def delete(self, *, asset_id: str, tenant_id: str, user_id: str) -> bool:
        asset = self.get(asset_id=asset_id, tenant_id=tenant_id, user_id=user_id)
        if not asset:
            return False
        (self.root / asset["storage_key"]).unlink(missing_ok=True)
        timestamp = self.now().isoformat(timespec="seconds")
        self.conn.execute(
            """UPDATE office_assets SET status='DELETED', deleted_at=?
               WHERE asset_id=? AND tenant_id=? AND user_id=? AND status='ACTIVE'""",
            (timestamp, asset_id, tenant_id, user_id),
        )
        self.conn.execute(
            "DELETE FROM office_extractions WHERE asset_id=? AND tenant_id=? AND user_id=?",
            (asset_id, tenant_id, user_id),
        )
        return True

    @staticmethod
    def public_asset(asset: dict) -> dict:
        return {
            key: asset[key]
            for key in ("asset_id", "filename", "extension", "detected_mime", "byte_size", "sha256", "scan_status", "status", "created_at", "expires_at")
            if key in asset
        }

    def cleanup_expired(self) -> int:
        now = self.now().isoformat(timespec="seconds")
        rows = self.conn.execute("SELECT * FROM office_assets WHERE status='ACTIVE' AND expires_at<=?", (now,)).fetchall()
        for row in rows:
            path = self.root / row["storage_key"]
            path.unlink(missing_ok=True)
            self.conn.execute("UPDATE office_assets SET status='EXPIRED', deleted_at=? WHERE asset_id=?", (now, row["asset_id"]))
        return len(rows)

    def _scan(self, filename: str, content: bytes) -> None:
        if not self.scanner:
            if self.require_scanner:
                raise OfficePolicyError("OFFICE_VIRUS_SCAN_UNAVAILABLE")
            return
        result = str(self.scanner.scan(filename, content)).upper()
        if result == "UNAVAILABLE":
            raise OfficePolicyError("OFFICE_VIRUS_SCAN_UNAVAILABLE")
        if result != "CLEAN":
            raise OfficePolicyError("OFFICE_VIRUS_SCAN_REJECTED")

    def _promote(self, tenant_id: str, user_id: str, filename: str, content: bytes, inspection: AssetInspection) -> dict:
        now = self.now()
        asset_id = f"asset_{uuid.uuid4().hex[:16]}"
        suffix = inspection.extension
        storage_key = str(Path(tenant_id) / user_id / f"{asset_id}{suffix}")
        path = self.root / storage_key
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Atomic local promotion: never expose a partial asset name.
        staging = path.with_suffix(path.suffix + ".staging")
        staging.write_bytes(content)
        os.chmod(staging, 0o600)
        os.replace(staging, path)
        expires = now + timedelta(days=ASSET_RETENTION_DAYS)
        self.conn.execute(
            """INSERT INTO office_assets(asset_id, tenant_id, user_id, filename, extension, detected_mime, byte_size, sha256,
                storage_key, scan_status, status, created_at, expires_at, deleted_at)
               VALUES(?,?,?,?,?,?,?,?,?,'CLEAN','ACTIVE',?,?,NULL)""",
            (asset_id, tenant_id, user_id, filename[:180], inspection.extension, inspection.detected_mime, inspection.byte_size,
             inspection.sha256, storage_key, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
        return {"asset_id": asset_id, "filename": filename, "detected_mime": inspection.detected_mime,
                "byte_size": inspection.byte_size, "sha256": inspection.sha256, "expires_at": expires.isoformat(timespec="seconds")}
