"""P0 Office Job state machine and local worker adapter.

The service only accepts already-authorised asset IDs or a validated
``ResearchBrief``.  It deliberately owns no search client and contains no
outbound HTTP code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Any, Callable, Protocol

from agent_governance.audit import summary_hash
from agent_governance.contracts import GateContext, GateDecision, GateError
from agent_governance.gate_engine import GateEngine
from agent_governance.policy_registry import P0_BLOCKED_OFFICE_ACTIONS, feature_enabled
from agent_governance.workflow_store import update_workflow

from .assets import OfficeAssetService
from .extraction import extract, minimal_fragments
from .generators import generate_pptx
from .policy import OfficePolicyError, research_brief_decision
from .rendering import render_pptx
from .specs import deterministic_spec, validate_spec
from .validation import validate_pptx_structure, validate_rendered_pages, validate_rendered_preview


class ModelGateway(Protocol):
    def create_spec(self, *, title: str, fragments: dict[str, Any], brief: dict | None) -> dict[str, Any]: ...


class DeterministicModelGateway:
    """A safe local fallback rather than a silent external model dependency."""
    def create_spec(self, *, title: str, fragments: dict[str, Any], brief: dict | None) -> dict[str, Any]:
        return deterministic_spec(title, fragments, brief)


class OfficeJobService:
    RETENTION_DAYS = 30

    def __init__(self, conn: Any, asset_service: OfficeAssetService, artifact_root: str | Path, *,
                 model_gateway: ModelGateway | None = None,
                 renderer: Callable[..., Path] = render_pptx,
                 generator: Callable[..., Path] = generate_pptx,
                 now: Callable[[], datetime] | None = None,
                 audit_logger: Callable[..., Any] | None = None):
        self.conn = conn
        self.assets = asset_service
        self.artifact_root = Path(artifact_root)
        self.model_gateway = model_gateway or DeterministicModelGateway()
        self.renderer = renderer
        self.generator = generator
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.audit_logger = audit_logger

    def create_ppt_job(self, *, tenant_id: str, user_id: str, conversation_id: str | None,
                       asset_ids: list[str] | None = None, brief_id: str | None = None,
                       title: str = "管理层汇报", template_id: str = "template_default",
                       workflow_id: str | None = None, mode_override: str = "AUTO",
                       auto_run: bool = False) -> dict[str, Any]:
        asset_ids = list(dict.fromkeys(asset_ids or []))
        now = self.now()
        # Calculate a possible idempotency hit before the queue gate.  A repeat
        # request must return its original queued job even when that job itself
        # occupies the user's one heavy-task slot.
        preliminary_assets = [self.assets.get(asset_id=item, tenant_id=tenant_id, user_id=user_id) for item in asset_ids]
        preliminary_brief = self._get_brief(brief_id, tenant_id, user_id) if brief_id else None
        preliminary_key = None
        existing = None
        if (not asset_ids or all(item is not None for item in preliminary_assets)) and (not brief_id or preliminary_brief is not None):
            preliminary_key = summary_hash({"tenant": tenant_id, "user": user_id, "assets": [item["sha256"] for item in preliminary_assets if item],
                                            "brief": preliminary_brief.get("content_hash") if preliminary_brief else None, "title": title, "template": template_id})
            existing = self.conn.execute(
                """SELECT * FROM office_jobs WHERE tenant_id=? AND user_id=? AND idempotency_key=?
                   ORDER BY created_at DESC LIMIT 1""",
                (tenant_id, user_id, preliminary_key),
            ).fetchone()
        context = GateContext(
            request_id=f"req_{uuid.uuid4().hex[:16]}", tenant_id=tenant_id, user_id=user_id,
            conversation_id=conversation_id, requested_domain="OFFICE", action="CREATE_PPT",
            workflow_id=workflow_id, mode_lock=mode_override.upper(), attachment_ids=tuple(asset_ids),
            input_summary_hash=summary_hash({"asset_ids": asset_ids, "brief_id": brief_id, "title": title, "template": template_id}),
            data_classification="INTERNAL",
        )
        engine = GateEngine(self.conn, now=now.isoformat(timespec="seconds"), audit_logger=self.audit_logger)
        try:
            engine.evaluate(context, [
                ("G0", lambda: GateDecision("G0", "ALLOW" if feature_enabled(self.conn, tenant_id, "office_enabled") else "BLOCK", "FEATURE_ENABLED" if feature_enabled(self.conn, tenant_id, "office_enabled") else "FEATURE_DISABLED")),
                ("G1", lambda: GateDecision("G1", "BLOCK" if mode_override.upper() == "INSPECTION" else "ALLOW", "OFFICE_DISABLED_IN_INSPECTION_MODE" if mode_override.upper() == "INSPECTION" else "OFFICE_ROUTE_CONFIRMED")),
                ("G2", lambda: self._asset_gate(tenant_id, user_id, asset_ids, brief_id)),
                ("G3", lambda: GateDecision("G3", "ALLOW" if template_id == "template_default" else "BLOCK", "OFFICE_TEMPLATE_ALLOWED" if template_id == "template_default" else "OFFICE_TEMPLATE_NOT_FOUND")),
                ("G4", lambda: GateDecision("G4", "ALLOW", "TRANSIENT_SESSION_AUTO_EXECUTION")),
                ("G5", lambda: GateDecision("G5", "ALLOW" if existing or self._can_schedule(tenant_id, user_id) else "DEGRADE", "OFFICE_IDEMPOTENCY_HIT" if existing else "OFFICE_QUEUE_AVAILABLE" if self._can_schedule(tenant_id, user_id) else "OFFICE_USER_CONCURRENCY_LIMIT")),
            ])
        except GateError as exc:
            return {"status": "BLOCKED", "reason_code": exc.decision.reason_code, "gate": exc.decision.public_dict()}
        if not asset_ids and not brief_id:
            return self._block(engine, context, "G3", "OFFICE_INPUT_REQUIRED")
        if self._blocked_action(title):
            return self._block(engine, context, "G4", "OFFICE_ACTION_NOT_ALLOWED")
        assets = [self.assets.get(asset_id=item, tenant_id=tenant_id, user_id=user_id) for item in asset_ids]
        if any(item is None for item in assets):
            return self._block(engine, context, "G2", "OFFICE_ASSET_NOT_FOUND")
        brief = self._get_brief(brief_id, tenant_id, user_id) if brief_id else None
        if brief_id and brief is None:
            return self._block(engine, context, "G2", "RESEARCH_BRIEF_NOT_FOUND")
        idempotency_key = preliminary_key or summary_hash({"tenant": tenant_id, "user": user_id, "assets": [item["sha256"] for item in assets if item],
                                                            "brief": brief.get("content_hash") if brief else None, "title": title, "template": template_id})
        if existing is None:
            existing = self.conn.execute(
                """SELECT * FROM office_jobs WHERE tenant_id=? AND user_id=? AND idempotency_key=?
                   ORDER BY created_at DESC LIMIT 1""",
                (tenant_id, user_id, idempotency_key),
            ).fetchone()
        if existing:
            return {"job": self._serialize_job(dict(existing)), "deduped": True}
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        self.conn.execute(
            """INSERT INTO office_jobs(job_id, tenant_id, user_id, conversation_id, workflow_id, status, stage, asset_ids_json,
                 research_brief_id, template_id, title, spec_json, idempotency_key, error_code, created_at, updated_at, completed_at)
               VALUES(?,?,?,?,?,'QUEUED','INSPECTING',?,?,?,?,'{}',?,NULL,?,?,NULL)""",
            (job_id, tenant_id, user_id, conversation_id, workflow_id, json.dumps(asset_ids), brief_id, template_id, title[:160],
             idempotency_key, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )
        job = self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id)
        if auto_run:
            return {"job": self.run_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id), "deduped": False}
        return {"job": job, "deduped": False}

    def run_job(self, *, job_id: str, tenant_id: str, user_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM office_jobs WHERE job_id=? AND tenant_id=? AND user_id=?",
            (job_id, tenant_id, user_id),
        ).fetchone()
        job = dict(row) if row else None
        if not job:
            raise OfficePolicyError("OFFICE_JOB_NOT_FOUND")
        if job["status"] == "SUCCEEDED":
            return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id) or job
        if job["status"] == "CANCELED":
            raise OfficePolicyError("OFFICE_JOB_CANCELED")
        now = self.now()
        asset_ids = json.loads(job["asset_ids_json"])
        context = GateContext(
            request_id=f"req_{uuid.uuid4().hex[:16]}", tenant_id=tenant_id, user_id=user_id,
            conversation_id=job.get("conversation_id"), requested_domain="OFFICE", action="RUN_PPT_JOB",
            workflow_id=job.get("workflow_id"), attachment_ids=tuple(asset_ids),
            input_summary_hash=summary_hash({"job_id": job_id, "assets": asset_ids, "brief": job.get("research_brief_id")}),
            data_classification="INTERNAL",
        )
        engine = GateEngine(self.conn, now=now.isoformat(timespec="seconds"), audit_logger=self.audit_logger)
        try:
            engine.evaluate(context, [
                ("G0", lambda: GateDecision("G0", "ALLOW" if feature_enabled(self.conn, tenant_id, "office_enabled") else "BLOCK", "FEATURE_ENABLED" if feature_enabled(self.conn, tenant_id, "office_enabled") else "FEATURE_DISABLED")),
                ("G1", lambda: GateDecision("G1", "ALLOW", "OFFICE_WORKER_ROUTE_CONFIRMED")),
                ("G2", lambda: self._asset_gate(tenant_id, user_id, asset_ids, job.get("research_brief_id"))),
                ("G3", lambda: GateDecision("G3", "ALLOW" if job.get("template_id") == "template_default" else "BLOCK", "OFFICE_TEMPLATE_ALLOWED" if job.get("template_id") == "template_default" else "OFFICE_TEMPLATE_NOT_FOUND")),
                ("G4", lambda: GateDecision("G4", "ALLOW", "TRANSIENT_SESSION_AUTO_EXECUTION")),
                ("G5", lambda: GateDecision("G5", "ALLOW", "OFFICE_WORKER_CLAIMED")),
            ])
        except GateError as exc:
            self._update(job_id, "FAILED", "FAILED", now, error=exc.decision.reason_code, completed=True)
            return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id) or {"job_id": job_id, "status": "FAILED", "error_code": exc.decision.reason_code}
        self._update(job_id, "RUNNING", "EXTRACTING", now)
        try:
            extractions = [self._extract_asset(asset_id=item, tenant_id=tenant_id, user_id=user_id) for item in asset_ids]
            self._raise_if_canceled(job_id)
            fragments = self._merge_fragments(extractions)
            brief = self._get_brief(job.get("research_brief_id"), tenant_id, user_id) if job.get("research_brief_id") else None
            self._update(job_id, "RUNNING", "SPEC_VALIDATING", now)
            if feature_enabled(self.conn, tenant_id, "office_model_processing_enabled"):
                # The adapter only gets minimal fragments assembled above.
                spec = self.model_gateway.create_spec(title=job["title"], fragments=fragments, brief=brief)
            else:
                spec = deterministic_spec(job["title"], fragments, brief)
            self._raise_if_canceled(job_id)
            self._bind_asset_sources(spec, asset_ids, fragments)
            validate_spec(spec, brief=brief)
            self._update(job_id, "RUNNING", "GENERATING", self.now(), spec=spec)
            version = self._generate_and_render(job, spec, brief)
            try:
                self._raise_if_canceled(job_id)
            except OfficePolicyError:
                self._discard_artifact_version(version["version_id"])
                raise
            engine.record(context, GateDecision("G6", "ALLOW", "OFFICE_STRUCTURE_DATA_RENDER_VALIDATED", {"version_id": version["version_id"]}))
            engine.record(context, GateDecision("G7", "ALLOW", "OFFICE_PRIVATE_ARTIFACT_DELIVERED", {"retention_days": self.RETENTION_DAYS}))
            self._update(job_id, "SUCCEEDED", "DELIVERED", self.now(), completed=True)
            if job.get("workflow_id"):
                update_workflow(self.conn, job["workflow_id"], status="DELIVERED", now=self.now().isoformat(timespec="seconds"), office_job_id=job_id, output_value=version)
            self._audit("office.job.deliver", "office_job", job_id, {"status": "SUCCEEDED", "workflow_id": job.get("workflow_id")})
            return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id) or {"job_id": job_id, "artifact": version}
        except OfficePolicyError as exc:
            if exc.code == "OFFICE_JOB_CANCELED":
                # Cancellation is an intentional terminal state, not a G6
                # quality failure.  Do not overwrite it with FAILED or expose
                # any just-produced artifact.
                return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id) or {"job_id": job_id, "status": "CANCELED"}
            engine.record(context, GateDecision("G6", "BLOCK", exc.code))
            self._update(job_id, "FAILED", "FAILED", self.now(), error=exc.code, completed=True)
            if job.get("workflow_id"):
                update_workflow(self.conn, job["workflow_id"], status="FAILED", now=self.now().isoformat(timespec="seconds"), office_job_id=job_id, output_value={"error_code": exc.code})
            self._audit("office.job.failed", "office_job", job_id, {"error_code": exc.code, "workflow_id": job.get("workflow_id")})
            return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id) or {"job_id": job_id, "status": "FAILED", "error_code": exc.code}
        except Exception:
            engine.record(context, GateDecision("G6", "BLOCK", "OFFICE_WORKER_FAILED"))
            self._update(job_id, "FAILED", "FAILED", self.now(), error="OFFICE_WORKER_FAILED", completed=True)
            if job.get("workflow_id"):
                update_workflow(self.conn, job["workflow_id"], status="FAILED", now=self.now().isoformat(timespec="seconds"), office_job_id=job_id, output_value={"error_code": "OFFICE_WORKER_FAILED"})
            self._audit("office.job.failed", "office_job", job_id, {"error_code": "OFFICE_WORKER_FAILED", "workflow_id": job.get("workflow_id")})
            return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id) or {"job_id": job_id, "status": "FAILED", "error_code": "OFFICE_WORKER_FAILED"}

    def extract_asset(self, *, asset_id: str, tenant_id: str, user_id: str, mode_override: str = "AUTO") -> dict[str, Any]:
        if not feature_enabled(self.conn, tenant_id, "office_enabled"):
            raise OfficePolicyError("FEATURE_DISABLED")
        if mode_override.upper() == "INSPECTION":
            raise OfficePolicyError("OFFICE_DISABLED_IN_INSPECTION_MODE")
        return self._extract_asset(asset_id=asset_id, tenant_id=tenant_id, user_id=user_id)

    def cancel_job(self, *, job_id: str, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id)
        if not job or job["status"] == "SUCCEEDED":
            return None
        self._update(job_id, "CANCELED", "CANCELED", self.now(), completed=True)
        self._audit("office.job.cancel", "office_job", job_id, {"status": "CANCELED"})
        return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id)

    def retry_job(self, *, job_id: str, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        """Requeue only transient failures; a new artifact is never silently appended."""
        job = self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id)
        if not job:
            return None
        retryable = {"OFFICE_RENDER_TIMEOUT", "OFFICE_WORKER_FAILED", "OFFICE_RENDER_RUNTIME_UNAVAILABLE"}
        if job["status"] not in {"FAILED", "RETRYABLE_FAILED"} or job.get("error_code") not in retryable:
            raise OfficePolicyError("OFFICE_JOB_NOT_RETRYABLE")
        self._update(job_id, "QUEUED", "INSPECTING", self.now(), error="")
        self._audit("office.job.retry", "office_job", job_id, {"previous_error": job.get("error_code")})
        return self.get_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id)

    def get_job(self, *, job_id: str, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM office_jobs WHERE job_id=? AND tenant_id=? AND user_id=?", (job_id, tenant_id, user_id)).fetchone()
        if not row:
            return None
        result = self._serialize_job(dict(row))
        artifacts = self.conn.execute(
            """SELECT * FROM office_artifact_versions
               WHERE job_id=? AND status='SUCCEEDED' AND expires_at>? ORDER BY created_at""",
            (job_id, self.now().isoformat(timespec="seconds")),
        ).fetchall()
        result["artifacts"] = [self._public_artifact(dict(item)) for item in artifacts]
        return result

    def get_artifact(self, *, version_id: str, tenant_id: str, user_id: str, kind: str) -> tuple[dict, Path] | None:
        row = self.conn.execute(
            """SELECT * FROM office_artifact_versions WHERE version_id=? AND tenant_id=? AND user_id=? AND status='SUCCEEDED'
               AND expires_at>?""",
            (version_id, tenant_id, user_id, self.now().isoformat(timespec="seconds")),
        ).fetchone()
        if not row:
            return None
        artifact = dict(row)
        key = artifact["preview_key"] if kind == "preview" else artifact.get("preview_png_key") if kind == "preview_png" else artifact["storage_key"]
        path = self.artifact_root / key
        return (artifact, path) if path.is_file() else None

    def cleanup_expired(self) -> int:
        now = self.now().isoformat(timespec="seconds")
        expired_extractions = self.conn.execute(
            "DELETE FROM office_extractions WHERE expires_at<=?",
            (now,),
        ).rowcount
        artifacts = self.conn.execute("SELECT * FROM office_artifact_versions WHERE status='SUCCEEDED' AND expires_at<=?", (now,)).fetchall()
        for raw_artifact in artifacts:
            artifact = dict(raw_artifact)
            (self.artifact_root / artifact["storage_key"]).unlink(missing_ok=True)
            (self.artifact_root / artifact["preview_key"]).unlink(missing_ok=True)
            if artifact.get("preview_png_key"):
                (self.artifact_root / artifact["preview_png_key"]).unlink(missing_ok=True)
            self.conn.execute("UPDATE office_artifact_versions SET status='EXPIRED', deleted_at=? WHERE version_id=?", (now, artifact["version_id"]))
        return self.assets.cleanup_expired() + len(artifacts) + max(0, expired_extractions)

    def _asset_gate(self, tenant_id: str, user_id: str, asset_ids: list[str], brief_id: str | None) -> GateDecision:
        if brief_id:
            brief = self._get_brief(brief_id, tenant_id, user_id)
            decision = research_brief_decision(brief or {}, enabled=feature_enabled(self.conn, tenant_id, "research_to_office_enabled"), same_owner=bool(brief))
            if not decision.allowed:
                # `_asset_gate` is evaluated at Office G2.  A malformed brief
                # may be diagnosed as a research-content G3 internally, but
                # it must not violate GateEngine's ordered callback contract
                # by returning a differently named decision from this slot.
                return GateDecision("G2", decision.decision, decision.reason_code, decision.allowed_scope)
            if asset_ids and not all(self.assets.get(asset_id=item, tenant_id=tenant_id, user_id=user_id) for item in asset_ids):
                return GateDecision("G2", "BLOCK", "OFFICE_ASSET_NOT_FOUND")
            return decision
        if not asset_ids:
            return GateDecision("G2", "ALLOW", "OFFICE_NO_ASSET_YET")
        return GateDecision("G2", "ALLOW" if all(self.assets.get(asset_id=item, tenant_id=tenant_id, user_id=user_id) for item in asset_ids) else "BLOCK",
                            "OFFICE_ASSET_ALLOWED" if all(self.assets.get(asset_id=item, tenant_id=tenant_id, user_id=user_id) for item in asset_ids) else "OFFICE_ASSET_NOT_FOUND")

    def _can_schedule(self, tenant_id: str, user_id: str) -> bool:
        active = self.conn.execute("SELECT COUNT(*) AS n FROM office_jobs WHERE tenant_id=? AND user_id=? AND status IN ('QUEUED','RUNNING')", (tenant_id, user_id)).fetchone()
        return int(active["n"] if hasattr(active, "keys") else active[0]) < 1

    def _block(self, engine: GateEngine, context: GateContext, gate: str, reason: str) -> dict[str, Any]:
        decision = engine.record(context, GateDecision(gate, "BLOCK", reason))
        return {"status": "BLOCKED", "reason_code": reason, "gate": decision.public_dict()}

    @staticmethod
    def _blocked_action(title: str) -> bool:
        normalized = str(title or "").upper()
        return any(token in normalized for token in ("覆盖", "外部共享", "邮件", "M365", "WPS", "数据库写回"))

    def _get_brief(self, brief_id: str | None, tenant_id: str, user_id: str) -> dict | None:
        if not brief_id:
            return None
        row = self.conn.execute(
            """SELECT brief_json FROM research_briefs
               WHERE brief_id=? AND tenant_id=? AND user_id=? AND expires_at>?""",
            (brief_id, tenant_id, user_id, self.now().isoformat(timespec="seconds")),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["brief_json"])

    def _extract_asset(self, *, asset_id: str, tenant_id: str, user_id: str) -> dict:
        existing = self.conn.execute(
            "SELECT extraction_json FROM office_extractions WHERE asset_id=? AND tenant_id=? AND user_id=? AND status='SUCCEEDED'",
            (asset_id, tenant_id, user_id),
        ).fetchone()
        if existing:
            return json.loads(existing["extraction_json"])
        stored = self.assets.read(asset_id=asset_id, tenant_id=tenant_id, user_id=user_id)
        if not stored:
            raise OfficePolicyError("OFFICE_ASSET_NOT_FOUND")
        asset, content = stored
        payload = extract(asset["filename"], content)
        self.conn.execute(
            """INSERT INTO office_extractions(extraction_id, asset_id, tenant_id, user_id, status, extraction_json, content_hash, created_at, expires_at)
               VALUES(?,?,?,?, 'SUCCEEDED',?,?,?,?)""",
            (f"ext_{uuid.uuid4().hex[:16]}", asset_id, tenant_id, user_id, json.dumps(payload, ensure_ascii=False), summary_hash(payload),
             self.now().isoformat(timespec="seconds"), (self.now() + timedelta(days=self.RETENTION_DAYS)).isoformat(timespec="seconds")),
        )
        return payload

    @staticmethod
    def _merge_fragments(extractions: list[dict]) -> dict:
        metrics, sheets, headings, paragraphs = [], [], [], []
        for item in extractions:
            mini = minimal_fragments(item)
            metrics.extend(mini.get("metrics") or [])
            sheets.extend(mini.get("sheets") or [])
            headings.extend(mini.get("headings") or [])
            paragraphs.extend(mini.get("paragraphs") or [])
        return {"kind": "mixed" if len(extractions) > 1 else (extractions[0].get("kind") if extractions else "unknown"),
                "metrics": metrics[:18], "sheets": sheets[:3], "headings": headings[:18], "paragraphs": paragraphs[:18],
                "data_classification": "INTERNAL", "purpose": "CREATE_MANAGEMENT_PPT"}

    def _generate_and_render(self, job: dict, spec: dict, brief: dict | None) -> dict:
        version_id = f"art_{uuid.uuid4().hex[:16]}"
        private_dir = Path(job["tenant_id"]) / job["user_id"] / version_id
        output_dir = self.artifact_root / private_dir
        pptx = output_dir / "management_deck.pptx"
        try:
            self.generator(spec, output_path=pptx, brief=brief)
            self._raise_if_canceled(job["job_id"])
            validate_pptx_structure(pptx, spec)
            self._update(job["job_id"], "RUNNING", "RENDER_VALIDATING", self.now())
            pdf = self.renderer(pptx, output_dir)
            preview = self._create_preview(pdf)
            validate_rendered_preview(pdf, preview, expected_pages=len(spec.get("slides") or []))
            self._raise_if_canceled(job["job_id"])
            now = self.now()
            self.conn.execute(
                """INSERT INTO office_artifact_versions(version_id, job_id, tenant_id, user_id, artifact_type, status, storage_key,
                     preview_key, preview_png_key, sha256, source_refs_json, created_at, expires_at, deleted_at)
                   VALUES(?,?,?,?, 'PPTX', 'SUCCEEDED',?,?,?,?,?,?,?,NULL)""",
                (version_id, job["job_id"], job["tenant_id"], job["user_id"], str(private_dir / pptx.name), str(private_dir / pdf.name), str(private_dir / preview.name),
                 hashlib.sha256(pptx.read_bytes()).hexdigest(), json.dumps({"asset_ids": json.loads(job["asset_ids_json"]), "asset_locations": self._asset_locations(spec), "brief_id": job.get("research_brief_id")}),
                 now.isoformat(timespec="seconds"), (now + timedelta(days=self.RETENTION_DAYS)).isoformat(timespec="seconds")),
            )
            return {"version_id": version_id}
        except Exception:
            # A failed validation/render must not leave an undiscoverable
            # half-artifact that a later retry could accidentally serve.
            shutil.rmtree(output_dir, ignore_errors=True)
            raise

    def _raise_if_canceled(self, job_id: str) -> None:
        row = self.conn.execute("SELECT status FROM office_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row and str(row["status"] if hasattr(row, "keys") else row[0]) == "CANCELED":
            raise OfficePolicyError("OFFICE_JOB_CANCELED")

    def _discard_artifact_version(self, version_id: str) -> None:
        row = self.conn.execute("SELECT * FROM office_artifact_versions WHERE version_id=?", (version_id,)).fetchone()
        if not row:
            return
        artifact = dict(row)
        for key in ("storage_key", "preview_key", "preview_png_key"):
            if artifact.get(key):
                (self.artifact_root / artifact[key]).unlink(missing_ok=True)
        self.conn.execute("UPDATE office_artifact_versions SET status='CANCELED', deleted_at=? WHERE version_id=?",
                          (self.now().isoformat(timespec="seconds"), version_id))

    def _create_preview(self, pdf: Path) -> Path:
        png = pdf.with_suffix(".png")
        converter = shutil.which("pdftoppm")
        if not converter:
            raise OfficePolicyError("OFFICE_RENDER_RUNTIME_UNAVAILABLE")
        page_dir = Path(tempfile.mkdtemp(prefix="preview-pages-", dir=pdf.parent))
        try:
            import subprocess
            subprocess.run([converter, "-png", "-scale-to-x", "960", "-scale-to-y", "-1", str(pdf), str(page_dir / "page")], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            pages = sorted(page_dir.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[-1]))
            from pypdf import PdfReader
            validate_rendered_pages(pages, expected_pages=len(PdfReader(str(pdf)).pages))
            # Expose one bounded PNG artifact while retaining visual coverage
            # for every page.  It is deliberately a contact sheet rather than
            # a private directory of untracked preview files.
            from PIL import Image
            thumbs = []
            for page in pages:
                with Image.open(page) as image:
                    thumbnail = image.convert("RGB")
                    thumbnail.thumbnail((480, 270))
                    thumbs.append(thumbnail.copy())
            columns = min(3, len(thumbs))
            rows = (len(thumbs) + columns - 1) // columns
            canvas = Image.new("RGB", (columns * 480, rows * 270), (248, 250, 252))
            for index, thumbnail in enumerate(thumbs):
                x, y = (index % columns) * 480, (index // columns) * 270
                canvas.paste(thumbnail, (x, y))
            canvas.save(png, format="PNG")
        except OfficePolicyError:
            raise
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise OfficePolicyError("OFFICE_RENDER_FAILED") from exc
        except Exception as exc:
            raise OfficePolicyError("OFFICE_RENDER_FAILED") from exc
        finally:
            shutil.rmtree(page_dir, ignore_errors=True)
        return png

    def _update(self, job_id: str, status: str, stage: str, now: datetime, *, spec: dict | None = None,
                error: str | None = None, completed: bool = False) -> None:
        fields, args = ["status=?", "stage=?", "updated_at=?"], [status, stage, now.isoformat(timespec="seconds")]
        if spec is not None:
            fields.append("spec_json=?")
            args.append(json.dumps(spec, ensure_ascii=False))
        if error is not None:
            fields.append("error_code=?")
            args.append(error)
        if completed:
            fields.append("completed_at=?")
            args.append(now.isoformat(timespec="seconds"))
        args.append(job_id)
        where = "WHERE job_id=?" if status == "CANCELED" else "WHERE job_id=? AND status!='CANCELED'"
        self.conn.execute(f"UPDATE office_jobs SET {', '.join(fields)} {where}", args)

    @staticmethod
    def _serialize_job(job: dict) -> dict:
        job["asset_ids"] = json.loads(job.pop("asset_ids_json") or "[]")
        job["spec"] = json.loads(job.pop("spec_json") or "{}")
        job.pop("idempotency_key", None)
        return job

    @staticmethod
    def _asset_locations(spec: dict) -> list[str]:
        locations: list[str] = []
        for slide in spec.get("slides") or []:
            locations.extend(str(item) for item in slide.get("asset_sources") or [])
        return list(dict.fromkeys(locations))[:100]

    @staticmethod
    def _bind_asset_sources(spec: dict, asset_ids: list[str], fragments: dict) -> None:
        """Bind source locations deterministically after model planning.

        The model may choose the page narrative but cannot invent a file path
        or delete the provenance of numeric material drawn from an asset.
        """
        locations = [str(item) for item in fragments.get("source_refs") or [] if item]
        if not locations or not asset_ids:
            return
        labels = [f"附件{index + 1}:{location}" for index, location in enumerate(locations[:12])]
        for slide in spec.get("slides") or []:
            if slide.get("layout") == "kpi" or slide.get("metrics"):
                slide["asset_sources"] = list(dict.fromkeys((slide.get("asset_sources") or []) + labels[:8]))[:12]

    @staticmethod
    def _public_artifact(artifact: dict) -> dict:
        """Never return private storage keys or source asset identifiers to UI/Trace."""
        return {
            "version_id": artifact["version_id"],
            "job_id": artifact["job_id"],
            "artifact_type": artifact["artifact_type"],
            "status": artifact["status"],
            "sha256": artifact["sha256"],
            "created_at": artifact["created_at"],
            "expires_at": artifact["expires_at"],
            "source_refs_hash": summary_hash(artifact.get("source_refs_json") or ""),
        }

    def _audit(self, action: str, object_type: str, object_id: str, after: dict) -> None:
        if self.audit_logger:
            self.audit_logger(action=action, object_type=object_type, object_id=object_id, after=after)
