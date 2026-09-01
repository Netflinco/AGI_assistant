#!/usr/bin/env python3
"""P0 runnable MVP for Wanxiang AGI inspection assistant."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

from credential_vault import CredentialVault, CredentialVaultError
from comparison_service import (
    eas_ovd_contract_report,
    OvdAdapterFailure,
    SafeOvdAdapter,
    evaluate_slot_evidence,
    ovd_contract_report,
)
from conversation_context import decide_continuation, public_context_summary
from agent_core import validate_agent_manifest
from agent_skills import public_agent_catalog, public_skill_catalog, skill_descriptor, standard_agent_catalog
from online_agent import (
    DeepVisionPaaSClient,
    IntentAnalyzer,
    OnlineAgentError,
    OnlineInspectionAgent,
    OpenQuestionResponder,
    VisualReasoner,
    get_online_agent,
)
from web_search import WebSearchClient, WebSearchError
from travel_enrichment import WikimediaImageClient, is_precise_venue_address, is_specific_venue_name
from agent_governance.audit import audit_payload, summary_hash
from agent_governance.contracts import GateContext, GateDecision
from agent_governance.gate_engine import GateEngine
from agent_governance.policy_registry import (
    DEFAULT_FEATURE_FLAGS,
    FeatureFlagPolicyError,
    apply_feature_updates,
    feature_enabled,
    feature_flag_definitions,
    feature_snapshot,
)
from agent_governance.workflow_store import create_workflow, get_workflow, update_workflow
from open_research.gateway import TavilyGateway
from open_research.intent import DomainRouter, EntityResolver
from open_research.memory import active_memories, delete_memory
from open_research.orchestrator import OpenResearchService
from open_research.source_policy import (
    SOURCE_POLICY_STATUSES,
    SOURCE_TIERS,
    load_active_source_policies,
    normalize_domain,
    upsert_source_policy,
)
from office_agent.assets import OfficeAssetService
from office_agent.jobs import OfficeJobService
from office_agent.policy import MAX_BATCH_BYTES, MAX_BATCH_FILES, MAX_FILE_BYTES, OfficePolicyError
from office_agent.readiness import production_readiness_errors
from office_agent.worker import OfficeJobWorker
from visual_compliance import (
    VISUAL_COMPLIANCE_ALIASES,
    VISUAL_COMPLIANCE_CAPABILITY_ID,
    VISUAL_COMPLIANCE_EVENT_TYPE,
    VISUAL_COMPLIANCE_NAME,
    extract_visual_compliance_pack,
    is_visual_compliance_request,
    visual_compliance_goal,
)

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - production runtime ships Pillow, fallback protects startup.
    Image = None
    ImageOps = None


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DB_PATH = ROOT / "data" / "agi_inspection.db"
DB_PATH = Path(os.environ.get("AGI_INSPECTION_DB", DEFAULT_DB_PATH))
SCHEDULED_EVIDENCE_DIR = ROOT / "data" / "scheduled_evidence"
ONLINE_SNAPSHOT_EVIDENCE_DIR = ROOT / "data" / "online_snapshot_evidence"
OPEN_QA_EXPORT_DIR = Path(os.environ.get("AGI_OPEN_QA_EXPORT_DIR", ROOT / "data" / "open_qa_exports"))
OFFICE_ASSET_DIR = Path(os.environ.get("AGI_OFFICE_ASSET_DIR", ROOT / "data" / "office_assets"))
OFFICE_ARTIFACT_DIR = Path(os.environ.get("AGI_OFFICE_ARTIFACT_DIR", ROOT / "data" / "office_artifacts"))
OFFICE_UPLOAD_STAGING_DIR = Path(os.environ.get("AGI_OFFICE_UPLOAD_STAGING_DIR", ROOT / "data" / "office_upload_staging"))
TRAVEL_MEDIA_CLIENT = WikimediaImageClient(timeout_seconds=8)
KNOWLEDGE_UPLOAD_DIR = STATIC_DIR / "uploads" / "knowledge"
CREDENTIAL_KEY_PATH = ROOT / "data" / ".credential_master_key"
CN_TZ = timezone(timedelta(hours=8))
CURRENT_DATE = date.fromisoformat(os.environ.get("CURRENT_DATE", datetime.now(CN_TZ).date().isoformat()))
MAX_KNOWLEDGE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_KNOWLEDGE_IMAGE_BATCH_COUNT = 10
MAX_KNOWLEDGE_IMAGE_BATCH_BYTES = 32 * 1024 * 1024
MAX_INSPECTION_KNOWLEDGE_HITS = 5
MAX_INSPECTION_REFERENCE_IMAGES = 5
MAX_INSPECTION_REFERENCE_EDGE = 1280
MAX_INSPECTION_REFERENCE_IMAGE_BYTES = 400 * 1024
MAX_INSPECTION_REFERENCE_TOTAL_BYTES = 1800 * 1024
MAX_JSON_BODY_BYTES = 48 * 1024 * 1024
MAX_OFFICE_UPLOAD_BODY_BYTES = 128 * 1024 * 1024
KNOWLEDGE_IMAGE_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class _MultipartBodyReader:
    """Bounded multipart reader that never assembles the HTTP body in memory."""

    def __init__(self, source, total_bytes: int):
        self.source = source
        self.remaining = total_bytes
        self.buffer = bytearray()

    def _fill(self) -> None:
        if self.remaining <= 0:
            return
        chunk = self.source.read(min(64 * 1024, self.remaining))
        if not chunk:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "上传请求体不完整"})
        self.remaining -= len(chunk)
        self.buffer.extend(chunk)

    def read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count and self.remaining:
            self._fill()
        if len(self.buffer) < count:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "multipart 格式不完整"})
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def read_until(self, marker: bytes, *, sink=None, max_bytes: int | None = None) -> bytes | None:
        """Consume bytes up to ``marker`` and optionally stream them to sink."""
        result = bytearray() if sink is None else None
        seen = 0
        keep = max(1, len(marker) - 1)

        def emit(chunk: bytes) -> None:
            nonlocal seen
            seen += len(chunk)
            if max_bytes is not None and seen > max_bytes:
                raise ApiError("OFFICE_FILE_TOO_LARGE", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            if sink is None:
                assert result is not None
                result.extend(chunk)
            elif chunk:
                sink.write(chunk)

        while True:
            marker_at = self.buffer.find(marker)
            if marker_at >= 0:
                emit(bytes(self.buffer[:marker_at]))
                del self.buffer[:marker_at + len(marker)]
                return bytes(result) if result is not None else None
            emit_count = max(0, len(self.buffer) - keep)
            if emit_count:
                emit(bytes(self.buffer[:emit_count]))
                del self.buffer[:emit_count]
            if self.remaining <= 0:
                raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "multipart boundary 缺失"})
            self._fill()


ERRORS = {
    "AUTH_REQUIRED": "未登录或登录态失效",
    "PERMISSION_DENIED": "当前角色没有执行该操作的权限",
    "TENANT_SCOPE_DENIED": "请求范围超出当前用户授权组织",
    "INTENT_LOW_CONFIDENCE": "意图置信度不足，需要用户确认",
    "SLOT_MISSING": "缺少关键槽位",
    "ENTITY_AMBIGUOUS": "实体存在多个候选，需要用户选择",
    "PLAN_NOT_CONFIRMABLE": "当前计划不可确认",
    "RESOURCE_NOT_FOUND": "资源不存在",
    "CAMERA_OFFLINE": "存在离线摄像头",
    "CAPABILITY_NOT_AVAILABLE": "能力不可用",
    "VALIDATION_FAILED": "业务校验失败",
    "IDEMPOTENCY_CONFLICT": "幂等键冲突",
    "BAD_REQUEST": "请求参数不正确",
    "UPSTREAM_HTTP_ERROR": "在线数据服务请求失败",
    "UPSTREAM_UNAVAILABLE": "在线数据服务暂时不可用",
    "UPSTREAM_INVALID_RESPONSE": "在线数据服务返回异常",
    "UPSTREAM_REJECTED": "在线数据服务拒绝了请求",
    "INTEGRATION_READ_ONLY": "当前线上接入为只读模式",
    "INTEGRATION_ALREADY_EXISTS": "该租户已经接入",
    "INTEGRATION_VALIDATION_FAILED": "租户凭证验证失败",
    "INTEGRATION_SYNC_FAILED": "租户门店同步失败",
    "SECURE_STORAGE_UNAVAILABLE": "安全凭证存储不可用",
    "AGENT_MANIFEST_INVALID": "Agent Manifest 校验未通过",
    "AGENT_MEMORY_INVALID": "长期记忆内容校验未通过",
    "AGENT_MEMORY_CONFIRM_REQUIRED": "重要长期记忆需要确认",
    "AGENT_KNOWLEDGE_INVALID": "知识内容校验未通过",
    "AGENT_KNOWLEDGE_ASSET_INVALID": "知识库图片上传失败",
    "CATALOG_INVALID": "商品目录数据校验失败",
    "CATALOG_VERSION_CONFLICT": "目录版本或 ETag 冲突",
    "CATALOG_NOT_PUBLISHED": "目录版本尚未发布",
    "COMPARISON_INVALID": "比对会话或证据校验失败",
    "COMPARISON_PREREQUISITE_MISSING": "比对前置条件未满足，已转人工复核",
    "OVD_CONTRACT_FAILED": "OVD 响应契约校验失败",
    "FEATURE_DISABLED": "当前租户尚未开启此能力",
    "RESEARCH_EGRESS_BLOCKED": "该问题可能包含企业数据、敏感信息或个人信息，未发送至公共搜索服务",
    "RESEARCH_DIRECT_FETCH_FORBIDDEN": "不能按用户提供的地址直接抓取内容，请使用受控公开检索",
    "RESEARCH_RATE_LIMITED": "公开检索已达到当前配额，请稍后再试",
    "SEARCH_RATE_LIMITED": "公开检索服务已限流，请稍后再试",
    "OFFICE_FILE_TOO_LARGE": "单个 Office 文件不能超过 40MB",
    "OFFICE_BATCH_FILE_LIMIT_EXCEEDED": "一次最多上传 3 个文件",
    "OFFICE_BATCH_SIZE_LIMIT_EXCEEDED": "单次上传总大小不能超过 120MB",
    "OFFICE_STRONG_SENSITIVE_DATA": "文件包含密钥、证件或银行卡等强敏感信息，已阻断处理",
    "OFFICE_ASSET_NOT_FOUND": "文件不存在、已过期或无权访问",
    "OFFICE_CONTENT_LIMIT_EXCEEDED": "文件内容超过当前处理上限",
    "OFFICE_RENDER_RUNTIME_UNAVAILABLE": "Office 渲染运行时不可用，未交付不完整产物",
    "OFFICE_RENDER_FAILED": "Office 渲染校验失败，未交付产物",
    "OFFICE_RENDER_TIMEOUT": "Office 渲染超时，未交付不完整产物",
    "OFFICE_STRUCTURE_INVALID": "生成的 Office 文件未通过结构校验",
    "OFFICE_DATA_VALIDATION_FAILED": "生成的 Office 文件未通过数据校验",
    "OFFICE_JOB_NOT_RETRYABLE": "当前 Office 任务不满足重试条件",
    "OFFICE_MANUAL_RUN_DISABLED": "生产环境由独立 Office Worker 执行任务，不能在 Web 请求中直接生成",
    "RESEARCH_BRIEF_INVALID": "检索结果缺少必要引用，不能用于生成事实性 Office 产物",
    "RESEARCH_BRIEF_STATUS_BLOCKED": "当前检索结果不能作为事实性 Office 产物输入",
    "INTERNAL_ERROR": "服务内部异常，请稍后重试",
}


def public_online_delivery_failure(exc: OnlineAgentError) -> dict:
    """Translate upstream failures into a durable, actionable user-message state."""
    retryable_codes = {"UPSTREAM_HTTP_ERROR", "UPSTREAM_UNAVAILABLE", "UPSTREAM_INVALID_RESPONSE", "LLM_UNAVAILABLE"}
    retryable = exc.code in retryable_codes
    if retryable:
        message = "暂时无法读取所需的在线数据。本次请求已保留，约 2 分钟后可重试。"
        next_action = "RETRY"
        retry_after_seconds = 120
        state = "TEMPORARY_FAILURE"
    elif exc.code == "UPSTREAM_REJECTED":
        vendor_message = str(exc.detail.get("vendor_message") or "")
        if exc.detail.get("vendor_code") == 400 and "部署形态" in vendor_message:
            message = "DeepVision 的已配置能力服务缺少该产品的部署形态配置。基础摄像头巡检不受此项影响。"
            next_action = "CONFIGURE_PRODUCT_DEPLOYMENT"
        else:
            message = "当前无法完成这项在线数据查询。请检查当前租户接入与数据权限后重试。"
            next_action = "CHECK_ACCESS"
        retry_after_seconds = None
        state = "CAPABILITY_UNAVAILABLE"
    else:
        message = "这项巡检请求暂时无法完成。请补充业务对象或稍后重试。"
        next_action = "CLARIFY_OR_RETRY"
        retry_after_seconds = None
        state = "TEMPORARY_FAILURE"
    return {
        "status": "FAILED",
        "state": state,
        "code": exc.code,
        "retryable": retryable,
        "retry_after_seconds": retry_after_seconds,
        "next_action": next_action,
        "message": message,
        "correlation_id": f"cor_{uuid.uuid4().hex[:12]}",
    }


_CREDENTIAL_VAULT: CredentialVault | None = None
_TENANT_AGENT_CACHE: dict[str, tuple[str, OnlineInspectionAgent]] = {}
_TENANT_AGENT_LOCK = threading.Lock()
# Local test hook.  Production leaves this as None and always constructs the
# Tavily-only gateway from encrypted runtime configuration.
OPEN_RESEARCH_GATEWAY_FACTORY = None


def credential_vault() -> CredentialVault:
    global _CREDENTIAL_VAULT
    if _CREDENTIAL_VAULT is None:
        try:
            _CREDENTIAL_VAULT = CredentialVault(CREDENTIAL_KEY_PATH)
        except (OSError, ValueError) as exc:
            raise ApiError("SECURE_STORAGE_UNAVAILABLE", HTTPStatus.SERVICE_UNAVAILABLE) from exc
    return _CREDENTIAL_VAULT


def environment_tenant_code() -> str:
    online = get_online_agent()
    return str(os.environ.get("DEEPVISION_TENANT_CODE") or (online.tenant_code if online else "")).strip()


def online_agent_for_tenant(
    conn: sqlite3.Connection,
    tenant_code: str | None = None,
    *,
    required: bool = False,
) -> OnlineInspectionAgent | None:
    code = str(tenant_code or environment_tenant_code()).strip()
    environment_agent = get_online_agent()
    model_config, model_cache_token = model_runtime_config(conn)
    web_search_config, web_search_cache_token = web_search_runtime_config(conn, code)
    open_responder = OpenQuestionResponder(
        IntentAnalyzer(model_config),
        WebSearchClient(web_search_config),
        web_search_usage_summary(conn, web_search_config),
    )
    if environment_agent and code == environment_tenant_code():
        if model_config or open_responder.web_search_client.configured:
            return OnlineInspectionAgent(
                environment_agent.client,
                IntentAnalyzer(model_config) if model_config else environment_agent.analyzer,
                VisualReasoner(model_config) if model_config else environment_agent.visual_reasoner,
                open_responder,
            )
        return environment_agent
    if not code:
        return None
    integration = one(
        conn,
        "SELECT * FROM tenant_integrations WHERE tenant_code=? AND status='CONNECTED'",
        (code,),
    )
    if not integration:
        if required:
            raise ApiError("TENANT_SCOPE_DENIED", HTTPStatus.FORBIDDEN)
        return None
    cache_token = f"{integration.get('credential_fingerprint') or integration['updated_at']}:{model_cache_token}:{web_search_cache_token}"
    with _TENANT_AGENT_LOCK:
        cached = _TENANT_AGENT_CACHE.get(code)
        if cached and cached[0] == cache_token:
            return cached[1]
        try:
            credentials = credential_vault().decrypt(integration["encrypted_credentials"])
        except CredentialVaultError as exc:
            raise ApiError("SECURE_STORAGE_UNAVAILABLE", HTTPStatus.SERVICE_UNAVAILABLE) from exc
        client = DeepVisionPaaSClient(
            app_key=str(credentials.get("app_key") or ""),
            app_secret=str(credentials.get("app_secret") or ""),
            tenant_code=code,
            base_url=os.environ.get("DEEPVISION_BASE_URL", "https://api.deepeleph.com"),
        )
        agent = OnlineInspectionAgent(
            client,
            IntentAnalyzer(model_config),
            VisualReasoner(model_config),
            open_responder,
        )
        _TENANT_AGENT_CACHE[code] = (cache_token, agent)
        return agent


def tenant_name_for_code(conn: sqlite3.Connection, tenant_code: str) -> str:
    if tenant_code == environment_tenant_code():
        return os.environ.get("DEEPVISION_TENANT_NAME", tenant_code.upper())
    integration = one(conn, "SELECT tenant_name FROM tenant_integrations WHERE tenant_code=?", (tenant_code,))
    return str(integration.get("tenant_name") if integration else tenant_code)


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value, default=None):
    if value in (None, ""):
        return default
    return json.loads(value)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def dict_row(row: sqlite3.Row | None):
    return None if row is None else dict(row)


def rows(conn: sqlite3.Connection, sql: str, args=()):
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def one(conn: sqlite3.Connection, sql: str, args=()):
    return dict_row(conn.execute(sql, args).fetchone())


def model_runtime_config(conn: sqlite3.Connection) -> tuple[dict, str]:
    env_api_key = str(os.environ.get("AGENT_VLM_API_KEY") or os.environ.get("AGENT_LLM_API_KEY") or "").strip()
    env_model = str(os.environ.get("AGENT_VLM_MODEL") or os.environ.get("AGENT_LLM_MODEL") or "").strip()
    if env_api_key and env_model:
        return {
            "api_key": env_api_key,
            "model": env_model,
            "base_url": str(os.environ.get("AGENT_VLM_BASE_URL") or os.environ.get("AGENT_LLM_BASE_URL") or "").strip(),
            "chat_completions_url": str(os.environ.get("AGENT_VLM_CHAT_COMPLETIONS_URL") or "").strip(),
            "auth_scheme": str(os.environ.get("AGENT_VLM_AUTH_SCHEME") or "Bearer").strip(),
            "max_images": int(os.environ.get("AGENT_VLM_MAX_IMAGES") or 8),
        }, "environment"
    table_exists = one(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='service_configs'",
    )
    if not table_exists:
        return {}, "unconfigured"
    row = one(conn, "SELECT * FROM service_configs WHERE config_key='visual_model'")
    if not row:
        return {}, "unconfigured"
    try:
        secret = credential_vault().decrypt(row["encrypted_value"])
    except CredentialVaultError as exc:
        raise ApiError("SECURE_STORAGE_UNAVAILABLE", HTTPStatus.SERVICE_UNAVAILABLE) from exc
    metadata = json_loads(row.get("public_metadata"), {})
    return {
        "api_key": str(secret.get("api_key") or ""),
        "model": str(metadata.get("model") or ""),
        "base_url": str(metadata.get("base_url") or ""),
        "chat_completions_url": str(metadata.get("chat_completions_url") or ""),
        "auth_scheme": str(metadata.get("auth_scheme") or "Bearer"),
        "max_images": int(metadata.get("max_images") or 8),
    }, str(row.get("updated_at") or "configured")


def save_model_runtime_config(conn: sqlite3.Connection, params: dict) -> dict:
    api_key = str(params.get("api_key") or "").strip()
    model = str(params.get("model") or "").strip()
    url = str(params.get("chat_completions_url") or "").strip()
    auth_scheme = str(params.get("auth_scheme") or "Bearer").strip()
    max_images = int(params.get("max_images") or 8)
    parsed = urlparse(url)
    if not api_key or not model or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("visual model configuration is incomplete")
    if auth_scheme.lower() not in {"raw", "bearer", "token"} or not 1 <= max_images <= 12:
        raise ValueError("visual model configuration is invalid")
    timestamp = now_iso()
    metadata = {
        "model": model,
        "chat_completions_url": url,
        "auth_scheme": auth_scheme,
        "max_images": max_images,
    }
    encrypted = credential_vault().encrypt({"api_key": api_key})
    conn.execute(
        """INSERT INTO service_configs(config_key, encrypted_value, public_metadata, updated_at)
           VALUES('visual_model', ?, ?, ?)
           ON CONFLICT(config_key) DO UPDATE SET
             encrypted_value=excluded.encrypted_value,
             public_metadata=excluded.public_metadata,
             updated_at=excluded.updated_at""",
        (encrypted, json_dumps(metadata), timestamp),
    )
    with _TENANT_AGENT_LOCK:
        _TENANT_AGENT_CACHE.clear()
    return {"configured": True, "model": model, "auth_scheme": auth_scheme, "max_images": max_images}


def web_search_config_key(tenant_id: str | None = None) -> str:
    """Keep outbound-search credentials inside the tenant that owns the setting."""
    tenant = str(tenant_id or "").strip()
    return f"web_search:{tenant}" if tenant else "web_search"


def web_search_runtime_config(conn: sqlite3.Connection, tenant_id: str | None = None) -> tuple[dict, str]:
    """Read public-search credentials without exposing them to the Agent or UI."""
    def bounded_int(value, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    env_provider = str(os.environ.get("AGENT_WEB_SEARCH_PROVIDER") or "").strip().lower()
    env_api_key = str(os.environ.get("AGENT_WEB_SEARCH_API_KEY") or "").strip()
    if env_provider or env_api_key:
        return {
            "provider": env_provider,
            "api_key": env_api_key,
            "max_results": bounded_int(os.environ.get("AGENT_WEB_SEARCH_MAX_RESULTS"), 5, 1, 8),
            "country": str(os.environ.get("AGENT_WEB_SEARCH_COUNTRY") or "").strip(),
            "search_lang": str(os.environ.get("AGENT_WEB_SEARCH_LANG") or "").strip(),
            "timeout_seconds": bounded_int(os.environ.get("AGENT_WEB_SEARCH_TIMEOUT_SECONDS"), 8, 1, 10),
        }, "environment"
    table_exists = one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='service_configs'")
    if not table_exists:
        return {}, "unconfigured"
    config_key = web_search_config_key(tenant_id)
    row = one(conn, "SELECT * FROM service_configs WHERE config_key=?", (config_key,))
    source = "tenant_config" if row and tenant_id else "service_config"
    # Preserve existing deployments that stored one platform-level search credential
    # before tenant-scoped configuration was available.
    if not row and tenant_id:
        row = one(conn, "SELECT * FROM service_configs WHERE config_key='web_search'")
        source = "platform_default" if row else "unconfigured"
    if not row:
        return {}, "unconfigured"
    try:
        secret = credential_vault().decrypt(row["encrypted_value"])
    except CredentialVaultError as exc:
        raise ApiError("SECURE_STORAGE_UNAVAILABLE", HTTPStatus.SERVICE_UNAVAILABLE) from exc
    metadata = json_loads(row.get("public_metadata"), {})
    return {
        "provider": str(metadata.get("provider") or "").strip().lower(),
        "api_key": str(secret.get("api_key") or ""),
        "max_results": bounded_int(metadata.get("max_results"), 5, 1, 8),
        "country": str(metadata.get("country") or "").strip(),
        "search_lang": str(metadata.get("search_lang") or "").strip(),
        "timeout_seconds": bounded_int(metadata.get("timeout_seconds"), 8, 1, 10),
    }, f"{source}:{str(row.get('updated_at') or 'configured')}"


WEB_SEARCH_LOW_BALANCE_RATIO = 0.10


def bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def web_search_credential_fingerprint(config: dict | None) -> str:
    config = config or {}
    provider = str(config.get("provider") or "").strip().lower()
    api_key = str(config.get("api_key") or "").strip()
    if not provider or not api_key:
        return ""
    return hashlib.sha256(f"{provider}:{api_key}".encode("utf-8")).hexdigest()[:24]


def _usage_number(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def web_search_usage_summary(conn: sqlite3.Connection, config: dict | None) -> dict:
    """Summarize local call volume and the latest provider-reported balance."""
    fingerprint = web_search_credential_fingerprint(config)
    empty = {
        "calls_this_month": 0,
        "credits_this_month": 0,
        "used_credits": None,
        "credit_limit": None,
        "remaining_credits": None,
        "remaining_ratio": None,
        "low_balance": False,
        "reported_at": None,
    }
    if not fingerprint or not one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='web_search_usage'"):
        return empty
    month = now_iso()[:7]
    monthly = one(
        conn,
        """SELECT COUNT(*) AS calls, COALESCE(SUM(credits_consumed), 0) AS credits
           FROM web_search_usage
           WHERE credential_fingerprint=? AND operation='SEARCH' AND substr(created_at, 1, 7)=?""",
        (fingerprint, month),
    ) or {}
    latest = one(
        conn,
        """SELECT provider_used_credits, provider_credit_limit, provider_remaining_credits, created_at
           FROM web_search_usage
           WHERE credential_fingerprint=? AND provider_credit_limit IS NOT NULL
           ORDER BY created_at DESC LIMIT 1""",
        (fingerprint,),
    )
    summary = {
        **empty,
        "calls_this_month": int(monthly.get("calls") or 0),
        "credits_this_month": int(monthly.get("credits") or 0),
    }
    if not latest:
        return summary
    used = _usage_number(latest.get("provider_used_credits"))
    limit = _usage_number(latest.get("provider_credit_limit"))
    provider_remaining = _usage_number(latest.get("provider_remaining_credits"))
    if used is None or limit is None or limit <= 0:
        return summary
    # Tavily usage snapshots can lag a completed search. The local ledger records
    # response-confirmed credits immediately, so present the lower safe balance.
    effective_used = max(used, summary["credits_this_month"])
    local_remaining = max(0, limit - effective_used)
    if provider_remaining is None:
        remaining = local_remaining
    else:
        remaining = min(max(provider_remaining, 0), local_remaining)
    ratio = remaining / limit
    return {
        **summary,
        "used_credits": effective_used,
        "credit_limit": limit,
        "remaining_credits": remaining,
        "remaining_ratio": round(ratio, 4),
        "low_balance": ratio < WEB_SEARCH_LOW_BALANCE_RATIO,
        "reported_at": latest.get("created_at"),
    }


def record_web_search_usage(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    conversation_id: str | None,
    config: dict,
    operation: str,
    status: str,
    search: dict | None = None,
    account_usage: dict | None = None,
) -> dict:
    """Write a redacted usage event, then return the key-level monthly summary."""
    fingerprint = web_search_credential_fingerprint(config)
    if not fingerprint:
        return web_search_usage_summary(conn, config)
    search = search if isinstance(search, dict) else {}
    account_usage = account_usage if isinstance(account_usage, dict) else search.get("account_usage")
    account_usage = account_usage if isinstance(account_usage, dict) else {}
    used = _usage_number(account_usage.get("used_credits"))
    limit = _usage_number(account_usage.get("credit_limit"))
    remaining = _usage_number(account_usage.get("remaining_credits"))
    if used is not None and limit is not None:
        remaining = max(0, limit - used) if remaining is None else min(max(remaining, 0), limit)
    usage_events = search.get("usage_events") if isinstance(search.get("usage_events"), list) else []
    if not usage_events:
        call_usage = search.get("usage") if isinstance(search.get("usage"), dict) else {}
        usage_events = [
            {
                "provider": search.get("provider"),
                "request_id": search.get("request_id"),
                "status": status,
                "credits": _usage_number(call_usage.get("credits")) or 0,
            }
        ]
    created_at = now_iso()
    for event in usage_events[:8]:
        if not isinstance(event, dict):
            continue
        event_status = str(event.get("status") or status).upper()
        credits = _usage_number(event.get("credits")) or 0
        conn.execute(
            """INSERT INTO web_search_usage(
                 usage_id, tenant_id, conversation_id, provider, credential_fingerprint,
                 operation, status, provider_request_id, credits_consumed,
                 provider_used_credits, provider_credit_limit, provider_remaining_credits, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"wsu_{uuid.uuid4().hex[:16]}",
                tenant_id,
                conversation_id,
                str(event.get("provider") or search.get("provider") or config.get("provider") or "").lower(),
                fingerprint,
                operation,
                event_status,
                str(event.get("request_id") or "")[:128] or None,
                credits,
                used,
                limit,
                remaining,
                created_at,
            ),
        )
    return web_search_usage_summary(conn, config)


def public_web_search_config(conn: sqlite3.Connection, tenant_id: str | None = None) -> dict:
    """Return operational state only; public API callers never receive the key."""
    config, cache_token = web_search_runtime_config(conn, tenant_id)
    public = WebSearchClient(config).public_config
    if cache_token == "environment":
        source = "environment"
    elif cache_token.startswith("tenant_config:"):
        source = "tenant_config"
    elif cache_token.startswith("platform_default:"):
        source = "platform_default"
    elif cache_token.startswith("service_config:"):
        source = "service_config"
    else:
        source = "unconfigured"
    return {
        **public,
        "source": source,
        "editable": source != "environment",
        "usage": web_search_usage_summary(conn, config),
    }


def save_web_search_runtime_config(conn: sqlite3.Connection, params: dict, tenant_id: str | None = None) -> dict:
    """Persist provider credentials in the existing encrypted service-config store."""
    provider = str(params.get("provider") or "").strip().lower()
    api_key = str(params.get("api_key") or "").strip()
    try:
        max_results = int(params.get("max_results") or 5)
        timeout_seconds = int(params.get("timeout_seconds") or 8)
    except (TypeError, ValueError) as exc:
        raise ValueError("web search configuration is invalid") from exc
    country = str(params.get("country") or "").strip().upper()
    search_lang = str(params.get("search_lang") or "").strip().lower()
    if provider not in {"tavily", "brave"} or not api_key or not 1 <= max_results <= 8 or not 1 <= timeout_seconds <= 10:
        raise ValueError("web search configuration is invalid")
    if len(country) > 8 or len(search_lang) > 16:
        raise ValueError("web search configuration is invalid")
    timestamp = now_iso()
    metadata = {
        "provider": provider,
        "max_results": max_results,
        "country": country,
        "search_lang": search_lang,
        "timeout_seconds": timeout_seconds,
    }
    encrypted = credential_vault().encrypt({"api_key": api_key})
    conn.execute(
        """INSERT INTO service_configs(config_key, encrypted_value, public_metadata, updated_at)
           VALUES(?, ?, ?, ?)
           ON CONFLICT(config_key) DO UPDATE SET
             encrypted_value=excluded.encrypted_value,
             public_metadata=excluded.public_metadata,
             updated_at=excluded.updated_at""",
        (web_search_config_key(tenant_id), encrypted, json_dumps(metadata), timestamp),
    )
    with _TENANT_AGENT_LOCK:
        _TENANT_AGENT_CACHE.clear()
    return {"configured": True, "source": "tenant_config" if tenant_id else "service_config", **metadata}


def init_db(reset: bool = False):
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              role TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              allowed_org_ids TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orgs (
              org_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              parent_id TEXT,
              name TEXT NOT NULL,
              org_type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cameras (
              camera_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              name TEXT NOT NULL,
              point_label TEXT NOT NULL,
              vendor TEXT NOT NULL,
              stream_protocol TEXT NOT NULL,
              stream_status TEXT NOT NULL,
              snapshot_url TEXT NOT NULL,
              last_online_at TEXT,
              calibration_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capabilities (
              capability_id TEXT PRIMARY KEY,
              app_id TEXT NOT NULL,
              app_version_id TEXT NOT NULL,
              name TEXT NOT NULL,
              aliases TEXT NOT NULL,
              event_type TEXT NOT NULL,
              calibration_required INTEGER NOT NULL,
              allow_full_frame INTEGER NOT NULL,
              status TEXT NOT NULL,
              scene TEXT NOT NULL,
              data_source_type TEXT NOT NULL,
              version TEXT NOT NULL,
              thresholds_default TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
              conversation_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              page_code TEXT,
              org_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              message_id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL,
              sender TEXT NOT NULL,
              content TEXT NOT NULL,
              linked_plan_id TEXT,
              linked_object TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_contexts (
              context_id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              domain TEXT NOT NULL,
              task_kind TEXT NOT NULL,
              state TEXT NOT NULL,
              version INTEGER NOT NULL,
              effective_query TEXT NOT NULL,
              page_scope_json TEXT NOT NULL,
              task_scope_json TEXT NOT NULL,
              scope_history_json TEXT NOT NULL,
              predicate_json TEXT NOT NULL,
              temporal_json TEXT NOT NULL,
              evidence_refs_json TEXT NOT NULL,
              result_refs_json TEXT NOT NULL,
              decision_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              superseded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_context_active
              ON conversation_contexts(conversation_id, tenant_id, user_id, state, version DESC);
            CREATE TABLE IF NOT EXISTS plans (
              plan_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              intent TEXT NOT NULL,
              risk_level TEXT NOT NULL,
              status TEXT NOT NULL,
              slots TEXT NOT NULL,
              actions TEXT NOT NULL,
              validators TEXT NOT NULL,
              confirm_required INTEGER NOT NULL,
              summary TEXT NOT NULL,
              validation_result TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              confirmed_at TEXT,
              result TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
              subscription_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              name TEXT NOT NULL,
              app_id TEXT NOT NULL,
              app_version_id TEXT NOT NULL,
              capability_id TEXT NOT NULL,
              camera_ids TEXT NOT NULL,
              schedule TEXT NOT NULL,
              valid_from TEXT NOT NULL,
              valid_to TEXT NOT NULL,
              thresholds TEXT NOT NULL,
              dedupe_policy TEXT NOT NULL,
              status TEXT NOT NULL,
              source TEXT NOT NULL,
              plan_id TEXT,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_inspections (
              task_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              org_name TEXT NOT NULL,
              name TEXT NOT NULL,
              inspection_goal TEXT NOT NULL,
              camera_ids TEXT NOT NULL,
              camera_names TEXT NOT NULL,
              schedule TEXT NOT NULL,
              start_at TEXT NOT NULL,
              end_at TEXT NOT NULL,
              next_run_at TEXT,
              last_run_at TEXT,
              status TEXT NOT NULL,
              run_count INTEGER NOT NULL DEFAULT 0,
              anomaly_count INTEGER NOT NULL DEFAULT 0,
              uncertain_count INTEGER NOT NULL DEFAULT 0,
              thresholds TEXT NOT NULL,
              plan_id TEXT,
              batch_id TEXT,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inspection_runs (
              run_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              scheduled_at TEXT NOT NULL,
              started_at TEXT NOT NULL,
              completed_at TEXT,
              status TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 1,
              result_status TEXT,
              conclusion TEXT,
              confidence REAL,
              business_reason TEXT,
              observations TEXT NOT NULL,
              evidence_ids TEXT NOT NULL,
              anomaly_evidence_ids TEXT NOT NULL DEFAULT '[]',
              sku_matches_json TEXT NOT NULL DEFAULT '[]',
              model_version TEXT,
              error_message TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(task_id, scheduled_at)
            );
            CREATE TABLE IF NOT EXISTS scheduled_evidence (
              evidence_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              org_name TEXT NOT NULL,
              camera_id TEXT NOT NULL,
              camera_name TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              storage_path TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              access_token TEXT NOT NULL,
              byte_size INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS online_snapshot_evidence (
              evidence_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              camera_id TEXT NOT NULL,
              camera_name TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              storage_path TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              access_token TEXT NOT NULL,
              byte_size INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inspection_batches (
              batch_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              plan_id TEXT NOT NULL UNIQUE,
              conversation_id TEXT NOT NULL,
              intent TEXT NOT NULL,
              scope_snapshot TEXT NOT NULL,
              execution_mode TEXT NOT NULL,
              status TEXT NOT NULL,
              total_store_count INTEGER NOT NULL,
              success_store_count INTEGER NOT NULL DEFAULT 0,
              failed_store_count INTEGER NOT NULL DEFAULT 0,
              skipped_store_count INTEGER NOT NULL DEFAULT 0,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inspection_batch_items (
              item_id TEXT PRIMARY KEY,
              batch_id TEXT NOT NULL,
              store_id TEXT NOT NULL,
              store_name TEXT NOT NULL,
              camera_ids TEXT NOT NULL,
              camera_names TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL,
              failure_code TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              subscription_id TEXT,
              scheduled_task_id TEXT,
              run_ids TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(batch_id, store_id)
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_inspections(status, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_inspection_runs_task ON inspection_runs(task_id, scheduled_at DESC);
            CREATE INDEX IF NOT EXISTS idx_scheduled_evidence_run ON scheduled_evidence(run_id);
            CREATE INDEX IF NOT EXISTS idx_online_snapshot_evidence_tenant
              ON online_snapshot_evidence(tenant_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_inspection_batches_tenant
              ON inspection_batches(tenant_id, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_inspection_batch_items_batch
              ON inspection_batch_items(batch_id, status);
            CREATE TABLE IF NOT EXISTS tenant_integrations (
              integration_id TEXT PRIMARY KEY,
              tenant_code TEXT NOT NULL UNIQUE,
              tenant_name TEXT NOT NULL,
              app_key_masked TEXT NOT NULL,
              encrypted_credentials TEXT NOT NULL,
              credential_fingerprint TEXT NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              store_count INTEGER NOT NULL DEFAULT 0,
              last_synced_at TEXT,
              last_error TEXT,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS service_configs (
              config_key TEXT PRIMARY KEY,
              encrypted_value TEXT NOT NULL,
              public_metadata TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_search_usage (
              usage_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              conversation_id TEXT,
              provider TEXT NOT NULL,
              credential_fingerprint TEXT NOT NULL,
              operation TEXT NOT NULL,
              status TEXT NOT NULL,
              provider_request_id TEXT,
              credits_consumed INTEGER NOT NULL DEFAULT 0,
              provider_used_credits INTEGER,
              provider_credit_limit INTEGER,
              provider_remaining_credits INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_web_search_usage_credential_time
              ON web_search_usage(credential_fingerprint, created_at DESC);
            CREATE TABLE IF NOT EXISTS tenant_integration_stores (
              integration_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              parent_id TEXT,
              name TEXT NOT NULL,
              org_type TEXT NOT NULL,
              status TEXT NOT NULL,
              camera_count INTEGER,
              synced_at TEXT NOT NULL,
              PRIMARY KEY(integration_id, org_id)
            );
            CREATE INDEX IF NOT EXISTS idx_integration_stores_integration ON tenant_integration_stores(integration_id, name);
            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              org_id TEXT NOT NULL,
              camera_id TEXT NOT NULL,
              subscription_id TEXT,
              task_id TEXT,
              event_type TEXT NOT NULL,
              severity TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT NOT NULL,
              duration_seconds INTEGER NOT NULL,
              confidence REAL NOT NULL,
              status TEXT NOT NULL,
              evidence_ids TEXT NOT NULL,
              model_version TEXT NOT NULL,
              rule_snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
              evidence_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              type TEXT NOT NULL,
              storage_url TEXT NOT NULL,
              thumbnail_url TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              bbox TEXT NOT NULL,
              metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback (
              feedback_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              feedback_type TEXT NOT NULL,
              reason TEXT NOT NULL,
              description TEXT,
              evidence_id TEXT,
              created_by TEXT NOT NULL,
              status TEXT NOT NULL,
              badcase_id TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
              audit_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              action TEXT NOT NULL,
              object_type TEXT NOT NULL,
              object_id TEXT NOT NULL,
              before_json TEXT,
              after_json TEXT,
              source TEXT NOT NULL,
              plan_id TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idempotency_keys (
              idempotency_key TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              response_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analytics_queries (
              query_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              question TEXT NOT NULL,
              scope TEXT NOT NULL,
              result TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_manifest_imports (
              manifest_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              name TEXT NOT NULL,
              label TEXT NOT NULL,
              version TEXT NOT NULL,
              status TEXT NOT NULL,
              risk_level TEXT NOT NULL,
              confirm_required INTEGER NOT NULL,
              manifest_json TEXT NOT NULL,
              validation_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_manifest_imports_tenant
              ON agent_manifest_imports(tenant_id, kind, status);
            CREATE TABLE IF NOT EXISTS agent_memories (
              memory_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              scope TEXT NOT NULL,
              category TEXT NOT NULL,
              memory_key TEXT NOT NULL,
              memory_value TEXT NOT NULL,
              aliases_json TEXT NOT NULL,
              confidence REAL NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_memories_tenant
              ON agent_memories(tenant_id, status, category);
            CREATE TABLE IF NOT EXISTS agent_knowledge_items (
              knowledge_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              title TEXT NOT NULL,
              sku TEXT,
              knowledge_type TEXT NOT NULL,
              modality TEXT NOT NULL,
              content_text TEXT NOT NULL,
              asset_url TEXT,
              asset_urls_json TEXT NOT NULL DEFAULT '[]',
              asset_metadata_json TEXT NOT NULL DEFAULT '[]',
              tags_json TEXT NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_knowledge_tenant
              ON agent_knowledge_items(tenant_id, status, knowledge_type);
            """
        )
        ensure_schema_extensions(conn)
        ensure_platform_research_source_policies(conn)
        if one(conn, "SELECT user_id FROM users LIMIT 1"):
            ensure_demo_extensions(conn)
            refresh_demo_fixture_dates(conn)
            return
        seed_data(conn)
        ensure_demo_extensions(conn)
        refresh_demo_fixture_dates(conn)


def ensure_schema_extensions(conn: sqlite3.Connection):
    """Apply additive migrations required by newer local builds."""
    scheduled_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(scheduled_inspections)")
    }
    if "batch_id" not in scheduled_columns:
        conn.execute("ALTER TABLE scheduled_inspections ADD COLUMN batch_id TEXT")
    inspection_run_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(inspection_runs)")
    }
    if "anomaly_evidence_ids" not in inspection_run_columns:
        conn.execute(
            "ALTER TABLE inspection_runs ADD COLUMN anomaly_evidence_ids TEXT NOT NULL DEFAULT '[]'"
        )
    if "trace_json" not in inspection_run_columns:
        conn.execute(
            "ALTER TABLE inspection_runs ADD COLUMN trace_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "sku_matches_json" not in inspection_run_columns:
        conn.execute(
            "ALTER TABLE inspection_runs ADD COLUMN sku_matches_json TEXT NOT NULL DEFAULT '[]'"
        )
    knowledge_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(agent_knowledge_items)")
    }
    if "sku" not in knowledge_columns:
        conn.execute("ALTER TABLE agent_knowledge_items ADD COLUMN sku TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_contexts (
          context_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          task_kind TEXT NOT NULL,
          state TEXT NOT NULL,
          version INTEGER NOT NULL,
          effective_query TEXT NOT NULL,
          page_scope_json TEXT NOT NULL,
          task_scope_json TEXT NOT NULL,
          scope_history_json TEXT NOT NULL,
          predicate_json TEXT NOT NULL,
          temporal_json TEXT NOT NULL,
          evidence_refs_json TEXT NOT NULL,
          result_refs_json TEXT NOT NULL,
          decision_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          superseded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_context_active
          ON conversation_contexts(conversation_id, tenant_id, user_id, state, version DESC);
        CREATE TABLE IF NOT EXISTS agent_memories (
          memory_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          scope TEXT NOT NULL,
          category TEXT NOT NULL,
          memory_key TEXT NOT NULL,
          memory_value TEXT NOT NULL,
          aliases_json TEXT NOT NULL,
          confidence REAL NOT NULL,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_memories_tenant
          ON agent_memories(tenant_id, status, category);
        CREATE TABLE IF NOT EXISTS agent_knowledge_items (
          knowledge_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          title TEXT NOT NULL,
          sku TEXT,
          knowledge_type TEXT NOT NULL,
          modality TEXT NOT NULL,
          content_text TEXT NOT NULL,
          asset_url TEXT,
          asset_urls_json TEXT NOT NULL DEFAULT '[]',
          asset_metadata_json TEXT NOT NULL DEFAULT '[]',
          tags_json TEXT NOT NULL,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_knowledge_tenant
          ON agent_knowledge_items(tenant_id, status, knowledge_type);
        CREATE TABLE IF NOT EXISTS inspection_batches (
          batch_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          plan_id TEXT NOT NULL UNIQUE,
          conversation_id TEXT NOT NULL,
          intent TEXT NOT NULL,
          scope_snapshot TEXT NOT NULL,
          execution_mode TEXT NOT NULL,
          status TEXT NOT NULL,
          total_store_count INTEGER NOT NULL,
          success_store_count INTEGER NOT NULL DEFAULT 0,
          failed_store_count INTEGER NOT NULL DEFAULT 0,
          skipped_store_count INTEGER NOT NULL DEFAULT 0,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inspection_batch_items (
          item_id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL,
          store_id TEXT NOT NULL,
          store_name TEXT NOT NULL,
          camera_ids TEXT NOT NULL,
          camera_names TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL,
          failure_code TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0,
          subscription_id TEXT,
          scheduled_task_id TEXT,
          run_ids TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(batch_id, store_id)
        );
        CREATE INDEX IF NOT EXISTS idx_inspection_batches_tenant
          ON inspection_batches(tenant_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inspection_batch_items_batch
          ON inspection_batch_items(batch_id, status);
        CREATE TABLE IF NOT EXISTS catalog_versions (
          version_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          version_number INTEGER NOT NULL,
          state TEXT NOT NULL,
          content_hash TEXT,
          change_summary TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL,
          approved_by TEXT,
          created_at TEXT NOT NULL,
          approved_at TEXT,
          published_at TEXT,
          retired_at TEXT,
          UNIQUE(tenant_id, version_number)
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_versions_tenant
          ON catalog_versions(tenant_id, state, version_number DESC);
        CREATE TABLE IF NOT EXISTS catalog_skus (
          sku_item_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          catalog_version_id TEXT NOT NULL,
          sku_id TEXT NOT NULL,
          canonical_name TEXT NOT NULL,
          display_name TEXT NOT NULL,
          brand TEXT NOT NULL DEFAULT '',
          family_id TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '',
          variant_attributes_json TEXT NOT NULL DEFAULT '{}',
          aliases_json TEXT NOT NULL DEFAULT '[]',
          external_codes_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL,
          effective_from TEXT,
          effective_to TEXT,
          etag TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(tenant_id, catalog_version_id, sku_id)
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_skus_tenant
          ON catalog_skus(tenant_id, catalog_version_id, status, sku_id);
        CREATE TABLE IF NOT EXISTS domain_profiles (
          profile_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          name TEXT NOT NULL,
          domain TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '',
          capture_mode TEXT NOT NULL,
          identity_policy_json TEXT NOT NULL,
          quality_bundle_json TEXT NOT NULL,
          version INTEGER NOT NULL,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          approved_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_domain_profiles_tenant
          ON domain_profiles(tenant_id, status, domain, version DESC);
        CREATE TABLE IF NOT EXISTS calibration_profiles (
          calibration_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          camera_id TEXT NOT NULL,
          version TEXT NOT NULL,
          roi_json TEXT NOT NULL,
          health_state TEXT NOT NULL,
          effective_from TEXT NOT NULL,
          expires_at TEXT,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          approved_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(tenant_id, camera_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_calibrations_tenant
          ON calibration_profiles(tenant_id, camera_id, status, effective_from);
        CREATE TABLE IF NOT EXISTS reference_assets (
          asset_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          sku_id TEXT NOT NULL,
          catalog_version_id TEXT NOT NULL,
          knowledge_id TEXT,
          asset_url TEXT NOT NULL,
          view_tag TEXT NOT NULL DEFAULT '',
          camera_profile TEXT NOT NULL DEFAULT '',
          source_type TEXT NOT NULL,
          feature_version TEXT NOT NULL DEFAULT '',
          approval_status TEXT NOT NULL,
          effective_at TEXT NOT NULL,
          expires_at TEXT,
          created_by TEXT NOT NULL,
          approved_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reference_assets_tenant
          ON reference_assets(tenant_id, catalog_version_id, sku_id, approval_status);
        CREATE TABLE IF NOT EXISTS display_slots (
          slot_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          org_id TEXT NOT NULL,
          camera_id TEXT NOT NULL,
          domain_profile_id TEXT NOT NULL,
          catalog_version_id TEXT NOT NULL,
          calibration_version TEXT NOT NULL,
          zone_polygon_json TEXT NOT NULL,
          expected_skus_json TEXT NOT NULL,
          expected_count INTEGER NOT NULL,
          min_valid_frames INTEGER NOT NULL,
          quality_threshold REAL NOT NULL,
          min_roi_coverage REAL NOT NULL,
          max_occlusion REAL NOT NULL,
          automation_enabled INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL,
          effective_to TEXT,
          created_by TEXT NOT NULL,
          approved_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_display_slots_tenant
          ON display_slots(tenant_id, camera_id, status, effective_from);
        CREATE TABLE IF NOT EXISTS comparison_sessions (
          session_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          created_by TEXT NOT NULL,
          camera_id TEXT NOT NULL,
          capture_mode TEXT NOT NULL,
          domain_profile_id TEXT NOT NULL,
          domain_profile_version INTEGER NOT NULL,
          catalog_version_id TEXT NOT NULL,
          calibration_version TEXT NOT NULL,
          display_slot_ids_json TEXT NOT NULL,
          evidence_refs_json TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          status TEXT NOT NULL,
          run_snapshot_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(tenant_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_comparison_sessions_tenant
          ON comparison_sessions(tenant_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS comparison_frames (
          frame_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          evidence_id TEXT,
          evidence_sha256 TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          frame_state TEXT NOT NULL,
          quality_score REAL,
          roi_coverage REAL,
          occlusion_ratio REAL,
          camera_health TEXT,
          detections_json TEXT NOT NULL,
          object_evidence_json TEXT NOT NULL,
          reason_codes_json TEXT NOT NULL,
          run_snapshot_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(session_id, evidence_sha256, captured_at)
        );
        CREATE INDEX IF NOT EXISTS idx_comparison_frames_session
          ON comparison_frames(session_id, captured_at);
        CREATE TABLE IF NOT EXISTS comparison_slot_decisions (
          decision_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          slot_id TEXT NOT NULL,
          state TEXT NOT NULL,
          calibrated_probability REAL,
          observed_count INTEGER NOT NULL,
          valid_frame_count INTEGER NOT NULL,
          reason_codes_json TEXT NOT NULL,
          evidence_refs_json TEXT NOT NULL,
          run_snapshot_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(session_id, slot_id)
        );
        CREATE INDEX IF NOT EXISTS idx_comparison_decisions_tenant
          ON comparison_slot_decisions(tenant_id, state, created_at DESC);
        CREATE TABLE IF NOT EXISTS comparison_reviews (
          review_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          decision TEXT NOT NULL,
          chosen_identity TEXT,
          reason TEXT NOT NULL,
          evidence_refs_json TEXT NOT NULL,
          training_eligibility INTEGER NOT NULL DEFAULT 0,
          operator_id TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_comparison_reviews_decision
          ON comparison_reviews(decision_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS agent_feature_flags (
          tenant_id TEXT NOT NULL,
          flag TEXT NOT NULL,
          enabled INTEGER NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(tenant_id, flag)
        );
        CREATE TABLE IF NOT EXISTS agent_gate_decisions (
          decision_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL,
          workflow_id TEXT,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          action TEXT NOT NULL,
          gate TEXT NOT NULL,
          decision TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          allowed_scope_json TEXT NOT NULL,
          input_summary_hash TEXT NOT NULL,
          policy_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_gate_decisions_scope
          ON agent_gate_decisions(tenant_id, user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS agent_workflow_runs (
          workflow_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          conversation_id TEXT,
          kind TEXT NOT NULL,
          status TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          output_hash TEXT,
          research_run_id TEXT,
          office_job_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_workflows_scope
          ON agent_workflow_runs(tenant_id, user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS agent_feedback (
          feedback_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          workflow_id TEXT,
          domain TEXT NOT NULL,
          resource_id TEXT NOT NULL,
          feedback_type TEXT NOT NULL,
          reason TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_feedback_scope
          ON agent_feedback(tenant_id, domain, created_at DESC);
        CREATE TABLE IF NOT EXISTS agent_runtime_limits (
          tenant_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          window_seconds INTEGER NOT NULL,
          max_requests INTEGER NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(tenant_id, domain)
        );
        CREATE TABLE IF NOT EXISTS agent_runtime_usage (
          usage_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runtime_usage_scope
          ON agent_runtime_usage(tenant_id, user_id, domain, created_at DESC);
        CREATE TABLE IF NOT EXISTS open_research_runs (
          run_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          conversation_id TEXT,
          workflow_id TEXT,
          status TEXT NOT NULL,
          question_hash TEXT NOT NULL,
          fact_intent TEXT NOT NULL DEFAULT 'EVERGREEN_FACT',
          quality_status TEXT NOT NULL DEFAULT 'QUERYING',
          territory_assumption TEXT,
          retention_class TEXT NOT NULL DEFAULT 'NO_MEMORY',
          force_fresh INTEGER NOT NULL DEFAULT 0,
          rewrite_json TEXT NOT NULL,
          plan_json TEXT NOT NULL,
          answer_json TEXT NOT NULL,
          provider_requests_json TEXT NOT NULL DEFAULT '[]',
          as_of TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_runs_scope
          ON open_research_runs(tenant_id, user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS open_research_queries (
          query_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          query_hash TEXT NOT NULL,
          purpose TEXT NOT NULL,
          freshness TEXT NOT NULL,
          topic TEXT NOT NULL,
          provider TEXT,
          provider_request_id TEXT,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_queries_run
          ON open_research_queries(run_id, created_at);
        CREATE TABLE IF NOT EXISTS open_research_provider_usage (
          usage_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          provider TEXT NOT NULL,
          latency_ms INTEGER NOT NULL,
          credits INTEGER NOT NULL DEFAULT 0,
          outcome TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_provider_usage_scope
          ON open_research_provider_usage(tenant_id, provider, created_at DESC);
        CREATE TABLE IF NOT EXISTS open_research_evidence (
          evidence_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          title TEXT NOT NULL,
          canonical_url TEXT NOT NULL,
          publisher TEXT NOT NULL,
          published_at TEXT,
          fetched_at TEXT NOT NULL,
          source_tier TEXT NOT NULL,
          source_policy_id TEXT,
          source_reputation REAL NOT NULL DEFAULT 0.55,
          relevance_score REAL NOT NULL DEFAULT 0,
          freshness_score REAL NOT NULL DEFAULT 0,
          semantic_score REAL NOT NULL DEFAULT 0,
          evidence_confidence REAL NOT NULL DEFAULT 0,
          evidence_type TEXT NOT NULL DEFAULT 'DIRECT_SERP_EVIDENCE',
          detail_fetch_status TEXT,
          extraction_locator_type TEXT,
          fact_fragment_hash TEXT,
          detail_rejection_reason TEXT,
          snippet_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(evidence_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_evidence_run
          ON open_research_evidence(run_id, created_at);
        CREATE TABLE IF NOT EXISTS open_research_claims (
          claim_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          subject TEXT NOT NULL DEFAULT '',
          predicate TEXT NOT NULL DEFAULT '',
          claim_value TEXT,
          territory TEXT,
          event_state TEXT,
          evidence_ids_json TEXT NOT NULL,
          claim_status TEXT NOT NULL,
          confidence REAL NOT NULL,
          claim_hash TEXT NOT NULL,
          claim_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_claims_run
          ON open_research_claims(run_id, created_at);
        CREATE TABLE IF NOT EXISTS research_source_policies (
          policy_id TEXT PRIMARY KEY,
          domain TEXT NOT NULL UNIQUE,
          match_subdomains INTEGER NOT NULL DEFAULT 0,
          tier TEXT NOT NULL,
          allowed_fact_types_json TEXT NOT NULL,
          reputation_weight REAL NOT NULL DEFAULT 0.55,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          reviewed_by TEXT,
          reviewed_at TEXT,
          expires_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_research_source_policies_active
          ON research_source_policies(status, expires_at, domain);
        CREATE TABLE IF NOT EXISTS open_research_memory_index (
          memory_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          topic TEXT NOT NULL,
          memory_json TEXT NOT NULL,
          retention_class TEXT NOT NULL DEFAULT 'NO_MEMORY',
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_memory_scope
          ON open_research_memory_index(tenant_id, user_id, topic, status, expires_at DESC);
        CREATE TABLE IF NOT EXISTS open_research_history_records (
          run_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          user_message_id TEXT NOT NULL,
          assistant_message_id TEXT NOT NULL,
          fact_intent TEXT NOT NULL,
          quality_status TEXT NOT NULL,
          retention_class TEXT NOT NULL,
          force_fresh INTEGER NOT NULL DEFAULT 0,
          completed_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_history_scope
          ON open_research_history_records(tenant_id, user_id, completed_at DESC, run_id DESC);
        CREATE TABLE IF NOT EXISTS open_research_entity_aliases (
          alias_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          alias_text TEXT NOT NULL,
          canonical_entity TEXT NOT NULL,
          confidence REAL NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(tenant_id, alias_text)
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_entity_aliases_scope
          ON open_research_entity_aliases(tenant_id, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS open_research_interactions (
          interaction_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          interaction_type TEXT NOT NULL,
          evidence_id TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_open_research_interactions_scope
          ON open_research_interactions(tenant_id, interaction_type, created_at DESC);
        CREATE TABLE IF NOT EXISTS research_briefs (
          brief_id TEXT PRIMARY KEY,
          producer_run_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          status TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          brief_json TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_research_briefs_scope
          ON research_briefs(tenant_id, user_id, producer_run_id);
        CREATE TABLE IF NOT EXISTS office_assets (
          asset_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          filename TEXT NOT NULL,
          extension TEXT NOT NULL,
          detected_mime TEXT NOT NULL,
          byte_size INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          storage_key TEXT NOT NULL,
          scan_status TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_office_assets_scope
          ON office_assets(tenant_id, user_id, status, expires_at DESC);
        CREATE TABLE IF NOT EXISTS office_extractions (
          extraction_id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          status TEXT NOT NULL,
          extraction_json TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_office_extractions_asset
          ON office_extractions(asset_id, tenant_id, user_id, status);
        CREATE TABLE IF NOT EXISTS office_jobs (
          job_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          conversation_id TEXT,
          workflow_id TEXT,
          status TEXT NOT NULL,
          stage TEXT NOT NULL,
          asset_ids_json TEXT NOT NULL,
          research_brief_id TEXT,
          template_id TEXT NOT NULL,
          title TEXT NOT NULL,
          spec_json TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          error_code TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          UNIQUE(tenant_id, user_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_office_jobs_scope
          ON office_jobs(tenant_id, user_id, status, created_at DESC);
        CREATE TABLE IF NOT EXISTS office_artifact_versions (
          version_id TEXT PRIMARY KEY,
          job_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          artifact_type TEXT NOT NULL,
          status TEXT NOT NULL,
          storage_key TEXT NOT NULL,
          preview_key TEXT NOT NULL,
          preview_png_key TEXT NOT NULL DEFAULT '',
          sha256 TEXT NOT NULL,
          source_refs_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_office_artifacts_scope
          ON office_artifact_versions(tenant_id, user_id, status, expires_at DESC);
        """
    )
    knowledge_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(agent_knowledge_items)")
    }
    if "asset_urls_json" not in knowledge_columns:
        conn.execute(
            "ALTER TABLE agent_knowledge_items ADD COLUMN asset_urls_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "asset_metadata_json" not in knowledge_columns:
        conn.execute(
            "ALTER TABLE agent_knowledge_items ADD COLUMN asset_metadata_json TEXT NOT NULL DEFAULT '[]'"
        )
    artifact_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(office_artifact_versions)")
    }
    if artifact_columns and "preview_png_key" not in artifact_columns:
        conn.execute("ALTER TABLE office_artifact_versions ADD COLUMN preview_png_key TEXT NOT NULL DEFAULT ''")
    research_run_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(open_research_runs)")
    }
    if research_run_columns and "fact_intent" not in research_run_columns:
        conn.execute("ALTER TABLE open_research_runs ADD COLUMN fact_intent TEXT NOT NULL DEFAULT 'EVERGREEN_FACT'")
    if research_run_columns and "quality_status" not in research_run_columns:
        conn.execute("ALTER TABLE open_research_runs ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'QUERYING'")
    if research_run_columns and "territory_assumption" not in research_run_columns:
        conn.execute("ALTER TABLE open_research_runs ADD COLUMN territory_assumption TEXT")
    if research_run_columns and "retention_class" not in research_run_columns:
        conn.execute("ALTER TABLE open_research_runs ADD COLUMN retention_class TEXT NOT NULL DEFAULT 'NO_MEMORY'")
    if research_run_columns and "force_fresh" not in research_run_columns:
        conn.execute("ALTER TABLE open_research_runs ADD COLUMN force_fresh INTEGER NOT NULL DEFAULT 0")
    research_evidence_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(open_research_evidence)")
    }
    if research_evidence_columns and "source_policy_id" not in research_evidence_columns:
        conn.execute("ALTER TABLE open_research_evidence ADD COLUMN source_policy_id TEXT")
    for column, ddl in {
        "evidence_type": "TEXT NOT NULL DEFAULT 'DIRECT_SERP_EVIDENCE'",
        "detail_fetch_status": "TEXT",
        "extraction_locator_type": "TEXT",
        "fact_fragment_hash": "TEXT",
        "detail_rejection_reason": "TEXT",
        "source_reputation": "REAL NOT NULL DEFAULT 0.55",
        "relevance_score": "REAL NOT NULL DEFAULT 0",
        "freshness_score": "REAL NOT NULL DEFAULT 0",
        "semantic_score": "REAL NOT NULL DEFAULT 0",
        "evidence_confidence": "REAL NOT NULL DEFAULT 0",
    }.items():
        if research_evidence_columns and column not in research_evidence_columns:
            conn.execute(f"ALTER TABLE open_research_evidence ADD COLUMN {column} {ddl}")
    source_policy_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(research_source_policies)")
    }
    if source_policy_columns and "reputation_weight" not in source_policy_columns:
        conn.execute("ALTER TABLE research_source_policies ADD COLUMN reputation_weight REAL NOT NULL DEFAULT 0.55")
    research_memory_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(open_research_memory_index)")
    }
    if research_memory_columns and "retention_class" not in research_memory_columns:
        conn.execute("ALTER TABLE open_research_memory_index ADD COLUMN retention_class TEXT NOT NULL DEFAULT 'NO_MEMORY'")
    research_claim_columns = {
        item["name"] for item in rows(conn, "PRAGMA table_info(open_research_claims)")
    }
    for column, ddl in {
        "subject": "TEXT NOT NULL DEFAULT ''",
        "predicate": "TEXT NOT NULL DEFAULT ''",
        "claim_value": "TEXT",
        "territory": "TEXT",
        "event_state": "TEXT",
        "claim_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if research_claim_columns and column not in research_claim_columns:
            conn.execute(f"ALTER TABLE open_research_claims ADD COLUMN {column} {ddl}")


def ensure_platform_research_source_policies(conn: sqlite3.Connection):
    """Seed auditable reputation boosts, never a result-admission list.

    An absent host still participates in evidence evaluation at the neutral
    reputation baseline; these entries only increase/decrease its prior.
    """
    timestamp = now_iso()
    reviewed_publishers = (
        "news.cn",
        "xinhuanet.com",
        "people.com.cn",
        "cctv.com",
        "chinanews.com.cn",
        "chinafilmnews.cn",
        "thepaper.cn",
        "yicai.com",
        "ctdsb.net",
    )
    for domain in reviewed_publishers:
        upsert_source_policy(
            conn,
            policy_id=f"rsp_seed_{domain.replace('.', '_')}",
            domain=domain,
            match_subdomains=True,
            tier="PUBLISHER",
            allowed_fact_types=["*"],
            reputation_weight=0.92,
            status="ACTIVE",
            reviewed_by="system_seed",
            reviewed_at=timestamp,
            expires_at=None,
            created_by="system_seed",
            now=timestamp,
        )
    for domain, weight in (
        ("zh.wikipedia.org", 0.76),
        ("baike.baidu.com", 0.74),
        ("1905.com", 0.78),
    ):
        upsert_source_policy(
            conn,
            policy_id=f"rsp_seed_{domain.replace('.', '_')}",
            domain=domain,
            match_subdomains=True,
            tier="SECONDARY",
            allowed_fact_types=["*"],
            reputation_weight=weight,
            status="ACTIVE",
            reviewed_by="system_seed",
            reviewed_at=timestamp,
            expires_at=None,
            created_by="system_seed",
            now=timestamp,
        )


def seed_data(conn: sqlite3.Connection):
    users = [
        ("u_admin", "租户管理员", "tenant_admin", "tenant_jihu", ["*"]),
        ("u_system", "系统管理员", "system_admin", "tenant_jihu", ["*"]),
        ("u_region", "华东区域运营", "region_operator", "tenant_jihu", ["org_hd"]),
        ("u_store", "广州店负责人", "store_manager", "tenant_jihu", ["org_gz"]),
        ("u_frontline", "一线处理人员", "frontline", "tenant_jihu", ["org_gz"]),
    ]
    conn.executemany(
        "INSERT INTO users VALUES (?,?,?,?,?)",
        [(u, n, r, t, json_dumps(orgs)) for u, n, r, t, orgs in users],
    )
    orgs = [
        ("org_root", "tenant_jihu", None, "极狐汽车", "tenant"),
        ("org_hd", "tenant_jihu", "org_root", "华东区", "region"),
        ("org_hn", "tenant_jihu", "org_root", "华南区", "region"),
        ("org_gz", "tenant_jihu", "org_hd", "广州悦汇城", "store"),
        ("org_hz1", "tenant_jihu", "org_hd", "杭州大悦城", "store"),
        ("org_hz2", "tenant_jihu", "org_hd", "杭州西湖店", "store"),
        ("org_sh", "tenant_jihu", "org_hd", "上海旗舰店", "store"),
        ("org_sz_store", "tenant_jihu", "org_hn", "深圳前海店", "store"),
    ]
    conn.executemany("INSERT INTO orgs VALUES (?,?,?,?,?)", orgs)
    cameras = [
        ("cam_gz_gate", "tenant_jihu", "org_gz", "广州悦汇城-门口-01", "门口", "海康", "GB28181", "ONLINE", "/static/evidence/ev-10231.svg", "2026-06-24T09:32:00+08:00", "CALIBRATED"),
        ("cam_gz_cashier", "tenant_jihu", "org_gz", "广州悦汇城-收银台-01", "收银台", "大华", "RTSP", "ONLINE", "/static/evidence/ev-10232.svg", "2026-06-24T09:31:00+08:00", "CALIBRATED"),
        ("cam_gz_fire", "tenant_jihu", "org_gz", "广州悦汇城-消防通道-01", "消防通道", "海康", "RTSP", "OFFLINE", "/static/evidence/ev-10234.svg", "2026-06-23T20:12:00+08:00", "UNCALIBRATED"),
        ("cam_hz1_gate", "tenant_jihu", "org_hz1", "杭州大悦城-门口-01", "门口", "宇视", "GB28181", "ONLINE", "/static/evidence/ev-hz1.svg", "2026-06-24T09:20:00+08:00", "CALIBRATED"),
        ("cam_hz2_gate", "tenant_jihu", "org_hz2", "杭州西湖店-门口-01", "门口", "宇视", "GB28181", "ONLINE", "/static/evidence/ev-hz2.svg", "2026-06-24T09:20:00+08:00", "CALIBRATED"),
        ("cam_sh_gate", "tenant_jihu", "org_sh", "上海旗舰店-门口-01", "门口", "海康", "RTSP", "ONLINE", "/static/evidence/ev-sh.svg", "2026-06-24T09:25:00+08:00", "CALIBRATED"),
        ("cam_sz_gate", "tenant_jihu", "org_sz_store", "深圳前海店-门口-01", "门口", "海康", "RTSP", "ONLINE", "/static/evidence/ev-sz.svg", "2026-06-24T09:25:00+08:00", "CALIBRATED"),
    ]
    conn.executemany("INSERT INTO cameras VALUES (?,?,?,?,?,?,?,?,?,?,?)", cameras)
    capabilities = [
        ("cap_leave", "app_leave_001", "appv_leave_1_3_0", "离岗检测", ["离岗", "空岗", "脱岗", "无人值守"], "LEAVE_POST", 1, 1, "ACTIVE", "门店/展厅岗位", "video_frame", "v1.3.0", {"duration_seconds": 300, "confidence": 0.82}),
        ("cap_smoke", "app_smoke_001", "appv_smoke_1_1_2", "抽烟检测", ["抽烟", "吸烟", "烟火"], "SMOKING", 0, 1, "ACTIVE", "门店/园区禁烟", "video_frame", "v1.1.2", {"confidence": 0.80}),
        ("cap_fire_lane", "app_fire_lane_001", "appv_fire_lane_2_0_1", "消防通道占用", ["消防通道", "占道", "通道占用"], "FIRE_LANE_BLOCKED", 1, 0, "ACTIVE", "消防通道", "video_frame", "v2.0.1", {"duration_seconds": 600, "confidence": 0.85}),
        ("cap_reception", "app_reception_001", "appv_reception_1_3_2", "进店无人接待检测", ["迎宾", "接待", "无人招呼", "没人管"], "RECEPTION_ABSENT", 1, 1, "ACTIVE", "汽车展厅/门店", "video_frame", "v1.3.2", {"no_reception_seconds": 60, "confidence": 0.80}),
        (VISUAL_COMPLIANCE_CAPABILITY_ID, "app_visual_compliance_001", "appv_visual_compliance_1_0_0", VISUAL_COMPLIANCE_NAME, list(VISUAL_COMPLIANCE_ALIASES), VISUAL_COMPLIANCE_EVENT_TYPE, 0, 1, "ACTIVE", "连锁门店/汽车展厅/手机门店", "video_frame+vlm", "v1.0.0", {"confidence": 0.80, "low_confidence_to_pending": True, "require_marked_anomaly_image": True}),
    ]
    conn.executemany(
        "INSERT INTO capabilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(cid, aid, avid, name, json_dumps(aliases), et, cr, aff, status, scene, ds, ver, json_dumps(th)) for cid, aid, avid, name, aliases, et, cr, aff, status, scene, ds, ver, th in capabilities],
    )
    events = [
        ("EV-10231", "org_gz", "cam_gz_gate", "LEAVE_POST", "IMPORTANT", "2026-06-23T10:05:00+08:00", "2026-06-23T10:12:00+08:00", 420, 0.91, "PENDING_CONFIRM", ["EVD-10231"], "leave-detector-v1.3.0", {"threshold_seconds": 300, "dedupe_minutes": 10}),
        ("EV-10232", "org_gz", "cam_gz_cashier", "LEAVE_POST", "NORMAL", "2026-06-23T14:20:00+08:00", "2026-06-23T14:26:00+08:00", 360, 0.88, "TRUE_POSITIVE", ["EVD-10232"], "leave-detector-v1.3.0", {"threshold_seconds": 300, "dedupe_minutes": 10}),
        ("EV-10233", "org_gz", "cam_gz_gate", "LEAVE_POST", "NORMAL", "2026-06-23T18:10:00+08:00", "2026-06-23T18:14:20+08:00", 260, 0.77, "PENDING_CONFIRM", ["EVD-10233"], "leave-detector-v1.3.0", {"threshold_seconds": 300, "dedupe_minutes": 10}),
        ("EV-10234", "org_gz", "cam_gz_fire", "FIRE_LANE_BLOCKED", "IMPORTANT", "2026-06-23T16:30:00+08:00", "2026-06-23T17:03:00+08:00", 1980, 0.93, "PENDING_CONFIRM", ["EVD-10234"], "fire-lane-v2.0.1", {"threshold_seconds": 600}),
        ("EV-SM-GZ-1", "org_gz", "cam_gz_gate", "SMOKING", "NORMAL", "2026-06-17T11:00:00+08:00", "2026-06-17T11:01:00+08:00", 60, 0.84, "FALSE_POSITIVE", ["EVD-SM-GZ-1"], "smoke-v1.1.2", {"confidence": 0.80}),
        ("EV-SM-HZ-1", "org_hz1", "cam_hz1_gate", "SMOKING", "IMPORTANT", "2026-06-18T12:00:00+08:00", "2026-06-18T12:01:00+08:00", 60, 0.89, "TRUE_POSITIVE", ["EVD-SM-HZ-1"], "smoke-v1.1.2", {"confidence": 0.80}),
        ("EV-SM-HZ-2", "org_hz1", "cam_hz1_gate", "SMOKING", "NORMAL", "2026-06-19T15:00:00+08:00", "2026-06-19T15:01:00+08:00", 60, 0.82, "PENDING_CONFIRM", ["EVD-SM-HZ-2"], "smoke-v1.1.2", {"confidence": 0.80}),
        ("EV-SM-SH-1", "org_sh", "cam_sh_gate", "SMOKING", "IMPORTANT", "2026-06-20T15:00:00+08:00", "2026-06-20T15:01:00+08:00", 60, 0.92, "TRUE_POSITIVE", ["EVD-SM-SH-1"], "smoke-v1.1.2", {"confidence": 0.80}),
        ("EV-SM-SH-2", "org_sh", "cam_sh_gate", "SMOKING", "NORMAL", "2026-06-21T17:00:00+08:00", "2026-06-21T17:01:00+08:00", 60, 0.87, "PENDING_CONFIRM", ["EVD-SM-SH-2"], "smoke-v1.1.2", {"confidence": 0.80}),
        ("EV-SM-SZ-1", "org_sz_store", "cam_sz_gate", "SMOKING", "NORMAL", "2026-06-23T13:00:00+08:00", "2026-06-23T13:01:00+08:00", 60, 0.86, "PENDING_CONFIRM", ["EVD-SM-SZ-1"], "smoke-v1.1.2", {"confidence": 0.80}),
    ]
    for event_id, org_id, camera_id, event_type, severity, started, ended, duration, confidence, status, evidence_ids, model_version, rule in events:
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                "tenant_jihu",
                org_id,
                camera_id,
                None,
                "task_seed",
                event_type,
                severity,
                started,
                ended,
                duration,
                confidence,
                status,
                json_dumps(evidence_ids),
                model_version,
                json_dumps(rule),
            ),
        )
        for evidence_id in evidence_ids:
            conn.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    event_id,
                    "IMAGE",
                    f"/static/evidence/{event_id.lower()}.svg",
                    f"/static/evidence/{event_id.lower()}.svg",
                    started,
                    json_dumps({"x": 0.18, "y": 0.24, "w": 0.42, "h": 0.38}),
                    json_dumps({"model_output": event_type, "frame_no": 128, "redacted": True}),
                ),
            )
    log_audit(conn, "u_admin", "tenant_jihu", "system.seed", "dataset", "seed", None, {"fixtures": "P0 demo data"}, "system", None)


def ensure_demo_extensions(conn: sqlite3.Connection):
    """Add demo fixtures introduced after the first seed without resetting data."""
    # Local demo tenant only: production tenants are fail-closed until an
    # administrator explicitly enables each feature through the flag API.
    for _flag, _enabled in {
        "open_research_enabled": True,
        "office_enabled": True,
        "research_to_office_enabled": True,
        "office_to_research_egress_enabled": False,
        "office_external_share_enabled": False,
        "office_model_processing_enabled": False,
    }.items():
        conn.execute(
            """INSERT OR IGNORE INTO agent_feature_flags(tenant_id, flag, enabled, updated_at)
               VALUES('tenant_jihu', ?, ?, ?)""",
            (_flag, 1 if _enabled else 0, now_iso()),
        )
    conn.execute(
        "INSERT OR IGNORE INTO users VALUES (?,?,?,?,?)",
        ("u_system", "系统管理员", "system_admin", "tenant_jihu", json_dumps(["*"])),
    )
    conn.execute(
        "INSERT OR IGNORE INTO capabilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            VISUAL_COMPLIANCE_CAPABILITY_ID,
            "app_visual_compliance_001",
            "appv_visual_compliance_1_0_0",
            VISUAL_COMPLIANCE_NAME,
            json_dumps(list(VISUAL_COMPLIANCE_ALIASES)),
            VISUAL_COMPLIANCE_EVENT_TYPE,
            0,
            1,
            "ACTIVE",
            "连锁门店/汽车展厅/手机门店",
            "video_frame+vlm",
            "v1.0.0",
            json_dumps({"confidence": 0.80, "low_confidence_to_pending": True, "require_marked_anomaly_image": True}),
        ),
    )
    orgs = [
        ("org_bj", "tenant_jihu", "org_root", "北京区域", "region"),
        ("org_bj_cbd", "tenant_jihu", "org_bj", "北京国贸店", "store"),
        ("org_bj_wk", "tenant_jihu", "org_bj", "北京五棵松店", "store"),
    ]
    conn.executemany("INSERT OR IGNORE INTO orgs VALUES (?,?,?,?,?)", orgs)
    cameras = [
        ("cam_bj_cbd_gate", "tenant_jihu", "org_bj_cbd", "北京国贸店-门口-01", "门口", "海康", "GB28181", "ONLINE", "/static/evidence/ev-bj-cbd.svg", "2026-06-24T09:40:00+08:00", "CALIBRATED"),
        ("cam_bj_cbd_cashier", "tenant_jihu", "org_bj_cbd", "北京国贸店-收银台-01", "收银台", "大华", "RTSP", "ONLINE", "/static/evidence/ev-bj-cbd-cashier.svg", "2026-06-24T09:41:00+08:00", "CALIBRATED"),
        ("cam_bj_wk_gate", "tenant_jihu", "org_bj_wk", "北京五棵松店-门口-01", "门口", "宇视", "GB28181", "ONLINE", "/static/evidence/ev-bj-wk.svg", "2026-06-24T09:38:00+08:00", "CALIBRATED"),
    ]
    conn.executemany("INSERT OR IGNORE INTO cameras VALUES (?,?,?,?,?,?,?,?,?,?,?)", cameras)


def refresh_demo_fixture_dates(conn: sqlite3.Connection):
    """Keep relative-date demo queries meaningful as the local demo ages."""
    schedule = {
        "EV-10231": (-1, "10:05:00", "10:12:00"),
        "EV-10232": (-1, "14:20:00", "14:26:00"),
        "EV-10233": (-1, "18:10:00", "18:14:20"),
        "EV-10234": (-1, "16:30:00", "17:03:00"),
        "EV-SM-GZ-1": (-6, "11:00:00", "11:01:00"),
        "EV-SM-HZ-1": (-5, "12:00:00", "12:01:00"),
        "EV-SM-HZ-2": (-4, "15:00:00", "15:01:00"),
        "EV-SM-SH-1": (-3, "15:00:00", "15:01:00"),
        "EV-SM-SH-2": (-2, "17:00:00", "17:01:00"),
        "EV-SM-SZ-1": (-1, "13:00:00", "13:01:00"),
    }
    for event_id, (day_offset, start_time, end_time) in schedule.items():
        event_date = CURRENT_DATE + timedelta(days=day_offset)
        started_at = f"{event_date.isoformat()}T{start_time}+08:00"
        ended_at = f"{event_date.isoformat()}T{end_time}+08:00"
        conn.execute("UPDATE events SET started_at=?, ended_at=? WHERE event_id=?", (started_at, ended_at, event_id))
        conn.execute("UPDATE evidence SET captured_at=? WHERE event_id=?", (started_at, event_id))


def user_from_request(handler: BaseHTTPRequestHandler, conn: sqlite3.Connection):
    # New-domain assets, research history and gate decisions are private.  A
    # demo fallback identity would turn a missing header into administrator
    # access, so all authenticated API routes require an explicit principal.
    user_id = str(handler.headers.get("X-User-Id") or "").strip()
    if not user_id:
        raise ApiError("AUTH_REQUIRED", HTTPStatus.UNAUTHORIZED)
    user = one(conn, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if not user:
        raise ApiError("AUTH_REQUIRED", HTTPStatus.UNAUTHORIZED)
    requested_tenant = str(handler.headers.get("X-Tenant-Code") or environment_tenant_code()).strip()
    # Once bootstrap has resolved the user's own local tenant, the SPA sends
    # it back on every request.  That tenant does not need a DeepVision
    # integration record: it is the user's existing, ACL-scoped tenant in the
    # local runtime.  Treating it as an external tenant made the second API
    # request fail closed with TENANT_SCOPE_DENIED, preventing all local P0
    # acceptance flows.  Any *different* requested tenant still goes through
    # the connected-integration check below and therefore cannot become an
    # implicit cross-tenant switch.
    is_own_local_tenant = bool(requested_tenant) and requested_tenant == str(user.get("tenant_id") or "")
    online = online_agent_for_tenant(conn, requested_tenant, required=bool(requested_tenant) and not is_own_local_tenant)
    if online:
        user = dict(user)
        user["tenant_id"] = online.tenant_code
        tenant_name = tenant_name_for_code(conn, online.tenant_code)
        role_labels = {
            "tenant_admin": "租户管理员",
            "region_operator": "区域运营",
            "store_manager": "门店负责人",
            "frontline": "一线处理人员",
            "delivery": "交付人员",
            "system_admin": "系统管理员",
        }
        user["online_tenant_name"] = tenant_name
        user["name"] = f"{tenant_name} {role_labels.get(user['role'], user['role'])}"
    return user


class ApiError(Exception):
    def __init__(self, code: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST, detail=None):
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


def role_can_create_subscription(role: str) -> bool:
    return role in {"tenant_admin", "region_operator", "delivery", "system_admin"}


def role_can_create_batch_inspection(role: str) -> bool:
    return role_can_create_subscription(role)


def role_can_feedback(role: str) -> bool:
    return role in {"tenant_admin", "region_operator", "store_manager", "frontline", "delivery", "system_admin"}


def role_can_view_audit(role: str) -> bool:
    return role in {"tenant_admin", "system_admin"}


def role_can_manage_integrations(role: str) -> bool:
    return role in {"tenant_admin", "system_admin"}


def role_can_manage_agent_catalog(role: str) -> bool:
    return role in {"tenant_admin", "system_admin"}


def tenant_feature_flag_settings(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Build the non-secret configuration and audit view for one tenant."""
    snapshot = feature_snapshot(conn, tenant_id)
    updated_rows = rows(
        conn,
        "SELECT flag, enabled, updated_at FROM agent_feature_flags WHERE tenant_id=?",
        (tenant_id,),
    )
    updated_at = {str(item["flag"]): item.get("updated_at") for item in updated_rows}
    definitions = []
    for definition in feature_flag_definitions():
        flag = definition["flag"]
        definitions.append({
            **definition,
            "enabled": bool(snapshot.get(flag, False)),
            "updated_at": updated_at.get(flag),
        })

    history = []
    for audit in rows(
        conn,
        """SELECT user_id, before_json, after_json, created_at
             FROM audit_logs
             WHERE tenant_id=? AND action='agent.feature_flags.update'
             ORDER BY created_at DESC, rowid DESC LIMIT 12""",
        (tenant_id,),
    ):
        before_payload = json_loads(audit.get("before_json"), {}) or {}
        after_payload = json_loads(audit.get("after_json"), {}) or {}
        before_flags = before_payload.get("flags", before_payload) if isinstance(before_payload, dict) else {}
        after_flags = after_payload.get("flags", after_payload) if isinstance(after_payload, dict) else {}
        changes = []
        for flag in DEFAULT_FEATURE_FLAGS:
            if bool(before_flags.get(flag, False)) != bool(after_flags.get(flag, False)):
                changes.append({"flag": flag, "enabled": bool(after_flags.get(flag, False))})
        if changes:
            history.append({
                "user_id": audit["user_id"],
                "created_at": audit["created_at"],
                "changes": changes,
                "forced_disabled": list(after_payload.get("forced_disabled") or []) if isinstance(after_payload, dict) else [],
            })
    return {"flags": snapshot, "definitions": definitions, "history": history}


CATALOG_STATES = {"DRAFT", "VALIDATING", "PENDING_APPROVAL", "PUBLISHED", "RETIRED"}
SKU_STATES = {"ACTIVE", "RETIRED"}
PROFILE_CAPTURE_MODES = {"FIXED_CAMERA", "GUIDED_HANDHELD", "ZONE_SCAN"}
REVIEW_DECISIONS = {"CONFIRMED", "OVERTURNED", "NEEDS_SITE_CHECK"}
SKU_ID_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._/-]{0,63}")
# 图片知识库中的受控标签既可以是规范 SKU，也允许门店已使用的中文型号、
# 色号或组合名称（例如“松果棕”“圣洁白+赤褐色”）。目录版本中的 sku_id
# 仍使用 SKU_ID_PATTERN，避免影响精确比对主数据的既有编码契约。
KNOWLEDGE_SKU_LABEL_PATTERN = re.compile(
    r"[A-Z0-9\u4E00-\u9FFF][A-Z0-9\u4E00-\u9FFF._/+()\-]{0,63}", re.IGNORECASE
)


def _comparison_access_required(user: dict):
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)


def _normalize_text_list(value, *, limit: int, item_limit: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        source = re.split(r"[,，\n]", value)
    elif isinstance(value, list):
        source = value
    else:
        raise ValueError("must be a list")
    normalized = []
    for item in source:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text[:item_limit])
    return normalized[:limit]


def _catalog_etag(normalized: dict) -> str:
    source = {
        key: normalized.get(key)
        for key in (
            "sku_id", "canonical_name", "display_name", "brand", "family_id", "category",
            "variant_attributes", "aliases", "external_codes", "status", "effective_from", "effective_to",
        )
    }
    return hashlib.sha256(json_dumps(source).encode("utf-8")).hexdigest()[:24]


def validate_catalog_sku_payload(payload: dict) -> dict:
    errors = []
    sku_id = str(payload.get("sku_id") or "").strip().upper()
    canonical_name = str(payload.get("canonical_name") or "").strip()
    display_name = str(payload.get("display_name") or canonical_name).strip()
    brand = str(payload.get("brand") or "").strip()
    family_id = str(payload.get("family_id") or payload.get("family") or "").strip()
    category = str(payload.get("category") or "").strip()
    status = str(payload.get("status") or "ACTIVE").strip().upper()
    try:
        aliases = _normalize_text_list(payload.get("aliases"), limit=30, item_limit=160)
    except ValueError:
        aliases = []
        errors.append("aliases must be a list")
    variant_attributes = payload.get("variant_attributes") or {}
    if not isinstance(variant_attributes, dict):
        errors.append("variant_attributes must be an object")
        variant_attributes = {}
    external_codes_raw = payload.get("external_codes") or []
    external_codes = []
    if not isinstance(external_codes_raw, list):
        errors.append("external_codes must be a list")
    else:
        code_keys = set()
        for item in external_codes_raw[:20]:
            if not isinstance(item, dict):
                errors.append("external_codes item must be an object")
                continue
            code_type = str(item.get("code_type") or item.get("type") or "").strip().upper()[:40]
            code_value = str(item.get("code_value") or item.get("value") or "").strip()[:160]
            source_system = str(item.get("source_system") or item.get("source") or "manual").strip()[:80]
            if not code_type or not code_value:
                errors.append("external code type and value are required")
                continue
            code_key = f"{code_type}:{code_value.casefold()}"
            if code_key in code_keys:
                errors.append("duplicate external code in SKU")
                continue
            code_keys.add(code_key)
            external_codes.append({"code_type": code_type, "code_value": code_value, "source_system": source_system})
    if not SKU_ID_PATTERN.fullmatch(sku_id):
        errors.append("sku_id format is invalid")
    if len(canonical_name) < 2:
        errors.append("canonical_name is required")
    if len(display_name) < 2:
        errors.append("display_name is required")
    if status not in SKU_STATES:
        errors.append("status must be ACTIVE or RETIRED")
    normalized = {
        "sku_id": sku_id,
        "canonical_name": canonical_name[:160],
        "display_name": display_name[:160],
        "brand": brand[:120],
        "family_id": family_id[:120],
        "category": category[:120],
        "variant_attributes": {str(key)[:80]: str(value)[:160] for key, value in variant_attributes.items()},
        "aliases": aliases,
        "external_codes": external_codes,
        "status": status,
        "effective_from": str(payload.get("effective_from") or "").strip()[:40] or None,
        "effective_to": str(payload.get("effective_to") or "").strip()[:40] or None,
    }
    normalized["etag"] = _catalog_etag(normalized)
    return {"ok": not errors, "errors": errors, "normalized": normalized}


def serialize_catalog_version(item: dict) -> dict:
    return {
        "version_id": item["version_id"],
        "tenant_id": item["tenant_id"],
        "version_number": item["version_number"],
        "state": item["state"],
        "content_hash": item.get("content_hash"),
        "change_summary": item.get("change_summary") or "",
        "created_by": item["created_by"],
        "approved_by": item.get("approved_by"),
        "created_at": item["created_at"],
        "approved_at": item.get("approved_at"),
        "published_at": item.get("published_at"),
        "retired_at": item.get("retired_at"),
    }


def serialize_catalog_sku(item: dict) -> dict:
    return {
        "sku_item_id": item["sku_item_id"],
        "catalog_version_id": item["catalog_version_id"],
        "sku_id": item["sku_id"],
        "canonical_name": item["canonical_name"],
        "display_name": item["display_name"],
        "brand": item.get("brand") or "",
        "family_id": item.get("family_id") or "",
        "category": item.get("category") or "",
        "variant_attributes": json_loads(item.get("variant_attributes_json"), {}),
        "aliases": json_loads(item.get("aliases_json"), []),
        "external_codes": json_loads(item.get("external_codes_json"), []),
        "status": item["status"],
        "effective_from": item.get("effective_from"),
        "effective_to": item.get("effective_to"),
        "etag": item["etag"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def _catalog_version(conn: sqlite3.Connection, user: dict, version_id: str) -> dict:
    item = one(conn, "SELECT * FROM catalog_versions WHERE version_id=? AND tenant_id=?", (version_id, user["tenant_id"]))
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    return item


def _catalog_content_hash(conn: sqlite3.Connection, tenant_id: str, version_id: str) -> str:
    values = [serialize_catalog_sku(item) for item in rows(
        conn,
        "SELECT * FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=? ORDER BY sku_id",
        (tenant_id, version_id),
    )]
    return hashlib.sha256(json_dumps(values).encode("utf-8")).hexdigest()


def create_catalog_version(conn: sqlite3.Connection, user: dict, payload: dict, *, clone_published: bool = False) -> dict:
    _comparison_access_required(user)
    latest = one(conn, "SELECT MAX(version_number) AS maximum FROM catalog_versions WHERE tenant_id=?", (user["tenant_id"],))
    version_number = int(latest.get("maximum") or 0) + 1
    version_id = f"catv_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO catalog_versions(version_id,tenant_id,version_number,state,content_hash,change_summary,created_by,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (version_id, user["tenant_id"], version_number, "DRAFT", None, str(payload.get("change_summary") or "")[:500], user["user_id"], timestamp),
    )
    if clone_published:
        published = one(
            conn,
            "SELECT version_id FROM catalog_versions WHERE tenant_id=? AND state='PUBLISHED' ORDER BY version_number DESC LIMIT 1",
            (user["tenant_id"],),
        )
        if published:
            source_items = rows(conn, "SELECT * FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=?", (user["tenant_id"], published["version_id"]))
            for source in source_items:
                copied = dict(source)
                copied["catalog_version_id"] = version_id
                copied["sku_item_id"] = f"skui_{uuid.uuid4().hex[:12]}"
                copied["etag"] = hashlib.sha256(f"{source['etag']}:{version_id}".encode("utf-8")).hexdigest()[:24]
                copied["created_by"] = user["user_id"]
                copied["created_at"] = timestamp
                copied["updated_at"] = timestamp
                conn.execute(
                    """INSERT INTO catalog_skus(sku_item_id,tenant_id,catalog_version_id,sku_id,canonical_name,display_name,brand,family_id,category,
                       variant_attributes_json,aliases_json,external_codes_json,status,effective_from,effective_to,etag,created_by,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(copied[key] for key in (
                        "sku_item_id", "tenant_id", "catalog_version_id", "sku_id", "canonical_name", "display_name", "brand", "family_id", "category",
                        "variant_attributes_json", "aliases_json", "external_codes_json", "status", "effective_from", "effective_to", "etag", "created_by", "created_at", "updated_at",
                    )),
                )
    version = _catalog_version(conn, user, version_id)
    log_audit(conn, user["user_id"], user["tenant_id"], "catalog.version.create", "catalog_version", version_id, None, {"version_number": version_number, "clone_published": clone_published}, "comparison_service", None)
    return serialize_catalog_version(version)


def _editable_catalog_version(conn: sqlite3.Connection, user: dict, version_id: str | None, payload: dict) -> dict:
    if not version_id:
        created = create_catalog_version(conn, user, payload, clone_published=bool(payload.get("clone_published")))
        version_id = created["version_id"]
    version = _catalog_version(conn, user, str(version_id))
    if version["state"] not in {"DRAFT", "VALIDATING"}:
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "只能编辑草稿或校验中的目录版本"})
    return version


def _catalog_duplicate_errors(conn: sqlite3.Connection, tenant_id: str, version_id: str, sku_id: str, external_codes: list[dict], *, exclude_item_id: str | None = None) -> list[str]:
    errors = []
    existing = one(
        conn,
        "SELECT sku_item_id FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=? AND sku_id=?",
        (tenant_id, version_id, sku_id),
    )
    if existing and existing["sku_item_id"] != exclude_item_id:
        errors.append("sku_id already exists in catalog version")
    wanted_codes = {f"{item['code_type']}:{item['code_value'].casefold()}" for item in external_codes}
    if wanted_codes:
        for row in rows(conn, "SELECT sku_item_id,external_codes_json FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=?", (tenant_id, version_id)):
            if row["sku_item_id"] == exclude_item_id:
                continue
            for code in json_loads(row["external_codes_json"], []):
                if not isinstance(code, dict):
                    continue
                key = f"{str(code.get('code_type') or '').upper()}:{str(code.get('code_value') or '').casefold()}"
                if key in wanted_codes:
                    errors.append("external code conflicts with another SKU in catalog version")
                    return errors
    return errors


def create_catalog_sku(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    _comparison_access_required(user)
    validation = validate_catalog_sku_payload(payload)
    if not validation["ok"]:
        raise ApiError("CATALOG_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": validation["errors"]})
    version = _editable_catalog_version(conn, user, str(payload.get("catalog_version_id") or "").strip() or None, payload)
    errors = []
    normalized = validation["normalized"]
    errors.extend(_catalog_duplicate_errors(conn, user["tenant_id"], version["version_id"], normalized["sku_id"], normalized["external_codes"]))
    if errors:
        raise ApiError("CATALOG_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": errors})
    item_id = f"skui_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO catalog_skus(sku_item_id,tenant_id,catalog_version_id,sku_id,canonical_name,display_name,brand,family_id,category,
           variant_attributes_json,aliases_json,external_codes_json,status,effective_from,effective_to,etag,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item_id, user["tenant_id"], version["version_id"], normalized["sku_id"], normalized["canonical_name"], normalized["display_name"], normalized["brand"], normalized["family_id"], normalized["category"], json_dumps(normalized["variant_attributes"]), json_dumps(normalized["aliases"]), json_dumps(normalized["external_codes"]), normalized["status"], normalized["effective_from"], normalized["effective_to"], normalized["etag"], user["user_id"], timestamp, timestamp),
    )
    item = one(conn, "SELECT * FROM catalog_skus WHERE sku_item_id=?", (item_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "catalog.sku.create", "catalog_sku", item_id, None, {"sku_id": normalized["sku_id"], "catalog_version_id": version["version_id"]}, "comparison_service", None)
    return {"sku": serialize_catalog_sku(item), "catalog_version": serialize_catalog_version(version)}


def update_catalog_sku(conn: sqlite3.Connection, user: dict, sku_item_id: str, payload: dict, if_match: str) -> dict:
    _comparison_access_required(user)
    item = one(conn, "SELECT * FROM catalog_skus WHERE sku_item_id=? AND tenant_id=?", (sku_item_id, user["tenant_id"]))
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    version = _editable_catalog_version(conn, user, item["catalog_version_id"], payload)
    if not if_match or if_match != item["etag"]:
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "If-Match 与当前 SKU ETag 不一致", "etag": item["etag"]})
    validation = validate_catalog_sku_payload({**serialize_catalog_sku(item), **payload, "sku_id": item["sku_id"]})
    normalized = validation["normalized"]
    errors = list(validation["errors"])
    errors.extend(_catalog_duplicate_errors(conn, user["tenant_id"], version["version_id"], normalized["sku_id"], normalized["external_codes"], exclude_item_id=sku_item_id))
    if errors:
        raise ApiError("CATALOG_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": errors})
    timestamp = now_iso()
    conn.execute(
        """UPDATE catalog_skus SET canonical_name=?,display_name=?,brand=?,family_id=?,category=?,variant_attributes_json=?,aliases_json=?,external_codes_json=?,
           status=?,effective_from=?,effective_to=?,etag=?,updated_at=? WHERE sku_item_id=? AND tenant_id=?""",
        (normalized["canonical_name"], normalized["display_name"], normalized["brand"], normalized["family_id"], normalized["category"], json_dumps(normalized["variant_attributes"]), json_dumps(normalized["aliases"]), json_dumps(normalized["external_codes"]), normalized["status"], normalized["effective_from"], normalized["effective_to"], normalized["etag"], timestamp, sku_item_id, user["tenant_id"]),
    )
    updated = one(conn, "SELECT * FROM catalog_skus WHERE sku_item_id=?", (sku_item_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "catalog.sku.update", "catalog_sku", sku_item_id, {"etag": item["etag"]}, {"etag": normalized["etag"], "catalog_version_id": version["version_id"]}, "comparison_service", None)
    return serialize_catalog_sku(updated)


def validate_catalog_version(conn: sqlite3.Connection, user: dict, version_id: str) -> dict:
    version = _catalog_version(conn, user, version_id)
    items = rows(conn, "SELECT * FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=?", (user["tenant_id"], version_id))
    errors = []
    if not items:
        errors.append("catalog version must contain at least one SKU")
    alias_owner: dict[str, str] = {}
    external_owner: dict[str, str] = {}
    for item in items:
        for alias in json_loads(item.get("aliases_json"), []):
            key = str(alias).strip().casefold()
            if key and key in alias_owner and alias_owner[key] != item["sku_id"]:
                errors.append(f"alias conflict: {alias}")
            elif key:
                alias_owner[key] = item["sku_id"]
        for code in json_loads(item.get("external_codes_json"), []):
            if not isinstance(code, dict):
                errors.append(f"invalid external code: {item['sku_id']}")
                continue
            key = f"{str(code.get('code_type') or '').upper()}:{str(code.get('code_value') or '').casefold()}"
            if not key.endswith(":") and key in external_owner and external_owner[key] != item["sku_id"]:
                errors.append(f"external code conflict: {key}")
            elif not key.endswith(":"):
                external_owner[key] = item["sku_id"]
    return {"ok": not errors, "errors": sorted(set(errors)), "sku_count": len(items), "content_hash": _catalog_content_hash(conn, user["tenant_id"], version_id)}


def approve_catalog_version(conn: sqlite3.Connection, user: dict, version_id: str) -> dict:
    _comparison_access_required(user)
    version = _catalog_version(conn, user, version_id)
    if version["state"] not in {"DRAFT", "VALIDATING"}:
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "只有草稿目录可以提交审批"})
    if version["created_by"] == user["user_id"]:
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN, {"message": "目录创建人与审批人必须不同"})
    validation = validate_catalog_version(conn, user, version_id)
    if not validation["ok"]:
        raise ApiError("CATALOG_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, validation)
    timestamp = now_iso()
    conn.execute("UPDATE catalog_versions SET state='PENDING_APPROVAL',content_hash=?,approved_by=?,approved_at=? WHERE version_id=? AND tenant_id=?", (validation["content_hash"], user["user_id"], timestamp, version_id, user["tenant_id"]))
    updated = _catalog_version(conn, user, version_id)
    log_audit(conn, user["user_id"], user["tenant_id"], "catalog.version.approve", "catalog_version", version_id, {"state": version["state"]}, {"state": "PENDING_APPROVAL", "content_hash": validation["content_hash"]}, "comparison_service", None)
    return {"catalog_version": serialize_catalog_version(updated), "validation": validation}


def publish_catalog_version(conn: sqlite3.Connection, user: dict, version_id: str) -> dict:
    _comparison_access_required(user)
    version = _catalog_version(conn, user, version_id)
    if version["state"] != "PENDING_APPROVAL":
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "目录必须先通过审批才能发布"})
    validation = validate_catalog_version(conn, user, version_id)
    if not validation["ok"]:
        raise ApiError("CATALOG_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, validation)
    timestamp = now_iso()
    conn.execute("UPDATE catalog_versions SET state='RETIRED',retired_at=? WHERE tenant_id=? AND state='PUBLISHED'", (timestamp, user["tenant_id"]))
    conn.execute("UPDATE catalog_versions SET state='PUBLISHED',content_hash=?,published_at=? WHERE version_id=? AND tenant_id=?", (validation["content_hash"], timestamp, version_id, user["tenant_id"]))
    updated = _catalog_version(conn, user, version_id)
    log_audit(conn, user["user_id"], user["tenant_id"], "catalog.version.publish", "catalog_version", version_id, {"state": version["state"]}, {"state": "PUBLISHED", "content_hash": validation["content_hash"]}, "comparison_service", None)
    return {"catalog_version": serialize_catalog_version(updated), "validation": validation}


def retire_catalog_version(conn: sqlite3.Connection, user: dict, version_id: str) -> dict:
    _comparison_access_required(user)
    version = _catalog_version(conn, user, version_id)
    if version["state"] not in {"PUBLISHED", "PENDING_APPROVAL"}:
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "当前目录状态不能退休"})
    timestamp = now_iso()
    conn.execute("UPDATE catalog_versions SET state='RETIRED',retired_at=? WHERE version_id=? AND tenant_id=?", (timestamp, version_id, user["tenant_id"]))
    updated = _catalog_version(conn, user, version_id)
    log_audit(conn, user["user_id"], user["tenant_id"], "catalog.version.retire", "catalog_version", version_id, {"state": version["state"]}, {"state": "RETIRED"}, "comparison_service", None)
    return serialize_catalog_version(updated)


def list_catalog_skus(conn: sqlite3.Connection, user: dict, version_id: str | None = None) -> dict:
    if version_id:
        version = _catalog_version(conn, user, version_id)
    else:
        version = one(conn, "SELECT * FROM catalog_versions WHERE tenant_id=? AND state='PUBLISHED' ORDER BY version_number DESC LIMIT 1", (user["tenant_id"],))
    if not version:
        return {"catalog_version": None, "skus": []}
    items = rows(conn, "SELECT * FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=? ORDER BY sku_id", (user["tenant_id"], version["version_id"]))
    return {"catalog_version": serialize_catalog_version(version), "skus": [serialize_catalog_sku(item) for item in items]}


def catalog_impact(conn: sqlite3.Connection, user: dict, version_id: str, sku_id: str | None = None) -> dict:
    version = _catalog_version(conn, user, version_id)
    args = [user["tenant_id"], version_id]
    sku_filter = ""
    if sku_id:
        sku_filter = " AND sku_id=?"
        args.append(str(sku_id).upper())
    assets = rows(conn, f"SELECT asset_id,sku_id,approval_status,asset_url FROM reference_assets WHERE tenant_id=? AND catalog_version_id=?{sku_filter} ORDER BY sku_id", args)
    slots = rows(conn, "SELECT slot_id,expected_skus_json,status,automation_enabled FROM display_slots WHERE tenant_id=? AND catalog_version_id=? ORDER BY slot_id", (user["tenant_id"], version_id))
    selected_slots = [slot for slot in slots if not sku_id or str(sku_id).upper() in {str(item).upper() for item in json_loads(slot["expected_skus_json"], [])}]
    return {"catalog_version": serialize_catalog_version(version), "sku_id": str(sku_id or "").upper() or None, "reference_assets": assets, "display_slots": [{**slot, "expected_skus": json_loads(slot.pop("expected_skus_json"), [])} for slot in selected_slots], "high_impact": bool(selected_slots)}


def _published_catalog_version(conn: sqlite3.Connection, user: dict, version_id: str) -> dict:
    version = _catalog_version(conn, user, version_id)
    if version["state"] != "PUBLISHED":
        raise ApiError("CATALOG_NOT_PUBLISHED", HTTPStatus.CONFLICT, {"catalog_version_id": version_id})
    return version


def serialize_domain_profile(item: dict) -> dict:
    return {
        "profile_id": item["profile_id"],
        "name": item["name"],
        "domain": item["domain"],
        "category": item.get("category") or "",
        "capture_mode": item["capture_mode"],
        "identity_policy": json_loads(item.get("identity_policy_json"), {}),
        "quality_bundle": json_loads(item.get("quality_bundle_json"), {}),
        "version": item["version"],
        "status": item["status"],
        "created_by": item["created_by"],
        "approved_by": item.get("approved_by"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def create_domain_profile(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    _comparison_access_required(user)
    name = str(payload.get("name") or "").strip()
    domain = str(payload.get("domain") or "").strip()
    category = str(payload.get("category") or "").strip()
    capture_mode = str(payload.get("capture_mode") or "FIXED_CAMERA").upper()
    policy = payload.get("identity_policy") or {}
    quality_bundle = payload.get("quality_bundle") or {}
    if len(name) < 2 or len(domain) < 2 or capture_mode not in PROFILE_CAPTURE_MODES or not isinstance(policy, dict) or not policy:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "业态包名称、业态、采集方式和身份证据策略均为必填"})
    if not isinstance(quality_bundle, dict):
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "质量阈值包必须为对象"})
    highest = one(conn, "SELECT MAX(version) AS maximum FROM domain_profiles WHERE tenant_id=? AND name=?", (user["tenant_id"], name))
    profile_id = f"dp_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO domain_profiles(profile_id,tenant_id,name,domain,category,capture_mode,identity_policy_json,quality_bundle_json,version,status,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (profile_id, user["tenant_id"], name[:120], domain[:80], category[:120], capture_mode, json_dumps(policy), json_dumps(quality_bundle), int(highest.get("maximum") or 0) + 1, "DRAFT", user["user_id"], timestamp, timestamp),
    )
    item = one(conn, "SELECT * FROM domain_profiles WHERE profile_id=?", (profile_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.domain_profile.create", "domain_profile", profile_id, None, {"name": name, "capture_mode": capture_mode}, "comparison_service", None)
    return serialize_domain_profile(item)


def approve_domain_profile(conn: sqlite3.Connection, user: dict, profile_id: str) -> dict:
    _comparison_access_required(user)
    item = one(conn, "SELECT * FROM domain_profiles WHERE profile_id=? AND tenant_id=?", (profile_id, user["tenant_id"]))
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    if item["status"] != "DRAFT":
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "业态包不是待审批草稿"})
    timestamp = now_iso()
    conn.execute("UPDATE domain_profiles SET status='ACTIVE',approved_by=?,updated_at=? WHERE profile_id=? AND tenant_id=?", (user["user_id"], timestamp, profile_id, user["tenant_id"]))
    updated = one(conn, "SELECT * FROM domain_profiles WHERE profile_id=?", (profile_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.domain_profile.approve", "domain_profile", profile_id, {"status": item["status"]}, {"status": "ACTIVE"}, "comparison_service", None)
    return serialize_domain_profile(updated)


def serialize_calibration_profile(item: dict) -> dict:
    return {
        "calibration_id": item["calibration_id"],
        "camera_id": item["camera_id"],
        "version": item["version"],
        "roi": json_loads(item.get("roi_json"), []),
        "health_state": item["health_state"],
        "effective_from": item["effective_from"],
        "expires_at": item.get("expires_at"),
        "status": item["status"],
        "approved_by": item.get("approved_by"),
        "updated_at": item["updated_at"],
    }


def _normalized_roi(value) -> list[list[float]]:
    if not isinstance(value, list) or len(value) < 3 or len(value) > 32:
        raise ValueError("ROI 至少需要三个归一化坐标点")
    normalized = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("ROI 坐标格式错误")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("ROI 坐标格式错误") from exc
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("ROI 坐标必须为 0 到 1 的归一化值")
        normalized.append([x, y])
    return normalized


def create_calibration_profile(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    _comparison_access_required(user)
    camera_id = str(payload.get("camera_id") or "").strip()
    version = str(payload.get("version") or "").strip()
    health_state = str(payload.get("health_state") or "GREEN").upper()
    if not camera_id or not version or health_state not in {"GREEN", "AMBER", "RED"}:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "标定必须指定镜头、版本和健康状态"})
    try:
        roi = _normalized_roi(payload.get("roi"))
    except ValueError as exc:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": str(exc)}) from exc
    calibration_id = f"cal_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO calibration_profiles(calibration_id,tenant_id,camera_id,version,roi_json,health_state,effective_from,expires_at,status,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (calibration_id, user["tenant_id"], camera_id[:120], version[:80], json_dumps(roi), health_state, str(payload.get("effective_from") or timestamp)[:40], str(payload.get("expires_at") or "").strip()[:40] or None, "DRAFT", user["user_id"], timestamp, timestamp),
    )
    item = one(conn, "SELECT * FROM calibration_profiles WHERE calibration_id=?", (calibration_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.calibration.create", "calibration_profile", calibration_id, None, {"camera_id": camera_id, "version": version}, "comparison_service", None)
    return serialize_calibration_profile(item)


def approve_calibration_profile(conn: sqlite3.Connection, user: dict, calibration_id: str) -> dict:
    _comparison_access_required(user)
    item = one(conn, "SELECT * FROM calibration_profiles WHERE calibration_id=? AND tenant_id=?", (calibration_id, user["tenant_id"]))
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    if item["status"] != "DRAFT":
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "标定不是待审批草稿"})
    timestamp = now_iso()
    conn.execute("UPDATE calibration_profiles SET status='ACTIVE',approved_by=?,updated_at=? WHERE calibration_id=? AND tenant_id=?", (user["user_id"], timestamp, calibration_id, user["tenant_id"]))
    updated = one(conn, "SELECT * FROM calibration_profiles WHERE calibration_id=?", (calibration_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.calibration.approve", "calibration_profile", calibration_id, {"status": item["status"]}, {"status": "ACTIVE"}, "comparison_service", None)
    return serialize_calibration_profile(updated)


def _active_calibration(conn: sqlite3.Connection, user: dict, camera_id: str, version: str) -> dict | None:
    item = one(conn, "SELECT * FROM calibration_profiles WHERE tenant_id=? AND camera_id=? AND version=? AND status='ACTIVE'", (user["tenant_id"], camera_id, version))
    if not item:
        return None
    timestamp = now_iso()
    if item.get("expires_at") and str(item["expires_at"]) < timestamp:
        return None
    return item


def serialize_reference_asset(item: dict) -> dict:
    return {
        "asset_id": item["asset_id"],
        "sku_id": item["sku_id"],
        "catalog_version_id": item["catalog_version_id"],
        "knowledge_id": item.get("knowledge_id"),
        "asset_url": item["asset_url"],
        "view_tag": item.get("view_tag") or "",
        "camera_profile": item.get("camera_profile") or "",
        "source_type": item["source_type"],
        "feature_version": item.get("feature_version") or "",
        "approval_status": item["approval_status"],
        "effective_at": item["effective_at"],
        "expires_at": item.get("expires_at"),
    }


def create_reference_asset(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    _comparison_access_required(user)
    version_id = str(payload.get("catalog_version_id") or "").strip()
    sku_id = str(payload.get("sku_id") or "").strip().upper()
    asset_url = str(payload.get("asset_url") or "").strip()
    _published_catalog_version(conn, user, version_id)
    sku = one(conn, "SELECT sku_id FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=? AND sku_id=? AND status='ACTIVE'", (user["tenant_id"], version_id, sku_id))
    if not sku or not asset_url or not (asset_url.startswith("/static/") or re.fullmatch(r"https://[^\s]+", asset_url, flags=re.IGNORECASE)):
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "样板必须关联已发布在售 SKU，并使用受控本地或 HTTPS 素材地址"})
    asset_id = f"ra_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO reference_assets(asset_id,tenant_id,sku_id,catalog_version_id,knowledge_id,asset_url,view_tag,camera_profile,source_type,feature_version,approval_status,effective_at,expires_at,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (asset_id, user["tenant_id"], sku_id, version_id, str(payload.get("knowledge_id") or "").strip() or None, asset_url[:1024], str(payload.get("view_tag") or "").strip()[:80], str(payload.get("camera_profile") or "").strip()[:120], str(payload.get("source_type") or "manual").strip()[:80], str(payload.get("feature_version") or "").strip()[:80], "DRAFT", str(payload.get("effective_at") or timestamp)[:40], str(payload.get("expires_at") or "").strip()[:40] or None, user["user_id"], timestamp, timestamp),
    )
    item = one(conn, "SELECT * FROM reference_assets WHERE asset_id=?", (asset_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.reference_asset.create", "reference_asset", asset_id, None, {"sku_id": sku_id, "catalog_version_id": version_id}, "comparison_service", None)
    return serialize_reference_asset(item)


def approve_reference_asset(conn: sqlite3.Connection, user: dict, asset_id: str) -> dict:
    _comparison_access_required(user)
    item = one(conn, "SELECT * FROM reference_assets WHERE asset_id=? AND tenant_id=?", (asset_id, user["tenant_id"]))
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    if item["approval_status"] != "DRAFT":
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "样板不是待审批状态"})
    timestamp = now_iso()
    conn.execute("UPDATE reference_assets SET approval_status='APPROVED',approved_by=?,updated_at=? WHERE asset_id=? AND tenant_id=?", (user["user_id"], timestamp, asset_id, user["tenant_id"]))
    updated = one(conn, "SELECT * FROM reference_assets WHERE asset_id=?", (asset_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.reference_asset.approve", "reference_asset", asset_id, {"approval_status": item["approval_status"]}, {"approval_status": "APPROVED"}, "comparison_service", None)
    return serialize_reference_asset(updated)


def serialize_display_slot(item: dict) -> dict:
    return {
        "slot_id": item["slot_id"],
        "org_id": item["org_id"],
        "camera_id": item["camera_id"],
        "domain_profile_id": item["domain_profile_id"],
        "catalog_version_id": item["catalog_version_id"],
        "calibration_version": item["calibration_version"],
        "zone_polygon": json_loads(item.get("zone_polygon_json"), []),
        "expected_skus": json_loads(item.get("expected_skus_json"), []),
        "expected_count": item["expected_count"],
        "min_valid_frames": item["min_valid_frames"],
        "quality_threshold": item["quality_threshold"],
        "min_roi_coverage": item["min_roi_coverage"],
        "max_occlusion": item["max_occlusion"],
        "automation_enabled": bool(item["automation_enabled"]),
        "status": item["status"],
        "effective_from": item["effective_from"],
        "effective_to": item.get("effective_to"),
    }


def create_display_slot(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    _comparison_access_required(user)
    org_id = str(payload.get("org_id") or "").strip()
    camera_id = str(payload.get("camera_id") or "").strip()
    profile_id = str(payload.get("domain_profile_id") or "").strip()
    catalog_version_id = str(payload.get("catalog_version_id") or "").strip()
    calibration_version = str(payload.get("calibration_version") or "").strip()
    expected_skus = [item.upper() for item in _normalize_text_list(payload.get("expected_skus"), limit=20, item_limit=64)]
    try:
        zone = _normalized_roi(payload.get("zone_polygon"))
        expected_count = max(1, int(payload.get("expected_count") or 1))
        min_valid_frames = max(1, int(payload.get("min_valid_frames") or 3))
        quality_threshold = float(payload.get("quality_threshold") if payload.get("quality_threshold") is not None else 0.7)
        min_roi_coverage = float(payload.get("min_roi_coverage") if payload.get("min_roi_coverage") is not None else 0.8)
        max_occlusion = float(payload.get("max_occlusion") if payload.get("max_occlusion") is not None else 0.2)
    except (ValueError, TypeError) as exc:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "槽位数值或空间配置不合法"}) from exc
    if not org_id or not camera_id or not profile_id or not expected_skus or not all(0 <= item <= 1 for item in (quality_threshold, min_roi_coverage, max_occlusion)):
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "槽位必须配置组织、镜头、业态包、预期 SKU 与 0~1 的质量阈值"})
    _published_catalog_version(conn, user, catalog_version_id)
    profile = one(conn, "SELECT * FROM domain_profiles WHERE profile_id=? AND tenant_id=? AND status='ACTIVE'", (profile_id, user["tenant_id"]))
    calibration = _active_calibration(conn, user, camera_id, calibration_version)
    if not profile or not calibration:
        raise ApiError("COMPARISON_PREREQUISITE_MISSING", HTTPStatus.CONFLICT, {"message": "槽位必须引用已审批业态包和有效镜头标定"})
    present_skus = {item["sku_id"] for item in rows(conn, "SELECT sku_id FROM catalog_skus WHERE tenant_id=? AND catalog_version_id=? AND status='ACTIVE'", (user["tenant_id"], catalog_version_id))}
    if not set(expected_skus).issubset(present_skus):
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "槽位包含未发布或已停售 SKU"})
    slot_id = f"slot_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO display_slots(slot_id,tenant_id,org_id,camera_id,domain_profile_id,catalog_version_id,calibration_version,zone_polygon_json,expected_skus_json,expected_count,min_valid_frames,quality_threshold,min_roi_coverage,max_occlusion,automation_enabled,status,effective_from,effective_to,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (slot_id, user["tenant_id"], org_id, camera_id, profile_id, catalog_version_id, calibration_version, json_dumps(zone), json_dumps(expected_skus), expected_count, min_valid_frames, quality_threshold, min_roi_coverage, max_occlusion, bool(payload.get("automation_enabled")), "DRAFT", str(payload.get("effective_from") or timestamp)[:40], str(payload.get("effective_to") or "").strip()[:40] or None, user["user_id"], timestamp, timestamp),
    )
    item = one(conn, "SELECT * FROM display_slots WHERE slot_id=?", (slot_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.display_slot.create", "display_slot", slot_id, None, {"camera_id": camera_id, "expected_skus": expected_skus}, "comparison_service", None)
    return serialize_display_slot(item)


def approve_display_slot(conn: sqlite3.Connection, user: dict, slot_id: str) -> dict:
    _comparison_access_required(user)
    item = one(conn, "SELECT * FROM display_slots WHERE slot_id=? AND tenant_id=?", (slot_id, user["tenant_id"]))
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    if item["status"] != "DRAFT":
        raise ApiError("CATALOG_VERSION_CONFLICT", HTTPStatus.CONFLICT, {"message": "槽位不是待审批状态"})
    approved_skus = {asset["sku_id"] for asset in rows(conn, "SELECT sku_id FROM reference_assets WHERE tenant_id=? AND catalog_version_id=? AND approval_status='APPROVED'", (user["tenant_id"], item["catalog_version_id"]))}
    expected_skus = {str(value).upper() for value in json_loads(item["expected_skus_json"], [])}
    if not expected_skus.issubset(approved_skus):
        raise ApiError("COMPARISON_PREREQUISITE_MISSING", HTTPStatus.CONFLICT, {"message": "槽位预期 SKU 尚缺已审批样板"})
    timestamp = now_iso()
    conn.execute("UPDATE display_slots SET status='ACTIVE',approved_by=?,updated_at=? WHERE slot_id=? AND tenant_id=?", (user["user_id"], timestamp, slot_id, user["tenant_id"]))
    updated = one(conn, "SELECT * FROM display_slots WHERE slot_id=?", (slot_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.display_slot.approve", "display_slot", slot_id, {"status": item["status"]}, {"status": "ACTIVE"}, "comparison_service", None)
    return serialize_display_slot(updated)


def _comparison_snapshot(catalog: dict, profile: dict, calibration: dict, slots: list[dict]) -> dict:
    source = {
        "catalog_version_id": catalog["version_id"],
        "catalog_version": catalog["version_number"],
        "catalog_content_hash": catalog.get("content_hash"),
        "domain_profile_id": profile["profile_id"],
        "domain_profile_version": profile["version"],
        "capture_mode": profile["capture_mode"],
        "identity_policy": json_loads(profile.get("identity_policy_json"), {}),
        "calibration_version": calibration["version"],
        "slot_ids": [item["slot_id"] for item in slots],
        "ovd_adapter": {"provider": "external_ovd", "contract": "detection-v1", "credential_source": "environment"},
        "comparison_worker": {"identity_evidence": "required", "calibrator": "unconfigured", "rule_bundle": "slot-window-v1"},
    }
    source["snapshot_id"] = hashlib.sha256(json_dumps(source).encode("utf-8")).hexdigest()[:24]
    return source


def serialize_comparison_frame(item: dict) -> dict:
    return {
        "frame_id": item["frame_id"],
        "evidence_id": item.get("evidence_id"),
        "evidence_sha256": item["evidence_sha256"],
        "captured_at": item["captured_at"],
        "state": item["frame_state"],
        "quality_score": item.get("quality_score"),
        "roi_coverage": item.get("roi_coverage"),
        "occlusion_ratio": item.get("occlusion_ratio"),
        "camera_health": item.get("camera_health"),
        "detections": json_loads(item.get("detections_json"), []),
        "object_evidence": json_loads(item.get("object_evidence_json"), []),
        "reason_codes": json_loads(item.get("reason_codes_json"), []),
        "run_snapshot": json_loads(item.get("run_snapshot_json"), {}),
    }


def serialize_comparison_decision(item: dict) -> dict:
    return {
        "slot_decision_id": item["decision_id"],
        "slot_id": item["slot_id"],
        "state": item["state"],
        "calibrated_probability": item.get("calibrated_probability"),
        "observed_count": item["observed_count"],
        "valid_frame_count": item["valid_frame_count"],
        "reason_codes": json_loads(item.get("reason_codes_json"), []),
        "evidence_refs": json_loads(item.get("evidence_refs_json"), []),
        "run_snapshot": json_loads(item.get("run_snapshot_json"), {}),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def serialize_comparison_session(item: dict) -> dict:
    return {
        "session_id": item["session_id"],
        "camera_id": item["camera_id"],
        "capture_mode": item["capture_mode"],
        "domain_profile_id": item["domain_profile_id"],
        "domain_profile_version": item["domain_profile_version"],
        "catalog_version_id": item["catalog_version_id"],
        "calibration_version": item["calibration_version"],
        "display_slot_ids": json_loads(item.get("display_slot_ids_json"), []),
        "evidence_refs": json_loads(item.get("evidence_refs_json"), []),
        "idempotency_key": item["idempotency_key"],
        "status": item["status"],
        "run_snapshot": json_loads(item.get("run_snapshot_json"), {}),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def _session_and_dependencies(conn: sqlite3.Connection, user: dict, session_id: str) -> tuple[dict, dict, dict, dict, list[dict]]:
    session = one(conn, "SELECT * FROM comparison_sessions WHERE session_id=? AND tenant_id=?", (session_id, user["tenant_id"]))
    if not session:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    catalog = _catalog_version(conn, user, session["catalog_version_id"])
    profile = one(conn, "SELECT * FROM domain_profiles WHERE profile_id=? AND tenant_id=?", (session["domain_profile_id"], user["tenant_id"]))
    calibration = one(conn, "SELECT * FROM calibration_profiles WHERE tenant_id=? AND camera_id=? AND version=?", (user["tenant_id"], session["camera_id"], session["calibration_version"]))
    slot_ids = json_loads(session.get("display_slot_ids_json"), [])
    slots = []
    for slot_id in slot_ids:
        slot = one(conn, "SELECT * FROM display_slots WHERE slot_id=? AND tenant_id=?", (str(slot_id), user["tenant_id"]))
        if slot:
            slots.append(slot)
    if not profile or not calibration:
        raise ApiError("COMPARISON_PREREQUISITE_MISSING", HTTPStatus.CONFLICT, {"message": "比对会话引用的业态包或标定已不可用"})
    return session, catalog, profile, calibration, slots


def create_comparison_session(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    _comparison_access_required(user)
    camera_id = str(payload.get("camera_id") or "").strip()
    profile_id = str(payload.get("domain_profile_id") or "").strip()
    catalog_version_id = str(payload.get("catalog_version_id") or "").strip()
    calibration_version = str(payload.get("calibration_version") or "").strip()
    capture_mode = str(payload.get("capture_mode") or "FIXED_CAMERA").upper()
    idempotency_key = str(payload.get("idempotency_key") or "").strip()[:160]
    try:
        slot_ids = _normalize_text_list(payload.get("display_slot_ids"), limit=20, item_limit=80)
        evidence_refs = _normalize_text_list(payload.get("evidence_refs"), limit=200, item_limit=160)
    except ValueError as exc:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "槽位或证据引用必须为列表"}) from exc
    if not camera_id or not profile_id or not catalog_version_id or not calibration_version or capture_mode not in PROFILE_CAPTURE_MODES or not idempotency_key or not slot_ids:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "会话必须提供镜头、业态包、已发布目录、标定、槽位和幂等键"})
    existing = one(conn, "SELECT * FROM comparison_sessions WHERE tenant_id=? AND idempotency_key=?", (user["tenant_id"], idempotency_key))
    if existing:
        same = all(str(existing[key]) == str(value) for key, value in (("camera_id", camera_id), ("capture_mode", capture_mode), ("domain_profile_id", profile_id), ("catalog_version_id", catalog_version_id), ("calibration_version", calibration_version)))
        same = same and json_loads(existing.get("display_slot_ids_json"), []) == slot_ids and json_loads(existing.get("evidence_refs_json"), []) == evidence_refs
        if not same:
            raise ApiError("IDEMPOTENCY_CONFLICT", HTTPStatus.CONFLICT, {"message": "幂等键已用于不同会话快照"})
        return serialize_comparison_session(existing)
    catalog = _published_catalog_version(conn, user, catalog_version_id)
    profile = one(conn, "SELECT * FROM domain_profiles WHERE profile_id=? AND tenant_id=? AND status='ACTIVE'", (profile_id, user["tenant_id"]))
    calibration = _active_calibration(conn, user, camera_id, calibration_version)
    if not profile or not calibration or calibration["health_state"] != "GREEN":
        raise ApiError("COMPARISON_PREREQUISITE_MISSING", HTTPStatus.CONFLICT, {"message": "会话必须绑定启用业态包和有效 GREEN 标定"})
    placeholders = ",".join("?" for _ in slot_ids)
    slots = rows(conn, f"SELECT * FROM display_slots WHERE tenant_id=? AND slot_id IN ({placeholders})", (user["tenant_id"], *slot_ids))
    if len(slots) != len(slot_ids) or any(item["status"] != "ACTIVE" or item["camera_id"] != camera_id or item["catalog_version_id"] != catalog_version_id or item["domain_profile_id"] != profile_id or item["calibration_version"] != calibration_version for item in slots):
        raise ApiError("COMPARISON_PREREQUISITE_MISSING", HTTPStatus.CONFLICT, {"message": "会话槽位必须与当前镜头、目录、业态包和标定快照一致且已启用"})
    snapshot = _comparison_snapshot(catalog, profile, calibration, slots)
    session_id = f"cs_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO comparison_sessions(session_id,tenant_id,created_by,camera_id,capture_mode,domain_profile_id,domain_profile_version,catalog_version_id,calibration_version,display_slot_ids_json,evidence_refs_json,idempotency_key,status,run_snapshot_json,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (session_id, user["tenant_id"], user["user_id"], camera_id, capture_mode, profile_id, profile["version"], catalog_version_id, calibration_version, json_dumps(slot_ids), json_dumps(evidence_refs), idempotency_key, "OPEN", json_dumps(snapshot), timestamp, timestamp),
    )
    session = one(conn, "SELECT * FROM comparison_sessions WHERE session_id=?", (session_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.session.create", "comparison_session", session_id, None, {"snapshot_id": snapshot["snapshot_id"], "slot_count": len(slots)}, "comparison_service", None)
    return serialize_comparison_session(session)


def _record_comparison_frame(conn: sqlite3.Connection, user: dict, session_id: str, payload: dict, *, internal_worker: bool = False) -> dict:
    session, _catalog, _profile, _calibration, _slots = _session_and_dependencies(conn, user, session_id)
    evidence_sha256 = str(payload.get("evidence_sha256") or "").strip().lower()
    captured_at = str(payload.get("captured_at") or "").strip()
    evidence_id = str(payload.get("evidence_id") or "").strip() or None
    if not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256) or not captured_at:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "帧必须提供 SHA-256 与采集时间"})
    state = str(payload.get("state") or "EVIDENCE_READY").upper()
    if state not in {"EVIDENCE_READY", "SYSTEM_FAILED"}:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "帧状态不合法"})
    object_evidence = payload.get("object_evidence") or []
    detections = payload.get("detections") or []
    if not internal_worker and object_evidence:
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN, {"message": "对象身份结果只能由受控 Comparison Worker 写入"})
    if not isinstance(object_evidence, list) or not isinstance(detections, list):
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "检测与对象证据必须为数组"})
    try:
        quality_score = float(payload.get("quality_score") if payload.get("quality_score") is not None else 0)
        roi_coverage = float(payload.get("roi_coverage") if payload.get("roi_coverage") is not None else 0)
        occlusion_ratio = float(payload.get("occlusion_ratio") if payload.get("occlusion_ratio") is not None else 1)
    except (TypeError, ValueError) as exc:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "帧质量指标不合法"}) from exc
    if not all(0 <= item <= 1 for item in (quality_score, roi_coverage, occlusion_ratio)):
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "帧质量指标必须在 0 到 1"})
    reason_codes = _normalize_text_list(payload.get("reason_codes"), limit=20, item_limit=80)
    snapshot = json_loads(session["run_snapshot_json"], {})
    frame_id = f"cf_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    try:
        conn.execute(
            """INSERT INTO comparison_frames(frame_id,session_id,tenant_id,evidence_id,evidence_sha256,captured_at,frame_state,quality_score,roi_coverage,occlusion_ratio,camera_health,detections_json,object_evidence_json,reason_codes_json,run_snapshot_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (frame_id, session_id, user["tenant_id"], evidence_id, evidence_sha256, captured_at[:40], state, quality_score, roi_coverage, occlusion_ratio, str(payload.get("camera_health") or "UNKNOWN").upper(), json_dumps(detections), json_dumps(object_evidence), json_dumps(reason_codes), json_dumps(snapshot), timestamp),
        )
    except sqlite3.IntegrityError as exc:
        existing = one(conn, "SELECT * FROM comparison_frames WHERE session_id=? AND evidence_sha256=? AND captured_at=?", (session_id, evidence_sha256, captured_at[:40]))
        if existing:
            return serialize_comparison_frame(existing)
        raise ApiError("IDEMPOTENCY_CONFLICT", HTTPStatus.CONFLICT) from exc
    item = one(conn, "SELECT * FROM comparison_frames WHERE frame_id=?", (frame_id,))
    conn.execute("UPDATE comparison_sessions SET status='EVIDENCE_COLLECTING',updated_at=? WHERE session_id=?", (timestamp, session_id))
    return serialize_comparison_frame(item)


def _scheduled_evidence_content(conn: sqlite3.Connection, user: dict, evidence_id: str) -> tuple[dict, bytes]:
    evidence = one(
        conn,
        """SELECT e.* FROM scheduled_evidence e JOIN scheduled_inspections s ON s.task_id=e.task_id
           WHERE e.evidence_id=? AND s.tenant_id=?""",
        (evidence_id, user["tenant_id"]),
    )
    if not evidence:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"message": "找不到当前租户的巡检证据"})
    path = Path(evidence["storage_path"]).resolve()
    root = SCHEDULED_EVIDENCE_DIR.resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise ApiError("COMPARISON_INVALID", HTTPStatus.CONFLICT, {"message": "巡检证据存储不可用"})
    return evidence, path.read_bytes()


def create_ovd_comparison_frame(conn: sqlite3.Connection, user: dict, session_id: str, payload: dict) -> dict:
    _comparison_access_required(user)
    evidence_id = str(payload.get("evidence_id") or "").strip()
    if not evidence_id:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "当前接口仅接受已受控存储的 evidence_id"})
    evidence, image_bytes = _scheduled_evidence_content(conn, user, evidence_id)
    session, _catalog, profile, _calibration, _slots = _session_and_dependencies(conn, user, session_id)
    policy = json_loads(profile.get("identity_policy_json"), {})
    prompts = _normalize_text_list(policy.get("ovd_prompts") or policy.get("object_prompts"), limit=20, item_limit=120)
    if not prompts:
        return _record_comparison_frame(
            conn, user, session_id,
            {"evidence_id": evidence_id, "evidence_sha256": evidence["sha256"], "captured_at": evidence["captured_at"], "state": "SYSTEM_FAILED", "quality_score": 0, "roi_coverage": 0, "occlusion_ratio": 1, "camera_health": "UNKNOWN", "reason_codes": ["OVD_PROMPT_POLICY_MISSING"]},
        )
    try:
        detections = SafeOvdAdapter().inspect_bytes(image_bytes, prompts, f"{session['session_id']}:{evidence_id}")
        return _record_comparison_frame(
            conn, user, session_id,
            {"evidence_id": evidence_id, "evidence_sha256": evidence["sha256"], "captured_at": evidence["captured_at"], "state": "EVIDENCE_READY", "quality_score": 0, "roi_coverage": 0, "occlusion_ratio": 1, "camera_health": "UNKNOWN", "detections": detections["detections"], "reason_codes": ["IDENTITY_RETRIEVAL_REQUIRED"]},
        )
    except OvdAdapterFailure as exc:
        return _record_comparison_frame(
            conn, user, session_id,
            {"evidence_id": evidence_id, "evidence_sha256": evidence["sha256"], "captured_at": evidence["captured_at"], "state": "SYSTEM_FAILED", "quality_score": 0, "roi_coverage": 0, "occlusion_ratio": 1, "camera_health": "UNKNOWN", "reason_codes": [exc.code]},
        )


def refresh_comparison_slot_decisions(conn: sqlite3.Connection, user: dict, session_id: str) -> list[dict]:
    _comparison_access_required(user)
    session, catalog, profile, calibration, slots = _session_and_dependencies(conn, user, session_id)
    frames = [serialize_comparison_frame(item) for item in rows(conn, "SELECT * FROM comparison_frames WHERE session_id=? AND tenant_id=? ORDER BY captured_at", (session_id, user["tenant_id"]))]
    reference_skus = {item["sku_id"] for item in rows(conn, "SELECT sku_id FROM reference_assets WHERE tenant_id=? AND catalog_version_id=? AND approval_status='APPROVED'", (user["tenant_id"], catalog["version_id"]))}
    timestamp = now_iso()
    decisions = []
    for slot in slots:
        prerequisites_ok = bool(catalog["state"] == "PUBLISHED" and profile["status"] == "ACTIVE" and calibration["status"] == "ACTIVE" and calibration["health_state"] == "GREEN" and slot["status"] == "ACTIVE" and slot["automation_enabled"])
        evidence = evaluate_slot_evidence({**serialize_display_slot(slot), "expected_skus": json_loads(slot["expected_skus_json"], [])}, frames, reference_skus, prerequisites_ok=prerequisites_ok)
        evidence_refs = [item["frame_id"] for item in frames]
        existing = one(conn, "SELECT * FROM comparison_slot_decisions WHERE session_id=? AND slot_id=?", (session_id, slot["slot_id"]))
        if existing:
            decision_id = existing["decision_id"]
            conn.execute(
                """UPDATE comparison_slot_decisions SET state=?,calibrated_probability=?,observed_count=?,valid_frame_count=?,reason_codes_json=?,evidence_refs_json=?,run_snapshot_json=?,updated_at=? WHERE decision_id=?""",
                (evidence["state"], None, evidence["observed_count"], evidence["valid_frame_count"], json_dumps(evidence["reason_codes"]), json_dumps(evidence_refs), session["run_snapshot_json"], timestamp, decision_id),
            )
        else:
            decision_id = f"sd_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO comparison_slot_decisions(decision_id,session_id,tenant_id,slot_id,state,calibrated_probability,observed_count,valid_frame_count,reason_codes_json,evidence_refs_json,run_snapshot_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, session_id, user["tenant_id"], slot["slot_id"], evidence["state"], None, evidence["observed_count"], evidence["valid_frame_count"], json_dumps(evidence["reason_codes"]), json_dumps(evidence_refs), session["run_snapshot_json"], timestamp, timestamp),
            )
        decisions.append(serialize_comparison_decision(one(conn, "SELECT * FROM comparison_slot_decisions WHERE decision_id=?", (decision_id,))))
    aggregate_status = "SYSTEM_FAILED" if any(item["state"] == "SYSTEM_FAILED" for item in decisions) else "REVIEW" if any(item["state"] in {"REVIEW", "INCONCLUSIVE"} for item in decisions) else "DECIDED"
    conn.execute("UPDATE comparison_sessions SET status=?,updated_at=? WHERE session_id=?", (aggregate_status, timestamp, session_id))
    return decisions


def comparison_session_detail(conn: sqlite3.Connection, user: dict, session_id: str) -> dict:
    session, _catalog, _profile, _calibration, _slots = _session_and_dependencies(conn, user, session_id)
    frames = [serialize_comparison_frame(item) for item in rows(conn, "SELECT * FROM comparison_frames WHERE session_id=? AND tenant_id=? ORDER BY captured_at", (session_id, user["tenant_id"]))]
    decisions = [serialize_comparison_decision(item) for item in rows(conn, "SELECT * FROM comparison_slot_decisions WHERE session_id=? AND tenant_id=? ORDER BY created_at", (session_id, user["tenant_id"]))]
    return {"comparison_session": serialize_comparison_session(session), "frames": frames, "slot_decisions": decisions}


def create_comparison_review(conn: sqlite3.Connection, user: dict, decision_id: str, payload: dict) -> dict:
    if not role_can_feedback(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    slot_decision = one(conn, "SELECT * FROM comparison_slot_decisions WHERE decision_id=? AND tenant_id=?", (decision_id, user["tenant_id"]))
    if not slot_decision:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    decision = str(payload.get("decision") or "").upper()
    reason = str(payload.get("reason") or "").strip()
    chosen_identity = str(payload.get("chosen_identity") or "").strip().upper() or None
    try:
        evidence_refs = _normalize_text_list(payload.get("evidence_refs"), limit=30, item_limit=160)
    except ValueError as exc:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "复核证据必须为数组"}) from exc
    if decision not in REVIEW_DECISIONS or len(reason) < 2:
        raise ApiError("COMPARISON_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "复核必须提供合法决定和原因"})
    review_id = f"rv_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO comparison_reviews(review_id,decision_id,tenant_id,decision,chosen_identity,reason,evidence_refs_json,training_eligibility,operator_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (review_id, decision_id, user["tenant_id"], decision, chosen_identity, reason[:1000], json_dumps(evidence_refs), bool(payload.get("training_eligibility")), user["user_id"], timestamp),
    )
    review = one(conn, "SELECT * FROM comparison_reviews WHERE review_id=?", (review_id,))
    log_audit(conn, user["user_id"], user["tenant_id"], "comparison.review.create", "comparison_slot_decision", decision_id, None, {"review_id": review_id, "decision": decision}, "comparison_service", None)
    return {"review_id": review["review_id"], "slot_decision_id": decision_id, "decision": review["decision"], "chosen_identity": review.get("chosen_identity"), "reason": review["reason"], "evidence_refs": json_loads(review["evidence_refs_json"], []), "training_eligibility": bool(review["training_eligibility"]), "operator_id": review["operator_id"], "created_at": review["created_at"]}


def serialize_agent_manifest_import(item: dict) -> dict:
    manifest = json_loads(item.get("manifest_json"), {})
    if not isinstance(manifest, dict):
        manifest = {}
    validation = json_loads(item.get("validation_json"), {})
    normalized = validation.get("normalized") if isinstance(validation, dict) else {}
    if not isinstance(normalized, dict):
        normalized = {}
    validation_errors = validation.get("errors") if isinstance(validation, dict) else []
    validation_warnings = validation.get("warnings") if isinstance(validation, dict) else []
    return {
        "manifest_id": item["manifest_id"],
        "tenant_id": item["tenant_id"],
        "kind": item["kind"],
        "name": item["name"],
        "label": item["label"],
        "version": item["version"],
        "status": item["status"],
        "risk_level": item["risk_level"],
        "confirm_required": bool(item["confirm_required"]),
        "manifest": manifest,
        "runtime_status": normalized.get("runtime_status") or "registry_only",
        "intent": normalized.get("intent"),
        "aliases": normalized.get("aliases") or [],
        "similar_intents": normalized.get("similar_intents") or [],
        "required_slots": normalized.get("required_slots") or [],
        "runtime_type": normalized.get("runtime_type"),
        "step_count": normalized.get("step_count"),
        "validation": {
            "ok": bool(validation.get("ok")) if isinstance(validation, dict) else False,
            "errors": validation_errors or [],
            "warnings": validation_warnings or [],
        },
        "created_by": item["created_by"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def list_agent_manifest_imports(conn: sqlite3.Connection, tenant_id: str, include_inactive: bool = False) -> list[dict]:
    if include_inactive:
        query = "SELECT * FROM agent_manifest_imports WHERE tenant_id=? ORDER BY updated_at DESC"
        params = (tenant_id,)
    else:
        query = "SELECT * FROM agent_manifest_imports WHERE tenant_id=? AND status='ENABLED' ORDER BY updated_at DESC"
        params = (tenant_id,)
    return [serialize_agent_manifest_import(item) for item in rows(conn, query, params)]


def agent_manifest_validation_context(conn: sqlite3.Connection, user: dict, manifest: dict | None = None) -> dict:
    """Build tenant-aware semantic validation context for imported manifests."""
    builtin = public_agent_catalog()
    known_tools = {
        str(item.get("name") or "").strip()
        for item in (builtin.get("tools") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    builtin_intents = {
        str(item.get("name") or "").strip()
        for item in (builtin.get("intents") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    tenant_intents: dict[str, str] = {}
    for item in list_agent_manifest_imports(conn, user["tenant_id"], include_inactive=False):
        if item.get("kind") == "tool" and item.get("name"):
            known_tools.add(str(item["name"]))
        if item.get("kind") == "skill" and item.get("intent") and item.get("name"):
            tenant_intents[str(item["intent"])] = str(item["name"])
    metadata = manifest.get("metadata") if isinstance(manifest, dict) and isinstance(manifest.get("metadata"), dict) else {}
    return {
        "known_tools": known_tools,
        "builtin_intents": builtin_intents,
        "tenant_intents": tenant_intents,
        "current_manifest_name": str(metadata.get("name") or "").strip() or None,
    }


def _manifest_step_label(raw: str) -> str:
    match = re.search(r"skill\.execution\.steps\[(\d+)\](?:\.([A-Za-z_]+))?", raw)
    if match:
        index = match.group(1)
        field = match.group(2) or ""
        suffix = {"tool": "工具", "skill": "Skill"}.get(field, "配置")
        return f"第 {index} 个执行步骤的{suffix}"
    field_labels = {
        "metadata.name": "能力唯一标识",
        "metadata.label": "展示名称",
        "kind": "Manifest 类型",
        "schema_version": "Schema 版本",
        "risk.level": "风险等级",
        "risk.confirm_required": "执行前确认",
        "skill.intent.name": "Skill 关联意图",
        "skill.intent.aliases": "相似说法",
        "skill.execution.steps": "执行步骤",
        "tool.runtime.type": "工具运行方式",
        "tool.runtime.endpoint": "HTTP 接口地址",
        "tool.runtime.method": "HTTP 请求方法",
        "tool.input_schema": "工具输入 Schema",
        "tool.output_schema": "工具输出 Schema",
        "tool.runtime.auth": "工具认证配置",
        "tool.auth": "工具认证配置",
    }
    for key, label in field_labels.items():
        if key in raw:
            return label
    if raw.startswith("$."):
        return raw[2:]
    return "Manifest 配置"


def _manifest_diagnostic(level: str, code: str, raw: str, *, title: str, message: str, suggestion: str) -> dict:
    return {
        "level": level,
        "code": code,
        "field": _manifest_step_label(raw),
        "title": title,
        "message": message,
        "suggestion": suggestion,
        "raw": raw,
    }


def localize_agent_manifest_message(raw: str, level: str = "error") -> dict:
    raw = str(raw or "").strip()
    unknown_tool = re.search(r"skill\.execution\.steps\[(\d+)\]\.tool references unknown tool (.+)", raw)
    if unknown_tool:
        return _manifest_diagnostic(
            level,
            "UNKNOWN_TOOL",
            raw,
            title="执行步骤引用了未注册工具",
            message=f"第 {unknown_tool.group(1)} 个步骤配置了工具「{unknown_tool.group(2)}」，但当前工具箱里没有这个工具。",
            suggestion="请先在「执行工具」中注册该工具，或把该步骤改成目录里已有的工具名，例如 paas.media.snapshot、vlm.image.inspect、knowledge.retrieve。",
        )
    secret_field = re.search(r"^(.+?) must use credential_ref; raw secret values are not allowed", raw)
    if secret_field:
        return _manifest_diagnostic(
            level,
            "RAW_SECRET",
            raw,
            title="发现明文密钥",
            message=f"{_manifest_step_label(secret_field.group(1))} 中写入了明文密钥或 Token。",
            suggestion="请不要把 AppSecret、Token、api_key 直接写在 Manifest 里，先放入凭证库，然后在 Manifest 中只填写 credential_ref。",
        )
    if "HIGH_WRITE manifests must explicitly set risk.confirm_required=true" in raw:
        return _manifest_diagnostic(
            level,
            "CONFIRM_REQUIRED",
            raw,
            title="高风险写操作缺少二次确认",
            message="该 Manifest 被标记为 HIGH_WRITE，会创建、修改或写入业务系统，但没有声明执行前确认。",
            suggestion='请在 risk 中设置 "confirm_required": true，确保真正执行前由用户确认。',
        )
    conflict = re.search(r"skill\.intent\.name conflicts with builtin intent (.+?);", raw)
    if conflict:
        return _manifest_diagnostic(
            level,
            "INTENT_CONFLICT",
            raw,
            title="意图名称与系统内置意图冲突",
            message=f"意图「{conflict.group(1)}」已经是系统内置意图，不能被新的 Skill 覆盖。",
            suggestion="请给新 Skill 使用一个新的意图名称，并把内置意图放到 similar_intents 里建立关联。",
        )
    owner = re.search(r"skill\.intent\.name already belongs to imported skill (.+)", raw)
    if owner:
        return _manifest_diagnostic(
            level,
            "INTENT_DUPLICATED",
            raw,
            title="意图已经绑定到其他 Skill",
            message=f"这个意图已被「{owner.group(1)}」使用，继续导入会导致路由不确定。",
            suggestion="请更换 intent.name，或基于已有 Skill 编辑新版本。",
        )
    if "skill.intent.aliases is empty" in raw:
        return _manifest_diagnostic(
            level,
            "ALIASES_EMPTY",
            raw,
            title="缺少用户说法样例",
            message="Skill 没有配置 aliases，Agent 可能不容易把用户自然语言路由到该能力。",
            suggestion="请补充 2 到 5 条用户可能说出的自然语言，例如「看下门口有没有排队」「巡检竞品 Logo」。",
        )
    if "manifest must be a JSON object" in raw:
        return _manifest_diagnostic(
            level,
            "NOT_JSON_OBJECT",
            raw,
            title="Manifest 格式不是 JSON 对象",
            message="当前内容不是一个完整的 JSON 对象。",
            suggestion="请使用系统模板或自然语言生成草稿，再按 JSON 格式补齐字段。",
        )
    if "kind must be skill or tool" in raw:
        return _manifest_diagnostic(
            level,
            "INVALID_KIND",
            raw,
            title="Manifest 类型不正确",
            message='kind 只能填写 "skill" 或 "tool"。',
            suggestion="如果描述的是业务能力请选择 skill；如果描述的是可调用 API 或执行单元请选择 tool。",
        )
    if "schema_version must be" in raw:
        return _manifest_diagnostic(
            level,
            "SCHEMA_VERSION_INVALID",
            raw,
            title="Schema 版本与类型不匹配",
            message="schema_version 和 kind 不一致。",
            suggestion='Skill 请使用 "skill.v1"，工具请使用 "tool.v1"。',
        )
    if "metadata.name is required" in raw:
        return _manifest_diagnostic(
            level,
            "NAME_REQUIRED",
            raw,
            title="缺少能力唯一标识",
            message="metadata.name 不能为空，它用于版本管理、审计和路由索引。",
            suggestion="请填写英文、数字、点、下划线或中划线组成的唯一名称，例如 floor_cleanliness_check。",
        )
    if "metadata.name only supports" in raw:
        return _manifest_diagnostic(
            level,
            "NAME_INVALID",
            raw,
            title="能力唯一标识格式不合法",
            message="metadata.name 只能使用英文、数字、点、下划线和中划线。",
            suggestion="展示中文名称请写在 metadata.label；metadata.name 建议使用英文短标识。",
        )
    if "metadata.label is required" in raw:
        return _manifest_diagnostic(
            level,
            "LABEL_REQUIRED",
            raw,
            title="缺少展示名称",
            message="metadata.label 不能为空，用户会在能力中心看到这个名称。",
            suggestion="请填写一个清晰的中文名称，例如「门店地面清洁巡检」。",
        )
    if "risk.level must be one of" in raw:
        return _manifest_diagnostic(
            level,
            "RISK_INVALID",
            raw,
            title="风险等级不在允许范围内",
            message="risk.level 当前值无法识别。",
            suggestion="请使用 READ_ONLY、TRANSIENT_SESSION、HIGH_WRITE 或 DESIGN_ONLY。",
        )
    if "skill.intent.name is required" in raw:
        return _manifest_diagnostic(
            level,
            "INTENT_REQUIRED",
            raw,
            title="缺少 Skill 关联意图",
            message="Skill 必须声明 intent.name，Agent 才知道什么时候路由到它。",
            suggestion="请填写一个新的大写意图名，例如 CHECK_FLOOR_CLEANLINESS。",
        )
    if "skill.execution.steps must contain" in raw:
        return _manifest_diagnostic(
            level,
            "STEPS_REQUIRED",
            raw,
            title="缺少执行步骤",
            message="Skill 没有配置 execution.steps，Agent 不知道该如何执行。",
            suggestion="至少配置一个工具调用步骤，例如先抓图 paas.media.snapshot，再视觉判断 vlm.image.inspect。",
        )
    if "must reference tool or skill" in raw:
        return _manifest_diagnostic(
            level,
            "STEP_TARGET_REQUIRED",
            raw,
            title="执行步骤缺少调用目标",
            message="该执行步骤没有写 tool 或 skill。",
            suggestion='请为该步骤补充 "tool": "已有工具名" 或 "skill": "已有 Skill 名称"。',
        )
    if "tool.runtime.type must be" in raw:
        return _manifest_diagnostic(
            level,
            "RUNTIME_TYPE_INVALID",
            raw,
            title="工具运行方式不正确",
            message="工具 runtime.type 当前值无法识别。",
            suggestion="请使用 http、local、mcp 或 builtin。",
        )
    if "http tool.runtime.endpoint is required" in raw:
        return _manifest_diagnostic(
            level,
            "ENDPOINT_REQUIRED",
            raw,
            title="缺少 HTTP 接口地址",
            message="HTTP 工具必须声明 runtime.endpoint。",
            suggestion="请填写真实可调用的完整接口地址；没有 endpoint 时不能导入为可执行工具。",
        )
    if "http tool.runtime.endpoint must be a real callable endpoint" in raw:
        return _manifest_diagnostic(
            level,
            "ENDPOINT_PLACEHOLDER",
            raw,
            title="接口地址仍是占位值",
            message="当前工具使用了示例或占位 endpoint，导入后无法真正调用。",
            suggestion="请替换为真实服务地址，例如 https://your-domain/api/path；如果只是想使用内置工具，请选择内置工具模板。",
        )
    if "builtin tool.runtime.handler is required" in raw:
        return _manifest_diagnostic(
            level,
            "BUILTIN_HANDLER_REQUIRED",
            raw,
            title="内置工具缺少调用处理器",
            message="runtime.type 为 builtin 时必须声明 handler，系统才知道要调用哪个内置执行器。",
            suggestion="请填写当前工具箱已存在的 handler，例如 paas.media.snapshot、paas.camera.page 或 vlm.image.inspect。",
        )
    if "http tool.runtime.method is invalid" in raw:
        return _manifest_diagnostic(
            level,
            "METHOD_INVALID",
            raw,
            title="HTTP 请求方法不合法",
            message="runtime.method 不是支持的方法。",
            suggestion="请使用 GET、POST、PUT、PATCH 或 DELETE。",
        )
    if "tool.input_schema must be an object" in raw:
        return _manifest_diagnostic(
            level,
            "INPUT_SCHEMA_INVALID",
            raw,
            title="工具输入 Schema 格式不正确",
            message="input_schema 必须是 JSON 对象，用于校验调用入参。",
            suggestion='请至少填写 {"type":"object","required":["tenant_id"]} 这样的结构。',
        )
    if "tool.output_schema must be an object" in raw:
        return _manifest_diagnostic(
            level,
            "OUTPUT_SCHEMA_INVALID",
            raw,
            title="工具输出 Schema 格式不正确",
            message="output_schema 必须是 JSON 对象，用于描述工具返回结果。",
            suggestion='请至少填写 {"type":"object","required":["result"]} 这样的结构。',
        )
    if "tool.auth is not supported at top level" in raw:
        return _manifest_diagnostic(
            level,
            "TOP_AUTH_INVALID",
            raw,
            title="认证字段放错位置",
            message="工具认证不能写在顶层 auth 字段。",
            suggestion='请把认证引用移动到 runtime.auth，例如 "runtime": {"auth": {"credential_ref": "xxx"}}。',
        )
    if "tool.runtime.auth must use credential_ref" in raw:
        return _manifest_diagnostic(
            level,
            "RAW_API_KEY",
            raw,
            title="HTTP 工具使用了明文 api_key",
            message="runtime.auth 中不能直接写 api_key。",
            suggestion="请把密钥保存到凭证库，Manifest 中仅引用 credential_ref。",
        )
    if "write-like tools should require confirmation" in raw:
        return _manifest_diagnostic(
            level,
            "WRITE_CONFIRM_RECOMMENDED",
            raw,
            title="建议为写操作开启确认",
            message="这个工具可能会修改外部系统，但没有开启执行前确认。",
            suggestion='建议设置 "confirm_required": true，降低误操作风险。',
        )
    return _manifest_diagnostic(
        level,
        "MANIFEST_CHECK",
        raw,
        title="配置需要调整",
        message=raw,
        suggestion="请根据字段位置检查 Manifest；也可以使用自然语言重新生成草稿。",
    )


def localize_agent_manifest_validation(validation: dict) -> dict:
    validation = dict(validation or {})
    errors = [str(item) for item in (validation.get("errors") or []) if str(item).strip()]
    warnings = [str(item) for item in (validation.get("warnings") or []) if str(item).strip()]
    diagnostics = [localize_agent_manifest_message(item, "error") for item in errors]
    diagnostics.extend(localize_agent_manifest_message(item, "warning") for item in warnings)
    validation["diagnostics"] = diagnostics
    validation["error_summary"] = "校验通过" if validation.get("ok") else f"发现 {len(errors)} 个必须修复的问题"
    validation["warning_summary"] = f"{len(warnings)} 个建议优化项" if warnings else ""
    return validation


def validate_agent_manifest_for_user(conn: sqlite3.Connection, user: dict, manifest: dict) -> dict:
    validation = validate_agent_manifest(manifest, **agent_manifest_validation_context(conn, user, manifest))
    return localize_agent_manifest_validation(validation)


def _manifest_slug(text: str, fallback: str) -> str:
    ascii_tokens = [token for token in re.findall(r"[A-Za-z0-9]+", text.lower()) if not token.isdigit()]
    slug = "_".join(ascii_tokens[:5]) if ascii_tokens else fallback
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", slug).strip("._-")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"{slug or fallback}_{digest}"


def _manifest_has_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


VISUAL_CONDITION_RULES = [
    (
        "员工吃东西",
        ["员工吃东西", "员工进食", "员工吃饭", "员工用餐", "员工吃零食", "店员吃东西", "店员进食", "店员用餐", "吃东西"],
    ),
    (
        "广告牌灯箱未开",
        ["广告牌灯箱未开", "广告灯箱未开", "广告牌未开", "广告牌未亮", "灯箱未开", "灯箱没开", "灯箱关闭", "灯箱未亮"],
    ),
    (
        "电视屏幕关闭",
        ["电视屏幕关闭", "电视屏幕未开", "电视屏幕未亮", "电视屏幕黑屏", "电视关闭", "电视未开", "电视没开", "电视黑屏", "屏幕关闭"],
    ),
    (
        "其他品牌 Logo 或宣传海报",
        ["其他品牌logo", "其他品牌 logo", "竞品logo", "竞品 logo", "非本品牌logo", "非本品牌 logo", "其他品牌宣传", "竞品宣传", "竞品海报", "非本品牌宣传", "品牌露出", "品牌logo", "品牌 logo", "宣传海报"],
    ),
    (
        "地面垃圾、污渍或散落杂物",
        ["地面垃圾", "垃圾", "污渍", "脏污", "杂物", "地面不干净", "地面清洁", "清洁标准"],
    ),
    (
        "指定物体或物料摆放不合规",
        ["统一风格的座椅", "指定物体", "物体内容", "按要求放置", "摆放", "座椅", "立牌", "展架", "物料"],
    ),
    (
        "消防通道占用或堵塞",
        ["消防通道", "通道堵塞", "占用通道", "消防", "堵塞"],
    ),
]


def extract_manifest_visual_conditions(prompt: str) -> list[str]:
    conditions: list[str] = []
    for label, keywords in VISUAL_CONDITION_RULES:
        if _manifest_has_any(prompt, keywords) and label not in conditions:
            conditions.append(label)
    return conditions


def infer_manifest_requirement(prompt: str) -> dict:
    conditions = extract_manifest_visual_conditions(prompt)
    scheduled = bool(
        _manifest_has_any(
            prompt,
            ["每天", "每隔", "定时", "周期", "每周", "每月", "早上", "上午", "下午", "晚上", "持续", "为期", "月底", "截止", "直到"],
        )
        or re.search(r"每\s*\d+\s*(分钟|小时|天|周|月)", prompt)
        or re.search(r"(早上|上午|中午|下午|晚上)?\s*\d{1,2}\s*点", prompt)
    )
    all_cameras = _manifest_has_any(prompt, ["所有镜头", "全部镜头", "所有摄像头", "全部摄像头", "所有监控", "全部监控", "店内所有", "全店", "所有"])
    brand = "其他品牌 Logo 或宣传海报" in conditions
    floor = "地面垃圾、污渍或散落杂物" in conditions
    object_check = "指定物体或物料摆放不合规" in conditions
    fire = "消防通道占用或堵塞" in conditions
    operation = any(item in conditions for item in ["员工吃东西", "广告牌灯箱未开", "电视屏幕关闭"])
    if operation or len(conditions) >= 2:
        category = "operation_compliance"
        base_name = "store_operation_compliance_check"
        label = "门店运营合规巡检"
        intent = "CHECK_STORE_OPERATION_COMPLIANCE"
        aliases = ["巡检店内运营合规", "检查员工行为和设备展示状态", "看店内是否有不符合要求的情况"]
        goal = f"识别店内画面中是否存在：{'、'.join(conditions)}。" if conditions else prompt
    elif brand:
        category = "brand_compliance"
        base_name = "brand_compliance_check"
        label = "品牌露出合规巡检"
        intent = "CHECK_BRAND_COMPLIANCE"
        aliases = ["检查竞品 Logo", "巡检其他品牌海报", "看门店是否有非本品牌宣传"]
        goal = "识别门店画面中是否存在不符合要求的品牌 Logo、宣传海报或广告内容。"
    elif floor:
        category = "floor_cleanliness"
        base_name = "floor_cleanliness_check"
        label = "地面清洁巡检"
        intent = "CHECK_FLOOR_CLEANLINESS"
        aliases = ["看地面有没有垃圾", "巡检门店地面清洁", "检查地面污渍"]
        goal = "识别门店地面是否存在垃圾、污渍、散落杂物，并排除固定标识和正常陈列物。"
    elif object_check:
        category = "object_placement"
        base_name = "object_placement_check"
        label = "指定物体摆放巡检"
        intent = "CHECK_OBJECT_PLACEMENT"
        aliases = ["检查指定物体摆放", "看门店物料是否符合要求"]
        goal = "识别门店是否按要求放置指定物体、物料、展架或播放指定内容。"
    elif fire:
        category = "fire_lane_block"
        base_name = "fire_lane_block_check"
        label = "消防通道占用检测"
        intent = "CHECK_FIRE_LANE_BLOCK"
        aliases = ["看消防通道有没有堵", "巡检消防通道占用"]
        goal = "识别消防通道是否被杂物、车辆或货物占用。"
    else:
        category = "custom_visual"
        base_name = "custom_visual_check"
        label = "自定义视觉巡检"
        intent = "CUSTOM_VISUAL_CHECK"
        aliases = [prompt[:30]]
        goal = prompt
    return {
        "category": category,
        "scheduled": scheduled,
        "all_cameras": all_cameras,
        "conditions": conditions,
        "base_name": base_name,
        "label": label,
        "intent": intent,
        "aliases": aliases,
        "goal": goal,
        "needs_knowledge": bool(brand or object_check or operation),
        "needs_visual_compliance": bool(brand or object_check or operation),
    }


def infer_manifest_draft_from_prompt(conn: sqlite3.Connection, user: dict, prompt: str, kind: str) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    prompt = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if len(prompt) < 6:
        raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "请用一句话描述要创建的 Skill 或工具，例如“创建一个每天检查门店竞品 Logo 的巡检 Skill”。"})
    kind = str(kind or "").strip().lower()
    if kind not in {"skill", "tool"}:
        kind = "tool" if _manifest_has_any(prompt, ["接口", "api", "http", "mcp", "token", "endpoint", "工具"]) else "skill"
    if kind == "tool":
        manifest, guide = build_tool_manifest_draft(prompt)
    else:
        manifest, guide = build_skill_manifest_draft(prompt)
    validation = validate_agent_manifest_for_user(conn, user, manifest)
    return {"kind": kind, "manifest": manifest, "validation": validation, "guide": guide}


def build_skill_manifest_draft(prompt: str) -> tuple[dict, dict]:
    requirement = infer_manifest_requirement(prompt)
    scheduled = requirement["scheduled"]
    similar_intents = ["ANALYZE_VISUAL"]
    if scheduled:
        similar_intents.append("CREATE_SCHEDULED_INSPECTION")
    if requirement["needs_visual_compliance"]:
        similar_intents.append("VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE")
    steps = []
    if requirement["all_cameras"]:
        steps.append({"tool": "paas.camera.page", "purpose": "按门店范围获取候选摄像头"})
    steps.append({"tool": "paas.media.snapshot", "purpose": "抓取目标摄像头快照"})
    if requirement["needs_knowledge"]:
        steps.append({"tool": "knowledge.retrieve", "purpose": "召回业务规则、参考物料、品牌规范或门店运营标准"})
    steps.extend(
        [
            {"tool": "vlm.image.inspect", "purpose": "基于画面和业务目标执行视觉判断"},
            {"tool": "evidence.archive", "purpose": "归档证据图片、异常标记和判断依据"},
            {"tool": "event.emit", "purpose": "输出巡检结论和可追溯事件"},
        ]
    )
    required_slots = ["org_scope", "camera_ids", "inspection_goal"]
    if scheduled:
        required_slots.extend(["schedule", "effective_time_range"])
    risk_level = "HIGH_WRITE" if scheduled else "READ_ONLY"
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:6].upper()
    optional_slots = ["roi", "thresholds", "target_conditions", "reference_materials"]
    if requirement["needs_visual_compliance"]:
        optional_slots.append("target_brand_list")
    manifest = {
        "kind": "skill",
        "schema_version": "skill.v1",
        "metadata": {
            "name": _manifest_slug(prompt, requirement["base_name"]),
            "label": requirement["label"],
            "version": "1.0.0",
            "description": requirement["goal"],
        },
        "intent": {
            "name": f"{requirement['intent']}_{digest}",
            "aliases": requirement["aliases"],
            "similar_intents": list(dict.fromkeys(similar_intents)),
        },
        "slots": {
            "required": list(dict.fromkeys(required_slots)),
            "optional": list(dict.fromkeys(optional_slots)),
        },
        "execution": {"mode": "workflow", "steps": steps},
        "risk": {"level": risk_level, "confirm_required": risk_level == "HIGH_WRITE"},
    }
    parsed_conditions = requirement["conditions"] or [requirement["goal"]]
    guide = {
        "title": "已生成 Skill 草稿",
        "parsed": [
            "识别为周期巡检需求" if scheduled else "识别为即时/可复用巡检能力",
            f"Skill 类型：{requirement['label']}",
            f"识别目标：{'、'.join(parsed_conditions)}",
            "需要召回知识库/运营规范作为判断标准" if requirement["needs_knowledge"] else "主要依赖实时画面判断",
            f"风险等级：{risk_level}",
        ],
        "assumptions": ["摄像头范围、ROI 和阈值可在导入前继续补充。", "自然语言生成只创建草稿，导入前仍必须通过 Manifest 校验。"],
        "next_steps": ["检查 aliases 是否贴近用户真实说法", "确认 execution.steps 中每个工具都已在工具箱可见", "点击校验后再导入目录"],
    }
    return manifest, guide


def build_tool_manifest_draft(prompt: str) -> tuple[dict, dict]:
    url_match = re.search(r"https?://[^\s，,；;]+", prompt)
    method_match = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\b", prompt, re.IGNORECASE)
    method = method_match.group(1).upper() if method_match else ("GET" if _manifest_has_any(prompt, ["查询", "读取", "只读", "获取"]) else "POST")
    ticket = _manifest_has_any(prompt, ["工单", "ticket"])
    snapshot = _manifest_has_any(prompt, ["抓图", "快照", "截图", "抽帧", "监控画面", "摄像头画面"])
    camera_page = _manifest_has_any(prompt, ["摄像头列表", "镜头列表", "获取摄像头", "查询摄像头", "门店镜头", "所有镜头"])
    live_stream = _manifest_has_any(prompt, ["直播", "取流", "拉流", "视频流", "rtsp", "hls", "flv"])
    vision_model = _manifest_has_any(prompt, ["视觉模型", "大模型", "vlm", "图片分析", "图像分析", "视觉分析"])
    knowledge = _manifest_has_any(prompt, ["知识库", "召回", "检索", "sop", "规范"])
    endpoint = url_match.group(0) if url_match else ""
    builtin_handler = ""
    if not endpoint:
        if camera_page:
            builtin_handler = "paas.camera.page"
            label = "摄像头列表工具"
            fallback_name = "camera_page_tool"
            required = ["tenant_id", "org_id"]
            output_required = ["cameras"]
        elif snapshot:
            builtin_handler = "paas.media.snapshot"
            label = "摄像头快照工具"
            fallback_name = "camera_snapshot_tool"
            required = ["tenant_id", "org_id", "camera_id"]
            output_required = ["snapshot_url", "captured_at"]
        elif live_stream:
            builtin_handler = "paas.media.live.start"
            label = "摄像头取流工具"
            fallback_name = "camera_stream_tool"
            required = ["tenant_id", "org_id", "camera_id"]
            output_required = ["stream_url", "expires_at"]
        elif vision_model:
            builtin_handler = "vlm.image.inspect"
            label = "视觉模型分析工具"
            fallback_name = "visual_model_tool"
            required = ["images", "prompt"]
            output_required = ["result", "confidence"]
        elif knowledge:
            builtin_handler = "knowledge.retrieve"
            label = "知识库检索工具"
            fallback_name = "knowledge_retrieve_tool"
            required = ["tenant_id", "query"]
            output_required = ["items"]
        else:
            label = "外部 API 工具"
            fallback_name = "external_api_tool"
            required = ["tenant_id", "query"]
            output_required = ["result"]
    elif ticket:
        label = "外部工单创建工具"
        fallback_name = "external_ticket_create"
        required = ["tenant_id", "org_id", "event_id", "summary"]
        output_required = ["ticket_id", "status"]
    else:
        label = "外部 API 工具"
        fallback_name = "external_api_tool"
        required = ["tenant_id", "query"]
        output_required = ["result"]
    runtime = {
        "type": "builtin",
        "handler": builtin_handler,
        "timeout_ms": 8000,
    } if builtin_handler else {
        "type": "http",
        "method": method,
        "endpoint": endpoint or "https://example.com/api/replace-me",
        "auth": {"credential_ref": "external_api_token"},
        "timeout_ms": 8000,
    }
    if builtin_handler in {"paas.media.snapshot", "paas.media.live.start"}:
        risk_level = "TRANSIENT_SESSION"
        confirm_required = False
    elif builtin_handler:
        risk_level = "READ_ONLY"
        confirm_required = False
    elif ticket or method in {"POST", "PUT", "PATCH", "DELETE"}:
        risk_level = "HIGH_WRITE"
        confirm_required = True
    else:
        risk_level = "READ_ONLY"
        confirm_required = False
    manifest = {
        "kind": "tool",
        "schema_version": "tool.v1",
        "metadata": {
            "name": _manifest_slug(prompt, fallback_name),
            "label": label,
            "version": "1.0.0",
            "description": prompt[:120],
        },
        "runtime": runtime,
        "input_schema": {"type": "object", "required": required},
        "output_schema": {"type": "object", "required": output_required},
        "risk": {"level": risk_level, "confirm_required": confirm_required},
    }
    if builtin_handler:
        manifest["metadata"]["description"] = f"{prompt[:90]}（绑定内置 handler：{builtin_handler}）"
    guide = {
        "title": "已生成工具草稿",
        "parsed": [
            f"工具类型：{label}",
            f"运行方式：{'builtin' if builtin_handler else 'http'}",
            f"调用目标：{builtin_handler or endpoint or '待补充真实 endpoint'}",
            f"风险等级：{manifest['risk']['level']}",
        ],
        "assumptions": [
            "自然语言生成的工具必须具备真实可调用目标；缺少 endpoint 的外部 API 会被校验拦截。",
            "接口鉴权默认使用 credential_ref，不会保存明文密钥。",
        ],
        "next_steps": ["确认调用目标是否真实可用", "把密钥先写入凭证库并替换 credential_ref", "点击校验后再导入目录"],
    }
    return manifest, guide


def agent_catalog_payload(conn: sqlite3.Connection, user: dict) -> dict:
    builtin = public_agent_catalog()
    extensions = list_agent_manifest_imports(conn, user["tenant_id"], include_inactive=False)
    skills = [item for item in extensions if item["kind"] == "skill"]
    tools = [item for item in extensions if item["kind"] == "tool"]
    memories = list_agent_memories(conn, user, limit=20)
    knowledge_items = list_agent_knowledge_items(conn, user, limit=20)
    try:
        web_search = public_web_search_config(conn, user["tenant_id"])
    except ApiError:
        web_search = {"configured": False, "provider": None, "max_results": 5}
    return {
        "catalog": builtin,
        "extensions": extensions,
        "web_search": web_search,
        "memory": {
            "items": memories,
            "contract": "长期记忆用于沉淀用户偏好、别名和业务判断口径；默认只进入检索上下文，不自动改写业务结果。",
        },
        "knowledge": {
            "items": knowledge_items,
            "contract": "知识库用于沉淀 SOP、品牌规范、参考图片链接、门店平面图等多模态资料，后续执行链路可检索引用。",
        },
        "summary": {
            "builtin_intents": len(builtin.get("intents") or []),
            "builtin_skills": len(builtin.get("skills") or []),
            "builtin_tools": len(builtin.get("tools") or []),
            "imported_skills": len(skills),
            "imported_tools": len(tools),
            "memory_items": len(memories),
            "knowledge_items": len(knowledge_items),
            "web_search_configured": bool(web_search.get("configured")),
            "tenant_id": user["tenant_id"],
            "generated_at": now_iso(),
        },
        "evaluability": {
            "trace_contract": "Each execution can expose intent, skill route, tool calls, model output, rule review, and final output.",
            "manifest_contract": "Imported manifests are schema-validated, risk-rated, versioned, audited, and must bind callable tools or runtimes before they enter the executable catalog.",
            "metrics": [
                "intent_hit_rate",
                "slot_completion_rate",
                "tool_success_rate",
                "model_confidence",
                "business_review_result",
                "memory_hit_rate",
                "knowledge_recall_rate",
            ],
        },
        "templates": agent_manifest_templates(),
    }


def agent_manifest_templates() -> dict:
    return {
        "skill": {
            "kind": "skill",
            "schema_version": "skill.v1",
            "metadata": {
                "name": "fire_lane_block_check",
                "label": "消防通道占用检测",
                "version": "1.0.0",
                "description": "识别消防通道是否被杂物、车辆或货物占用。",
            },
            "intent": {
                "name": "CHECK_FIRE_LANE_BLOCK",
                "aliases": ["看下消防通道有没有堵", "巡检消防通道占用"],
                "similar_intents": ["ANALYZE_VISUAL", "CREATE_SCHEDULED_INSPECTION"],
            },
            "slots": {
                "required": ["org_scope", "camera_ids", "inspection_goal"],
                "optional": ["roi", "schedule", "thresholds"],
            },
            "execution": {
                "mode": "workflow",
                "steps": [
                    {"tool": "paas.media.snapshot", "purpose": "抓取目标点位快照"},
                    {"tool": "vlm.image.inspect", "purpose": "识别通道占用"},
                    {"tool": "event.emit", "purpose": "输出巡检结果和证据"},
                ],
            },
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        },
        "tool": {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {
                "name": "external.ticket.create",
                "label": "外部工单创建",
                "version": "1.0.0",
                "description": "将已确认异常同步到客户工单系统。",
            },
            "runtime": {
                "type": "http",
                "method": "POST",
                "endpoint": "https://example.com/api/tickets",
                "auth": {"credential_ref": "ticket_system_token"},
                "timeout_ms": 8000,
            },
            "input_schema": {
                "type": "object",
                "required": ["tenant_id", "org_id", "event_id", "summary"],
            },
            "output_schema": {
                "type": "object",
                "required": ["ticket_id", "status"],
            },
            "risk": {"level": "HIGH_WRITE", "confirm_required": True},
        },
    }


def create_agent_manifest_import(conn: sqlite3.Connection, user: dict, manifest: dict) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    validation = validate_agent_manifest_for_user(conn, user, manifest)
    if not validation.get("ok"):
        raise ApiError("AGENT_MANIFEST_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "Manifest 校验未通过", "validation": validation})
    normalized = validation["normalized"]
    manifest_id = f"mf_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        "UPDATE agent_manifest_imports SET status='SUPERSEDED', updated_at=? WHERE tenant_id=? AND kind=? AND name=? AND status='ENABLED'",
        (timestamp, user["tenant_id"], normalized["kind"], normalized["name"]),
    )
    conn.execute(
        """INSERT INTO agent_manifest_imports(
             manifest_id, tenant_id, kind, name, label, version, status, risk_level,
             confirm_required, manifest_json, validation_json, created_by, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            manifest_id,
            user["tenant_id"],
            normalized["kind"],
            normalized["name"],
            normalized["label"],
            normalized["version"],
            "ENABLED",
            normalized["risk_level"],
            1 if normalized["confirm_required"] else 0,
            json_dumps(manifest),
            json_dumps(validation),
            user["user_id"],
            timestamp,
            timestamp,
        ),
    )
    created = one(conn, "SELECT * FROM agent_manifest_imports WHERE manifest_id=?", (manifest_id,))
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.manifest.import",
        "agent_manifest",
        manifest_id,
        None,
        {
            "kind": normalized["kind"],
            "name": normalized["name"],
            "version": normalized["version"],
            "risk_level": normalized["risk_level"],
            "runtime_status": normalized["runtime_status"],
        },
        "agent_catalog",
        None,
    )
    return serialize_agent_manifest_import(created)


def delete_agent_manifest_import(conn: sqlite3.Connection, user: dict, manifest_id: str) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    item = one(
        conn,
        "SELECT * FROM agent_manifest_imports WHERE manifest_id=? AND tenant_id=? AND status='ENABLED'",
        (manifest_id, user["tenant_id"]),
    )
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    timestamp = now_iso()
    conn.execute(
        "UPDATE agent_manifest_imports SET status='DELETED', updated_at=? WHERE manifest_id=? AND tenant_id=?",
        (timestamp, manifest_id, user["tenant_id"]),
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.manifest.delete",
        "agent_manifest",
        manifest_id,
        {"status": item["status"], "kind": item["kind"], "name": item["name"], "version": item["version"]},
        {"status": "DELETED", "deleted_at": timestamp},
        "agent_catalog",
        None,
    )
    deleted = dict_row(item)
    deleted["status"] = "DELETED"
    deleted["updated_at"] = timestamp
    return serialize_agent_manifest_import(deleted)


MEMORY_CATEGORIES = {
    "alias": "别名",
    "preference": "偏好",
    "business_rule": "业务判断口径",
    "conversation_style": "对话习惯",
}
MEMORY_SCOPES = {"tenant", "user", "store"}
KNOWLEDGE_TYPES = {
    "sop": "SOP",
    "brand_standard": "品牌规范",
    "reference_material": "参考物料",
    "floor_plan": "门店平面图",
    "policy": "管理制度",
}
KNOWLEDGE_MODALITIES = {"text", "image", "document", "video", "floor_plan"}


def _split_text_list(value) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[,，\n;；]+", str(value or ""))
    return [str(item).strip() for item in raw_items if str(item).strip()]


def knowledge_image_bytes_match_mime(content: bytes, mime_type: str) -> bool:
    if mime_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return False


def remove_uploaded_knowledge_asset(asset_url: str, tenant_id: str) -> None:
    tenant_folder = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tenant_id)[:80] or "tenant"
    prefix = f"/static/uploads/knowledge/{tenant_folder}/"
    if not asset_url.startswith(prefix):
        return
    filename = Path(asset_url).name
    if not re.fullmatch(r"ka_[a-f0-9]{16}\.(?:jpg|png|webp|gif)", filename):
        return
    storage_path = KNOWLEDGE_UPLOAD_DIR / tenant_folder / filename
    if storage_path.is_file():
        try:
            storage_path.unlink()
        except OSError:
            pass


def knowledge_asset_urls(item: dict) -> list[str]:
    stored_urls = json_loads(item.get("asset_urls_json"), [])
    serialized_urls = item.get("asset_urls")
    candidates = [
        item.get("asset_url"),
        *(serialized_urls if isinstance(serialized_urls, list) else []),
        *(stored_urls if isinstance(stored_urls, list) else []),
    ]
    return list(dict.fromkeys(str(url).strip() for url in candidates if str(url or "").strip()))


def normalize_knowledge_reference_assets(
    asset_urls: list[str], default_sku: str, raw_metadata,
) -> list[dict]:
    """Return one bounded, safe metadata record for every retained reference image.

    ``asset_urls_json`` remains the compatibility source of truth for older knowledge
    records.  The additive metadata column assigns a SKU and visual context to an
    individual reference image, which is essential when one knowledge item contains
    multiple products or multiple views of the same product.
    """
    urls = list(dict.fromkeys(str(url or "").strip() for url in asset_urls if str(url or "").strip()))
    default_sku = str(default_sku or "").strip().upper()
    by_url: dict[str, dict] = {}
    if isinstance(raw_metadata, list):
        for entry in raw_metadata[:MAX_KNOWLEDGE_IMAGE_BATCH_COUNT]:
            if not isinstance(entry, dict):
                continue
            asset_url = str(entry.get("asset_url") or "").strip()
            if asset_url not in urls or asset_url in by_url:
                continue
            sku = str(entry.get("sku") or "").strip().upper()
            if sku and not KNOWLEDGE_SKU_LABEL_PATTERN.fullmatch(sku):
                continue
            by_url[asset_url] = {
                "asset_url": asset_url,
                "sku": sku or default_sku,
                "description": str(entry.get("description") or "").strip()[:800],
                "view_tag": str(entry.get("view_tag") or "").strip()[:80],
            }
    return [
        by_url.get(
            asset_url,
            {
                "asset_url": asset_url,
                "sku": default_sku,
                "description": "",
                "view_tag": "",
            },
        )
        for asset_url in urls
    ]


def knowledge_reference_assets(item: dict) -> list[dict]:
    return normalize_knowledge_reference_assets(
        knowledge_asset_urls(item),
        str(item.get("sku") or ""),
        item.get("reference_assets")
        if isinstance(item.get("reference_assets"), list)
        else json_loads(item.get("asset_metadata_json"), []),
    )


def knowledge_asset_uploads_from_payload(payload: dict):
    uploads = []
    legacy_upload = payload.get("asset_upload")
    if legacy_upload is not None:
        uploads.append(legacy_upload)
    batch_uploads = payload.get("asset_uploads")
    if batch_uploads is not None:
        if not isinstance(batch_uploads, list):
            return None
        uploads.extend(batch_uploads)
    return uploads


def serialize_agent_memory(item: dict) -> dict:
    return {
        "memory_id": item["memory_id"],
        "tenant_id": item["tenant_id"],
        "user_id": item["user_id"],
        "scope": item["scope"],
        "category": item["category"],
        "category_label": MEMORY_CATEGORIES.get(item["category"], item["category"]),
        "key": item["memory_key"],
        "value": item["memory_value"],
        "aliases": json_loads(item.get("aliases_json"), []),
        "confidence": item["confidence"],
        "source": item["source"],
        "status": item["status"],
        "created_by": item["created_by"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def serialize_agent_knowledge(item: dict) -> dict:
    reference_assets = knowledge_reference_assets(item)
    return {
        "knowledge_id": item["knowledge_id"],
        "tenant_id": item["tenant_id"],
        "title": item["title"],
        "sku": str(item.get("sku") or ""),
        "knowledge_type": item["knowledge_type"],
        "knowledge_type_label": KNOWLEDGE_TYPES.get(item["knowledge_type"], item["knowledge_type"]),
        "modality": item["modality"],
        "content_text": item["content_text"],
        "asset_url": item.get("asset_url"),
        "asset_urls": [asset["asset_url"] for asset in reference_assets],
        "reference_assets": reference_assets,
        "tags": json_loads(item.get("tags_json"), []),
        "source": item["source"],
        "status": item["status"],
        "created_by": item["created_by"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def list_agent_memories(conn: sqlite3.Connection, user: dict, limit: int = 50) -> list[dict]:
    items = rows(
        conn,
        """SELECT * FROM agent_memories
           WHERE tenant_id=? AND status='ACTIVE'
           ORDER BY updated_at DESC LIMIT ?""",
        (user["tenant_id"], limit),
    )
    return [serialize_agent_memory(item) for item in items]


def list_agent_knowledge_items(conn: sqlite3.Connection, user: dict, limit: int = 50) -> list[dict]:
    items = rows(
        conn,
        """SELECT * FROM agent_knowledge_items
           WHERE tenant_id=? AND status='ACTIVE'
           ORDER BY updated_at DESC LIMIT ?""",
        (user["tenant_id"], limit),
    )
    return [serialize_agent_knowledge(item) for item in items]


def _knowledge_match_text(value) -> str:
    """Normalize text for tenant-local knowledge title matching."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _knowledge_ngrams(value: str, minimum: int = 2, maximum: int = 6) -> set[str]:
    compact = _knowledge_match_text(value)
    terms: set[str] = set()
    for start in range(len(compact)):
        for size in range(minimum, min(maximum, len(compact) - start) + 1):
            terms.add(compact[start : start + size])
    return terms


def _knowledge_retrieval_item(item: dict) -> dict:
    """Keep only the tenant-scoped fields that can enter an inspection prompt/trace."""
    return {
        "knowledge_id": item["knowledge_id"],
        # Store the source revision with a scheduled task.  A task may live for days,
        # while its reference images and per-image SKU labels are maintained later in
        # the knowledge base.  The revision lets execution detect and refresh an old
        # task snapshot before it is sent to the visual model.
        "knowledge_updated_at": str(item.get("updated_at") or ""),
        "title": item["title"],
        "sku": str(item.get("sku") or ""),
        "knowledge_type": item["knowledge_type"],
        "modality": item["modality"],
        "content_text": str(item.get("content_text") or "")[:2000],
        "tags": [str(tag)[:80] for tag in (item.get("tags") or [])[:20]],
        "asset_urls": knowledge_asset_urls(item),
        "reference_assets": knowledge_reference_assets(item),
    }


def retrieve_agent_knowledge(conn: sqlite3.Connection, user: dict, query: str, limit: int = MAX_INSPECTION_KNOWLEDGE_HITS) -> list[dict]:
    """Retrieve relevant active knowledge without ever crossing tenant boundaries.

    Full title matches deliberately dominate. This covers the normal interaction where a
    user names a knowledge item in quotation marks, while short shared phrases (such as
    \"样板图\") need more than a single weak overlap before they are admitted.
    """
    query_text = _knowledge_match_text(query)
    if len(query_text) < 2:
        return []
    query_terms = _knowledge_ngrams(query_text)
    ranked: list[tuple[int, dict]] = []
    for item in list_agent_knowledge_items(conn, user, limit=100):
        title_text = _knowledge_match_text(item.get("title"))
        content_text = _knowledge_match_text(item.get("content_text"))
        tags_text = _knowledge_match_text(" ".join(str(tag) for tag in item.get("tags") or []))
        if not title_text:
            continue
        score = 0
        if title_text in query_text:
            score += 100
        title_overlap = query_terms & _knowledge_ngrams(title_text)
        long_title_overlap = [term for term in title_overlap if len(term) >= 3]
        if long_title_overlap:
            score += max(len(term) for term in long_title_overlap) * 3
        elif len(title_overlap) >= 2:
            score += 4
        for source_text, weight in ((tags_text, 2), (content_text, 1)):
            if not source_text:
                continue
            overlap = query_terms & _knowledge_ngrams(source_text)
            if overlap:
                score += min(12, max(len(term) for term in overlap) * weight)
        if score >= 6:
            ranked.append((score, _knowledge_retrieval_item(item)))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]["title"], entry[1]["knowledge_id"]))
    return [item for _, item in ranked[: max(1, min(limit, MAX_INSPECTION_KNOWLEDGE_HITS))]]


def inspection_knowledge_hits(task: dict) -> list[dict]:
    thresholds = task.get("thresholds") if isinstance(task.get("thresholds"), dict) else json_loads(task.get("thresholds"), {})
    raw_hits = thresholds.get("knowledge_context") if isinstance(thresholds, dict) else []
    if not isinstance(raw_hits, list):
        return []
    hits = []
    for item in raw_hits[:MAX_INSPECTION_KNOWLEDGE_HITS]:
        if not isinstance(item, dict) or not item.get("knowledge_id") or not item.get("title"):
            continue
        asset_urls = list(
            dict.fromkeys(str(url) for url in (item.get("asset_urls") or []) if str(url))
        )
        reference_assets = normalize_knowledge_reference_assets(
            asset_urls,
            str(item.get("sku") or ""),
            item.get("reference_assets")
            if isinstance(item.get("reference_assets"), list)
            else item.get("asset_metadata"),
        )
        hits.append(
            {
                "knowledge_id": str(item["knowledge_id"]),
                "knowledge_updated_at": str(item.get("knowledge_updated_at") or ""),
                "title": str(item["title"])[:160],
                "sku": str(item.get("sku") or "")[:64],
                "knowledge_type": str(item.get("knowledge_type") or ""),
                "modality": str(item.get("modality") or ""),
                "content_text": str(item.get("content_text") or "")[:2000],
                "tags": [str(tag)[:80] for tag in (item.get("tags") or [])[:20]],
                "asset_urls": [asset["asset_url"] for asset in reference_assets],
                "reference_assets": reference_assets,
            }
        )
    return hits


def inspection_knowledge_context_is_stale(
    conn: sqlite3.Connection,
    tenant_id: str,
    hits: list[dict],
) -> bool:
    """Return whether a task's stored knowledge snapshot differs from active source data.

    Scheduled inspections persist their retrieval results for auditability.  That
    should not make a task silently keep pre-SKU reference metadata after a user has
    corrected the knowledge item.  Missing revisions are intentionally considered
    stale so all tasks created before this field was introduced are repaired on their
    next execution.
    """
    knowledge_ids = [str(item.get("knowledge_id") or "") for item in hits if item.get("knowledge_id")]
    if not knowledge_ids:
        return False
    active_rows = rows(
        conn,
        f"""SELECT knowledge_id, updated_at FROM agent_knowledge_items
            WHERE tenant_id=? AND status='ACTIVE'
              AND knowledge_id IN ({','.join('?' for _ in knowledge_ids)})""",
        (tenant_id, *knowledge_ids),
    )
    active_revisions = {
        str(item["knowledge_id"]): str(item.get("updated_at") or "")
        for item in active_rows
    }
    for hit in hits:
        knowledge_id = str(hit.get("knowledge_id") or "")
        stored_revision = str(hit.get("knowledge_updated_at") or "")
        if not stored_revision or active_revisions.get(knowledge_id) != stored_revision:
            return True
    return False


def resolve_inspection_knowledge_context(conn: sqlite3.Connection, task: dict, query: str) -> list[dict]:
    """Return current task knowledge, refreshing a stale task snapshot when needed."""
    hits = inspection_knowledge_hits(task)
    tenant_id = str(task.get("tenant_id") or "")
    if hits and tenant_id and not inspection_knowledge_context_is_stale(conn, tenant_id, hits):
        return hits
    if not tenant_id:
        return hits
    refreshed_hits = retrieve_agent_knowledge(conn, {"tenant_id": tenant_id}, query)
    thresholds = task.get("thresholds") if isinstance(task.get("thresholds"), dict) else json_loads(task.get("thresholds"), {})
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    thresholds["knowledge_context"] = refreshed_hits
    task["thresholds"] = thresholds
    if task.get("task_id"):
        conn.execute(
            "UPDATE scheduled_inspections SET thresholds=?, updated_at=? WHERE task_id=? AND tenant_id=?",
            (json_dumps(thresholds), now_iso(), task["task_id"], tenant_id),
        )
    return refreshed_hits


def inspection_thresholds(full_text: str, knowledge_hits: list[dict]) -> dict:
    thresholds = (
        {
            "confidence": 0.80,
            "low_confidence_to_pending": True,
            "require_marked_anomaly_image": True,
            "visual_compliance": extract_visual_compliance_pack(full_text),
        }
        if is_visual_compliance_request(full_text)
        else {"confidence": 0.80}
    )
    if knowledge_hits:
        thresholds["knowledge_context"] = knowledge_hits
    return thresholds


def inspection_knowledge_summary(knowledge_hits: list[dict]) -> str:
    if not knowledge_hits:
        return ""
    titles = "、".join(item["title"] for item in knowledge_hits[:3])
    image_count = sum(len(item.get("reference_assets") or item.get("asset_urls") or []) for item in knowledge_hits)
    image_note = f"，含 {image_count} 张参考图片" if image_count else ""
    return f"已召回 {len(knowledge_hits)} 条知识：{titles}{image_note}，将作为本次比对标准。"


def inspection_question_with_knowledge(inspection_goal: str, knowledge_hits: list[dict]) -> str:
    if not knowledge_hits:
        return inspection_goal
    lines = []
    for item in knowledge_hits:
        detail = []
        if item.get("tags"):
            detail.append(f"标签：{'、'.join(item['tags'])}")
        if item.get("content_text"):
            detail.append(f"说明：{item['content_text']}")
        reference_assets = item.get("reference_assets") or []
        reference_skus = list(
            dict.fromkeys(
                str(asset.get("sku") or "").strip().upper()
                for asset in reference_assets
                if isinstance(asset, dict) and str(asset.get("sku") or "").strip()
            )
        )
        if reference_skus:
            detail.append(f"样板 SKU：{'、'.join(reference_skus)}")
        elif item.get("sku"):
            detail.append(f"SKU：{item['sku']}")
        detail.append(f"参考图片：{len(reference_assets or item.get('asset_urls') or [])} 张")
        lines.append(f"- {item['title']}（{'；'.join(detail)}）")
    return (
        f"{inspection_goal}\n\n"
        "【知识库参照标准】以下知识和随附参考图片已成功召回，必须与现场监控图片进行视觉比对。"
        "不得以“缺少样板图/比对依据”为由跳过；只有现场画面未覆盖目标、遮挡或无法辨识时才可判定证据不足。"
        "若用户要求标出不符合样板的情况，清晰不符合应输出 POSITIVE 异常，清晰符合应输出 NEGATIVE，无从判断才输出 UNCERTAIN。"
        "当参考知识带有 SKU 时，按单个镜头执行 SKU 命中判定：镜头内命中任意一个受控 SKU 即为非风险镜头，"
        "必须返回命中的 SKU 并在该镜头图片右上角展示标签；只有镜头内存在可识别的出样家具且未命中任何受控 SKU 时，"
        "才把该镜头作为风险项报出。画面无出样家具或不可辨识时，不得误报为风险。不能根据标题、镜头名称或猜测补全 SKU。\n"
        + "\n".join(lines)
    )


def prepare_inspection_reference_image(content: bytes) -> tuple[bytes, str] | None:
    """Make an uploaded reference small enough to safely repeat in VLM calls.

    A scheduled run analyzes each live frame independently, so retaining original
    multi-megabyte uploads would multiply the request body for every camera. The
    uploaded original remains unchanged; this only creates an in-memory VLM copy.
    """
    if not content:
        return None
    if Image is None or ImageOps is None:
        return (content, "image/jpeg") if len(content) <= MAX_INSPECTION_REFERENCE_IMAGE_BYTES else None
    try:
        with Image.open(BytesIO(content)) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode == "RGBA":
                flattened = Image.new("RGB", image.size, "white")
                flattened.paste(image, mask=image.getchannel("A"))
                image = flattened
            elif image.mode != "RGB":
                image = image.convert("RGB")

            resampling = Image.Resampling.LANCZOS
            for edge in (MAX_INSPECTION_REFERENCE_EDGE, 1120, 960, 800, 640):
                candidate = image.copy()
                candidate.thumbnail((edge, edge), resampling)
                for quality in (84, 76, 68):
                    buffer = BytesIO()
                    candidate.save(
                        buffer,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=True,
                    )
                    prepared = buffer.getvalue()
                    if len(prepared) <= MAX_INSPECTION_REFERENCE_IMAGE_BYTES:
                        return prepared, "image/jpeg"
    except (OSError, ValueError):
        return None
    return None


def inspection_reference_images(tenant_id: str, knowledge_hits: list[dict]) -> list[dict]:
    """Turn tenant-owned uploaded references into VLM-ready data URLs.

    Remote HTTPS assets stay remote so an existing URL-imported knowledge item can still
    be used, while local paths are constrained to this tenant's knowledge upload folder.
    """
    tenant_folder = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tenant_id)[:80] or "tenant"
    local_prefix = f"/static/uploads/knowledge/{tenant_folder}/"
    allowed_folder = (KNOWLEDGE_UPLOAD_DIR / tenant_folder).resolve()
    references = []
    prepared_total_bytes = 0
    for hit in knowledge_hits:
        reference_assets = hit.get("reference_assets")
        if not isinstance(reference_assets, list):
            reference_assets = normalize_knowledge_reference_assets(
                list(hit.get("asset_urls") or []), str(hit.get("sku") or ""), []
            )
        for asset in reference_assets:
            if len(references) >= MAX_INSPECTION_REFERENCE_IMAGES:
                return references
            if not isinstance(asset, dict):
                continue
            url = str(asset.get("asset_url") or "").strip()
            snapshot_url = ""
            if url.startswith(local_prefix):
                filename = Path(url).name
                if not re.fullmatch(r"ka_[a-f0-9]{16}\.(?:jpg|png|webp)", filename):
                    continue
                storage_path = (KNOWLEDGE_UPLOAD_DIR / tenant_folder / filename).resolve()
                if not str(storage_path).startswith(str(allowed_folder)) or not storage_path.is_file():
                    continue
                try:
                    content = storage_path.read_bytes()
                except OSError:
                    continue
                if not content or len(content) > MAX_KNOWLEDGE_IMAGE_BYTES:
                    continue
                prepared = prepare_inspection_reference_image(content)
                if not prepared:
                    continue
                prepared_content, mime_type = prepared
                if prepared_total_bytes + len(prepared_content) > MAX_INSPECTION_REFERENCE_TOTAL_BYTES:
                    continue
                prepared_total_bytes += len(prepared_content)
                snapshot_url = f"data:{mime_type};base64,{base64.b64encode(prepared_content).decode('ascii')}"
            elif re.fullmatch(r"https://[^\s]+", url, flags=re.IGNORECASE):
                snapshot_url = url
            if snapshot_url:
                references.append(
                    {
                        "snapshot_url": snapshot_url,
                        "knowledge_id": hit["knowledge_id"],
                        "knowledge_title": hit["title"],
                        "sku": str(asset.get("sku") or hit.get("sku") or "")[:64],
                        "description": str(asset.get("description") or "")[:800],
                        "view_tag": str(asset.get("view_tag") or "")[:80],
                        "asset_url": url,
                        "prepared_bytes": len(prepared_content) if url.startswith(local_prefix) else None,
                    }
                )
    return references


def inspection_execution_error_message(exc: Exception) -> str:
    """Keep scheduled-run errors actionable without exposing upstream payloads."""
    if not isinstance(exc, OnlineAgentError):
        return str(exc)
    http_status = exc.detail.get("http_status") if isinstance(exc.detail, dict) else None
    if http_status and f"HTTP {http_status}" not in exc.message:
        return f"{exc.message}（HTTP {http_status}）"
    return exc.message


def validate_agent_memory_payload(payload: dict) -> dict:
    errors = []
    category = str(payload.get("category") or "business_rule").strip()
    scope = str(payload.get("scope") or "tenant").strip()
    key = str(payload.get("key") or payload.get("memory_key") or "").strip()
    value = str(payload.get("value") or payload.get("memory_value") or "").strip()
    confidence = payload.get("confidence", 1)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))
    if category not in MEMORY_CATEGORIES:
        errors.append(f"category must be one of {', '.join(MEMORY_CATEGORIES)}")
    if scope not in MEMORY_SCOPES:
        errors.append(f"scope must be one of {', '.join(sorted(MEMORY_SCOPES))}")
    if len(key) < 2:
        errors.append("key is required")
    if len(value) < 2:
        errors.append("value is required")
    aliases = _split_text_list(payload.get("aliases"))
    return {
        "ok": not errors,
        "errors": errors,
        "normalized": {
            "category": category,
            "scope": scope,
            "key": key[:120],
            "value": value[:2000],
            "aliases": aliases[:20],
            "confidence": confidence,
        },
    }


def validate_agent_knowledge_payload(payload: dict, *, allow_existing_assets: bool = False) -> dict:
    errors = []
    title = str(payload.get("title") or "").strip()
    sku = str(payload.get("sku") or "").strip().upper()
    knowledge_type = str(payload.get("knowledge_type") or payload.get("type") or "sop").strip()
    modality = str(payload.get("modality") or "text").strip()
    content_text = str(payload.get("content_text") or payload.get("content") or "").strip()
    asset_url = str(payload.get("asset_url") or "").strip()
    asset_uploads = knowledge_asset_uploads_from_payload(payload)
    tags = _split_text_list(payload.get("tags"))
    raw_asset_metadata = payload.get("asset_metadata", [])
    asset_metadata = []
    if len(title) < 2:
        errors.append("title is required")
    if sku and not KNOWLEDGE_SKU_LABEL_PATTERN.fullmatch(sku):
        errors.append("sku must be a 1-64 character SKU code or Chinese product/color label")
    if knowledge_type not in KNOWLEDGE_TYPES:
        errors.append(f"knowledge_type must be one of {', '.join(KNOWLEDGE_TYPES)}")
    if modality not in KNOWLEDGE_MODALITIES:
        errors.append(f"modality must be one of {', '.join(sorted(KNOWLEDGE_MODALITIES))}")
    if asset_uploads is None:
        errors.append("asset_uploads must be a list")
    elif len(asset_uploads) > MAX_KNOWLEDGE_IMAGE_BATCH_COUNT:
        errors.append(f"asset_uploads cannot exceed {MAX_KNOWLEDGE_IMAGE_BATCH_COUNT} items")
    else:
        for asset_upload in asset_uploads:
            if (
                not isinstance(asset_upload, dict)
                or not str(asset_upload.get("data_url") or "").strip()
                or not str(asset_upload.get("filename") or "").strip()
            ):
                errors.append("each asset_upload must include filename and data_url")
                break
    if len(content_text) < 4 and not asset_url and not asset_uploads and not allow_existing_assets:
        errors.append("content_text or asset_url is required")
    if asset_url:
        parsed = urlparse(asset_url)
        if parsed.scheme not in {"http", "https"} and not asset_url.startswith("/static/"):
            errors.append("asset_url must be http(s) or /static/ path")
    if raw_asset_metadata is None:
        raw_asset_metadata = []
    if not isinstance(raw_asset_metadata, list):
        errors.append("asset_metadata must be a list")
    elif len(raw_asset_metadata) > MAX_KNOWLEDGE_IMAGE_BATCH_COUNT:
        errors.append(f"asset_metadata cannot exceed {MAX_KNOWLEDGE_IMAGE_BATCH_COUNT} items")
    else:
        seen_sources = set()
        upload_count = len(asset_uploads or [])
        for index, raw_entry in enumerate(raw_asset_metadata):
            if not isinstance(raw_entry, dict):
                errors.append("each asset_metadata entry must be an object")
                break
            raw_upload_index = raw_entry.get("upload_index")
            metadata_url = str(raw_entry.get("asset_url") or "").strip()
            has_upload_index = raw_upload_index is not None and raw_upload_index != ""
            if has_upload_index and metadata_url:
                errors.append("each asset_metadata entry must reference either asset_url or upload_index")
                break
            if has_upload_index:
                if isinstance(raw_upload_index, bool):
                    errors.append("asset_metadata upload_index must be an integer")
                    break
                try:
                    upload_index = int(raw_upload_index)
                except (TypeError, ValueError):
                    errors.append("asset_metadata upload_index must be an integer")
                    break
                if upload_index < 0 or upload_index >= upload_count:
                    errors.append("asset_metadata upload_index must reference an uploaded image")
                    break
                source_key = f"upload:{upload_index}"
            elif metadata_url:
                upload_index = None
                source_key = f"url:{metadata_url}"
            else:
                errors.append("each asset_metadata entry must reference an image")
                break
            if source_key in seen_sources:
                errors.append("asset_metadata cannot repeat the same image")
                break
            seen_sources.add(source_key)
            entry_sku = str(raw_entry.get("sku") or "").strip().upper()
            description = str(raw_entry.get("description") or "").strip()
            view_tag = str(raw_entry.get("view_tag") or "").strip()
            if entry_sku and not KNOWLEDGE_SKU_LABEL_PATTERN.fullmatch(entry_sku):
                errors.append(f"asset_metadata[{index}].sku is invalid")
                break
            if len(description) > 800:
                errors.append(f"asset_metadata[{index}].description cannot exceed 800 characters")
                break
            if len(view_tag) > 80:
                errors.append(f"asset_metadata[{index}].view_tag cannot exceed 80 characters")
                break
            asset_metadata.append(
                {
                    **({"upload_index": upload_index} if upload_index is not None else {"asset_url": metadata_url}),
                    "sku": entry_sku,
                    "description": description,
                    "view_tag": view_tag,
                }
            )
    return {
        "ok": not errors,
        "errors": errors,
        "normalized": {
            "title": title[:160],
            "sku": sku,
            "knowledge_type": knowledge_type,
            "modality": modality,
            "content_text": content_text[:6000],
            "asset_url": asset_url[:1000] or None,
            "tags": tags[:30],
            "asset_metadata": asset_metadata,
        },
    }


def create_agent_memory(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    validation = validate_agent_memory_payload(payload)
    if not validation["ok"]:
        raise ApiError("AGENT_MEMORY_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "长期记忆校验未通过", "validation": validation})
    normalized = validation["normalized"]
    important_memory = normalized["scope"] == "tenant" or normalized["category"] == "business_rule"
    if important_memory and payload.get("confirm_important") is not True:
        raise ApiError(
            "AGENT_MEMORY_CONFIRM_REQUIRED",
            HTTPStatus.CONFLICT,
            {
                "message": "租户级记忆或业务判断口径会影响后续 Agent 推理，需要确认后再保存。",
                "requires_confirmation": True,
            },
        )
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO agent_memories(
             memory_id, tenant_id, user_id, scope, category, memory_key, memory_value,
             aliases_json, confidence, source, status, created_by, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            memory_id,
            user["tenant_id"],
            user["user_id"],
            normalized["scope"],
            normalized["category"],
            normalized["key"],
            normalized["value"],
            json_dumps(normalized["aliases"]),
            normalized["confidence"],
            str(payload.get("source") or "manual"),
            "ACTIVE",
            user["user_id"],
            timestamp,
            timestamp,
        ),
    )
    item = one(conn, "SELECT * FROM agent_memories WHERE memory_id=?", (memory_id,))
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.memory.create",
        "agent_memory",
        memory_id,
        None,
        {"category": normalized["category"], "scope": normalized["scope"], "key": normalized["key"]},
        "agent_catalog",
        None,
    )
    return serialize_agent_memory(item)


def delete_agent_memory(conn: sqlite3.Connection, user: dict, memory_id: str) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    item = one(
        conn,
        "SELECT * FROM agent_memories WHERE memory_id=? AND tenant_id=? AND status='ACTIVE'",
        (memory_id, user["tenant_id"]),
    )
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    timestamp = now_iso()
    conn.execute(
        "UPDATE agent_memories SET status='DELETED', updated_at=? WHERE memory_id=? AND tenant_id=?",
        (timestamp, memory_id, user["tenant_id"]),
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.memory.delete",
        "agent_memory",
        memory_id,
        None,
        {"category": item["category"], "scope": item["scope"], "key": item["memory_key"]},
        "agent_catalog",
        None,
    )
    return {"memory_id": memory_id, "status": "DELETED", "updated_at": timestamp}


def create_agent_knowledge_assets(conn: sqlite3.Connection, user: dict, asset_uploads: list[dict]) -> list[dict]:
    uploaded_assets = []
    try:
        for asset_upload in asset_uploads:
            asset = create_agent_knowledge_asset(conn, user, asset_upload)
            uploaded_assets.append(asset)
            if sum(item["size_bytes"] for item in uploaded_assets) > MAX_KNOWLEDGE_IMAGE_BATCH_BYTES:
                raise ApiError(
                    "AGENT_KNOWLEDGE_ASSET_INVALID",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"message": "本次图片总大小不能超过 32MB"},
                )
    except Exception:
        for asset in uploaded_assets:
            remove_uploaded_knowledge_asset(asset["asset_url"], user["tenant_id"])
        raise
    return uploaded_assets


def materialize_knowledge_asset_metadata(
    asset_urls: list[str],
    default_sku: str,
    metadata_entries: list[dict],
    uploaded_assets: list[dict],
    *,
    preserved_assets: list[dict] | None = None,
) -> list[dict]:
    """Resolve upload indexes to URLs and reject metadata for unretained images."""
    urls = list(dict.fromkeys(str(url or "").strip() for url in asset_urls if str(url or "").strip()))
    uploaded_urls = [str(asset.get("asset_url") or "").strip() for asset in uploaded_assets]
    source_metadata = {
        str(asset.get("asset_url") or "").strip(): asset
        for asset in (preserved_assets or [])
        if isinstance(asset, dict) and str(asset.get("asset_url") or "").strip() in urls
    }
    for entry in metadata_entries:
        if not isinstance(entry, dict):
            continue
        if "upload_index" in entry:
            upload_index = entry["upload_index"]
            asset_url = uploaded_urls[upload_index] if isinstance(upload_index, int) and 0 <= upload_index < len(uploaded_urls) else ""
        else:
            asset_url = str(entry.get("asset_url") or "").strip()
        if not asset_url or asset_url not in urls:
            raise ApiError(
                "AGENT_KNOWLEDGE_INVALID",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"message": "图片 SKU 元数据只能关联本次保留或新增的图片"},
            )
        source_metadata[asset_url] = {
            "asset_url": asset_url,
            "sku": str(entry.get("sku") or "").strip().upper(),
            "description": str(entry.get("description") or "").strip()[:800],
            "view_tag": str(entry.get("view_tag") or "").strip()[:80],
        }
    reference_assets = normalize_knowledge_reference_assets(
        urls, default_sku, list(source_metadata.values())
    )
    if any(not str(asset.get("sku") or "").strip() for asset in reference_assets):
        raise ApiError(
            "AGENT_KNOWLEDGE_INVALID",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"message": "每张知识库图片都必须填写 SKU；视角和特征说明可留空"},
        )
    return reference_assets


def create_agent_knowledge_item(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    validation = validate_agent_knowledge_payload(payload)
    if not validation["ok"]:
        raise ApiError("AGENT_KNOWLEDGE_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "知识内容校验未通过", "validation": validation})
    normalized = validation["normalized"]
    asset_uploads = knowledge_asset_uploads_from_payload(payload) or []
    uploaded_assets = create_agent_knowledge_assets(conn, user, asset_uploads)
    asset_urls = [item["asset_url"] for item in uploaded_assets]
    if normalized["asset_url"] and normalized["asset_url"] not in asset_urls:
        asset_urls.append(normalized["asset_url"])
    try:
        reference_assets = materialize_knowledge_asset_metadata(
            asset_urls,
            normalized["sku"],
            normalized["asset_metadata"],
            uploaded_assets,
        )
    except Exception:
        for asset in uploaded_assets:
            remove_uploaded_knowledge_asset(asset["asset_url"], user["tenant_id"])
        raise
    knowledge_id = f"kn_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    try:
        conn.execute(
            """INSERT INTO agent_knowledge_items(
                 knowledge_id, tenant_id, title, sku, knowledge_type, modality, content_text, asset_url,
                 asset_urls_json, asset_metadata_json, tags_json, source, status, created_by, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                knowledge_id,
                user["tenant_id"],
                normalized["title"],
                normalized["sku"],
                normalized["knowledge_type"],
                normalized["modality"],
                normalized["content_text"],
                asset_urls[0] if asset_urls else None,
                json_dumps(asset_urls),
                json_dumps(reference_assets),
                json_dumps(normalized["tags"]),
                str(payload.get("source") or "manual"),
                "ACTIVE",
                user["user_id"],
                timestamp,
                timestamp,
            ),
        )
    except Exception:
        for asset in uploaded_assets:
            remove_uploaded_knowledge_asset(asset["asset_url"], user["tenant_id"])
        raise
    item = one(conn, "SELECT * FROM agent_knowledge_items WHERE knowledge_id=?", (knowledge_id,))
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.knowledge.create",
        "agent_knowledge",
        knowledge_id,
        None,
        {
            "knowledge_type": normalized["knowledge_type"],
            "modality": normalized["modality"],
            "title": normalized["title"],
            "asset_count": len(asset_urls),
        },
        "agent_catalog",
        None,
    )
    return serialize_agent_knowledge(item)


def update_agent_knowledge_item(conn: sqlite3.Connection, user: dict, knowledge_id: str, payload: dict) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    item = one(
        conn,
        "SELECT * FROM agent_knowledge_items WHERE knowledge_id=? AND tenant_id=? AND status='ACTIVE'",
        (knowledge_id, user["tenant_id"]),
    )
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)

    current_asset_urls = knowledge_asset_urls(item)
    requested_asset_urls = payload.get("existing_asset_urls")
    if requested_asset_urls is None:
        retained_asset_urls = current_asset_urls
    elif not isinstance(requested_asset_urls, list):
        raise ApiError(
            "AGENT_KNOWLEDGE_INVALID",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"message": "保留素材必须是图片地址列表"},
        )
    else:
        retained_asset_urls = list(dict.fromkeys(str(url or "").strip() for url in requested_asset_urls if str(url or "").strip()))
        invalid_urls = [url for url in retained_asset_urls if url not in current_asset_urls]
        if invalid_urls:
            raise ApiError(
                "AGENT_KNOWLEDGE_INVALID",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"message": "不能保留不属于当前知识的图片素材"},
            )

    validation = validate_agent_knowledge_payload(payload, allow_existing_assets=bool(retained_asset_urls))
    if not validation["ok"]:
        raise ApiError("AGENT_KNOWLEDGE_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "知识内容校验未通过", "validation": validation})
    normalized = validation["normalized"]
    asset_uploads = knowledge_asset_uploads_from_payload(payload) or []
    uploaded_assets = create_agent_knowledge_assets(conn, user, asset_uploads)
    asset_urls = list(retained_asset_urls)
    asset_urls.extend(asset["asset_url"] for asset in uploaded_assets)
    if normalized["asset_url"]:
        asset_urls.append(normalized["asset_url"])
    asset_urls = list(dict.fromkeys(asset_urls))
    preserved_assets = (
        knowledge_reference_assets(item)
        if "asset_metadata" not in payload and json_loads(item.get("asset_metadata_json"), [])
        else []
    )
    try:
        reference_assets = materialize_knowledge_asset_metadata(
            asset_urls,
            normalized["sku"],
            normalized["asset_metadata"],
            uploaded_assets,
            preserved_assets=preserved_assets,
        )
    except Exception:
        for asset in uploaded_assets:
            remove_uploaded_knowledge_asset(asset["asset_url"], user["tenant_id"])
        raise
    timestamp = now_iso()
    source = "local_upload" if uploaded_assets else str(payload.get("source") or item["source"] or "manual")
    try:
        conn.execute(
            """UPDATE agent_knowledge_items
               SET title=?, sku=?, knowledge_type=?, modality=?, content_text=?, asset_url=?, asset_urls_json=?,
                   asset_metadata_json=?, tags_json=?, source=?, updated_at=?
               WHERE knowledge_id=? AND tenant_id=? AND status='ACTIVE'""",
            (
                normalized["title"],
                normalized["sku"],
                normalized["knowledge_type"],
                normalized["modality"],
                normalized["content_text"],
                asset_urls[0] if asset_urls else None,
                json_dumps(asset_urls),
                json_dumps(reference_assets),
                json_dumps(normalized["tags"]),
                source,
                timestamp,
                knowledge_id,
                user["tenant_id"],
            ),
        )
    except Exception:
        for asset in uploaded_assets:
            remove_uploaded_knowledge_asset(asset["asset_url"], user["tenant_id"])
        raise

    for asset_url in current_asset_urls:
        if asset_url not in asset_urls:
            remove_uploaded_knowledge_asset(asset_url, user["tenant_id"])
    updated_item = one(conn, "SELECT * FROM agent_knowledge_items WHERE knowledge_id=?", (knowledge_id,))
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.knowledge.update",
        "agent_knowledge",
        knowledge_id,
        {
            "title": item["title"],
            "sku": str(item.get("sku") or ""),
            "knowledge_type": item["knowledge_type"],
            "modality": item["modality"],
            "asset_count": len(current_asset_urls),
        },
        {
            "title": normalized["title"],
            "sku": normalized["sku"],
            "knowledge_type": normalized["knowledge_type"],
            "modality": normalized["modality"],
            "asset_count": len(asset_urls),
        },
        "agent_catalog",
        None,
    )
    return serialize_agent_knowledge(updated_item)


def create_agent_knowledge_asset(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    data_url = str(payload.get("data_url") or "").strip()
    filename = Path(str(payload.get("filename") or "knowledge-image")).name[:120]
    match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        raise ApiError("AGENT_KNOWLEDGE_ASSET_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "请上传有效的图片文件"})
    mime_type = match.group(1).lower()
    if mime_type not in KNOWLEDGE_IMAGE_MIME_EXT:
        raise ApiError("AGENT_KNOWLEDGE_ASSET_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "仅支持 JPG、PNG、WebP 或 GIF 图片"})
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) > int(MAX_KNOWLEDGE_IMAGE_BYTES * 1.45):
        raise ApiError("AGENT_KNOWLEDGE_ASSET_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "图片不能超过 8MB"})
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ApiError("AGENT_KNOWLEDGE_ASSET_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "图片内容解析失败"}) from exc
    if not content or len(content) > MAX_KNOWLEDGE_IMAGE_BYTES:
        raise ApiError("AGENT_KNOWLEDGE_ASSET_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "图片不能超过 8MB"})
    if not knowledge_image_bytes_match_mime(content, mime_type):
        raise ApiError("AGENT_KNOWLEDGE_ASSET_INVALID", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "图片内容与文件格式不匹配"})
    asset_id = f"ka_{uuid.uuid4().hex[:16]}"
    tenant_folder = re.sub(r"[^a-zA-Z0-9_.-]+", "_", user["tenant_id"])[:80] or "tenant"
    folder = KNOWLEDGE_UPLOAD_DIR / tenant_folder
    folder.mkdir(parents=True, exist_ok=True)
    extension = KNOWLEDGE_IMAGE_MIME_EXT[mime_type]
    storage_path = folder / f"{asset_id}{extension}"
    storage_path.write_bytes(content)
    asset_url = f"/static/uploads/knowledge/{tenant_folder}/{asset_id}{extension}"
    sha256 = hashlib.sha256(content).hexdigest()
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.knowledge_asset.upload",
        "agent_knowledge_asset",
        asset_id,
        None,
        {
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(content),
            "asset_url": asset_url,
            "sha256": sha256,
        },
        "agent_catalog",
        None,
    )
    return {
        "asset_id": asset_id,
        "asset_url": asset_url,
        "mime_type": mime_type,
        "filename": filename,
        "size_bytes": len(content),
        "sha256": sha256,
    }


def delete_agent_knowledge_item(conn: sqlite3.Connection, user: dict, knowledge_id: str) -> dict:
    if not role_can_manage_agent_catalog(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    item = one(
        conn,
        "SELECT * FROM agent_knowledge_items WHERE knowledge_id=? AND tenant_id=? AND status='ACTIVE'",
        (knowledge_id, user["tenant_id"]),
    )
    if not item:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    timestamp = now_iso()
    conn.execute(
        "UPDATE agent_knowledge_items SET status='DELETED', updated_at=? WHERE knowledge_id=? AND tenant_id=?",
        (timestamp, knowledge_id, user["tenant_id"]),
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.knowledge.delete",
        "agent_knowledge",
        knowledge_id,
        {
            "status": item["status"],
            "title": item["title"],
            "asset_urls": knowledge_asset_urls(item),
        },
        {"status": "DELETED", "deleted_at": timestamp},
        "agent_catalog",
        None,
    )
    return {"knowledge_id": knowledge_id, "status": "DELETED", "updated_at": timestamp}


def mask_app_key(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * min(12, len(value) - 8)}{value[-4:]}"


def integration_setup_request(text: str) -> bool:
    normalized = re.sub(r"[\s_]+", "", str(text or "")).lower()
    has_action = any(word in normalized for word in ("接入", "链接", "连接", "新增", "添加", "配置", "扩展"))
    has_target = any(word in normalized for word in ("租户", "appkey", "appsecret", "deepvision"))
    has_credential_bundle = "appkey" in normalized and "appsecret" in normalized
    return (has_action and has_target) or has_credential_bundle


INTEGRATION_FIELD_LABELS = {
    "tenant_name": "租户名称",
    "tenant_code": "租户编码",
    "app_key": "AppKey",
    "app_secret": "AppSecret",
}

INTEGRATION_SECRET_FIELDS = {"app_key", "app_secret"}

INTEGRATION_LABEL_PATTERN = (
    r"tenantname|tenant_name|租户名称|租户名|"
    r"tenantcode|tenant_code|tenantid|tenant_id|tenant\s*id|tenant\s*code|"
    r"租户\s*(?:id|code)|租户编码|租户代码|租户编号|编号|编码|"
    r"app_?key|app_?secret"
)


def is_placeholder_credential(value: str) -> bool:
    normalized = re.sub(r"[\s\[\]【】()（）{}<>《》,;，；:：]+", "", str(value or "").lower())
    if not normalized:
        return True
    if set(normalized) <= {"*", "•", "●", "·", "x"}:
        return True
    return any(marker in normalized for marker in ("已隐藏", "隐藏", "安全配置卡", "请使用", "redacted", "masked", "placeholder"))


def clean_integration_field_value(value: str, *, secret_like: bool = False) -> str:
    cleaned = str(value or "").strip().strip("\"'`")
    cleaned = re.sub(r"[\s,;，；。]+$", "", cleaned).strip()
    cleaned = re.split(
        rf"(?is)[\s,;，；]+(?:{INTEGRATION_LABEL_PATTERN})\s*[:：=]",
        cleaned,
        maxsplit=1,
    )[0].strip()
    if secret_like and is_placeholder_credential(cleaned):
        return ""
    if is_placeholder_credential(cleaned) and any(marker in cleaned for marker in ("已隐藏", "隐藏", "安全配置卡", "请使用")):
        return ""
    return cleaned


def extract_integration_labeled_value(text: str, labels: tuple[str, ...], *, secret_like: bool = False) -> str:
    label_pattern = "|".join(labels)
    match = re.search(
        rf"(?is)(?:^|[\s,;，；])(?:{label_pattern})\s*[:：=]\s*(.+?)(?=(?:[\s,;，；]+(?:{INTEGRATION_LABEL_PATTERN})\s*[:：=])|$)",
        text,
    )
    if not match:
        return ""
    return clean_integration_field_value(match.group(1), secret_like=secret_like)


def parse_integration_credentials(text: str) -> dict:
    value = str(text or "")
    extractors = {
        "tenant_name": (("tenantname", "tenant_name", "租户名称", "租户名"), False),
        "tenant_code": (
            (
                "tenantcode",
                "tenant_code",
                "tenantid",
                "tenant_id",
                "tenant\\s*id",
                "tenant\\s*code",
                "租户\\s*(?:id|code)",
                "租户编码",
                "租户代码",
                "租户编号",
                "编号",
                "编码",
            ),
            False,
        ),
        "app_key": (("app_?key",), True),
        "app_secret": (("app_?secret",), True),
    }
    parsed = {}
    for key, (labels, secret_like) in extractors.items():
        field_value = extract_integration_labeled_value(value, labels, secret_like=secret_like)
        if field_value:
            parsed[key] = field_value
    if parsed.get("tenant_code") and not parsed.get("tenant_name"):
        parsed["tenant_name"] = parsed["tenant_code"]
    return parsed


def integration_setup_prefill(credentials: dict, *, include_secrets: bool = False) -> dict:
    """Build a setup prefill payload.

    Secret fields are only allowed in the immediate API response so the current
    browser card can submit them. Storage sanitization strips them again.
    """
    allowed_fields = ["tenant_name", "tenant_code"]
    if include_secrets:
        allowed_fields.extend(["app_key", "app_secret"])
    return {
        key: credentials[key]
        for key in allowed_fields
        if credentials.get(key)
    }


def integration_setup_missing_fields(prefill: dict) -> list[str]:
    return [key for key in ("tenant_name", "tenant_code", "app_key", "app_secret") if not prefill.get(key)]


def integration_setup_assistant_copy(prefill: dict, missing_fields: list[str], secure_prefill_fields: list[str] | None = None) -> str:
    safe_prefill = {key: value for key, value in prefill.items() if key not in INTEGRATION_SECRET_FIELDS}
    secure_prefill_fields = [field for field in (secure_prefill_fields or []) if field in INTEGRATION_SECRET_FIELDS]
    if safe_prefill or secure_prefill_fields:
        recognized = "、".join(
            f"{INTEGRATION_FIELD_LABELS[key]}“{value}”"
            for key, value in safe_prefill.items()
        )
        if secure_prefill_fields:
            secret_label = "、".join(INTEGRATION_FIELD_LABELS[field] for field in secure_prefill_fields)
            recognized = "、".join(part for part in (recognized, f"{secret_label}（仅写入当前安全卡）") if part)
        missing = "、".join(
            INTEGRATION_FIELD_LABELS[key]
            for key in missing_fields
            if key not in secure_prefill_fields
        )
        action_copy = f"确认并补充{missing}" if missing else "确认信息"
        return (
            f"我已从你的描述中识别出{recognized}。请在下方安全配置卡{action_copy}，"
            "我会先验证连接并同步门店，不会把密钥保存在聊天文本中。"
        )
    return "我已识别为新租户接入需求。请在下方安全配置卡中填写凭证，我会先验证连接并同步门店，不会把密钥保存在聊天文本中。"


def latest_integration_setup_prefill(conn, conversation_id: str) -> dict:
    recent_messages = rows(
        conn,
        """SELECT linked_object
           FROM messages
           WHERE conversation_id=? AND sender='assistant'
           ORDER BY created_at DESC, rowid DESC
           LIMIT 20""",
        (conversation_id,),
    )
    for message in recent_messages:
        linked_object = json_loads(message.get("linked_object"), {})
        setup = (linked_object.get("artifact") or {}).get("integrationSetup") if isinstance(linked_object, dict) else None
        if not isinstance(setup, dict) or setup.get("mode") != "CREATE":
            continue
        prefill = setup.get("prefill") if isinstance(setup.get("prefill"), dict) else {}
        safe_prefill = {
            key: prefill[key]
            for key in ("tenant_name", "tenant_code")
            if prefill.get(key)
        }
        if safe_prefill:
            return safe_prefill
    return {}


def redact_integration_message(text: str) -> str:
    redacted = str(text or "")
    redacted = re.sub(
        r"(?i)(app_?(?:key|secret)\s*[:：=]\s*)[^\n\r,;，；]+",
        r"\1[已隐藏，请使用安全配置卡]",
        redacted,
    )
    return redacted[:1000]


def integration_artifact_summary(integration: dict) -> dict:
    """Keep chat artifacts compact; full store lists are loaded on the integration page."""
    return {
        "integration_id": integration["integration_id"],
        "tenant_code": integration["tenant_code"],
        "tenant_name": integration["tenant_name"],
        "app_key_masked": integration.get("app_key_masked"),
        "source": integration.get("source"),
        "status": integration.get("status"),
        "store_count": integration.get("store_count"),
        "last_synced_at": integration.get("last_synced_at"),
        "last_error": integration.get("last_error"),
        "created_at": integration.get("created_at"),
        "credentials_managed_externally": integration.get("credentials_managed_externally", False),
    }


def integration_result_message(conn: sqlite3.Connection, conversation_id: str, integration: dict) -> dict:
    content = f"租户“{integration['tenant_name']}”已安全接入，已同步 {integration['store_count']} 家门店。凭证已加密保存，页面仅显示脱敏 AppKey。"
    linked = {
        "artifact": {"integrationResult": integration_artifact_summary(integration)},
        "agent": {
            "intent": "CONFIGURE_TENANT_INTEGRATION",
            "engine": "secure_integration_manager",
            "status": "SUCCEEDED",
            "tool_calls": ["credential.redact", "paas.auth.verify", "paas.org.sync", "credential.encrypt"],
        },
        "source": "integration_manager",
    }
    attach_agent_trace(linked, "接入 DeepVision 租户")
    return add_message(
        conn,
        conversation_id,
        "assistant",
        content,
        None,
        linked,
    )


def serialize_integration_row(conn: sqlite3.Connection, integration: dict) -> dict:
    stores = rows(
        conn,
        """SELECT org_id, parent_id, name, org_type, status, camera_count, synced_at
           FROM tenant_integration_stores WHERE integration_id=? ORDER BY name""",
        (integration["integration_id"],),
    )
    return {
        "integration_id": integration["integration_id"],
        "tenant_code": integration["tenant_code"],
        "tenant_name": integration["tenant_name"],
        "app_key_masked": integration["app_key_masked"],
        "source": integration["source"],
        "status": integration["status"],
        "store_count": len(stores),
        "last_synced_at": integration.get("last_synced_at"),
        "last_error": integration.get("last_error"),
        "created_at": integration["created_at"],
        "stores": stores,
        "credentials_managed_externally": False,
    }


def environment_integration() -> dict | None:
    online = get_online_agent()
    if not online:
        return None
    app_key = os.environ.get("DEEPVISION_APP_KEY", "")
    tenant_code = os.environ.get("DEEPVISION_TENANT_CODE", online.tenant_code or "default")
    tenant_name = os.environ.get("DEEPVISION_TENANT_NAME", tenant_code.upper())
    orgs, fields = online._organization_inventory()
    stores = []
    for field in fields:
        camera_count = None
        try:
            camera_count = len(online._camera_rows(field))
        except OnlineAgentError:
            pass
        stores.append(
            {
                "org_id": field["org_id"],
                "parent_id": field.get("parent_id"),
                "name": field["name"],
                "org_type": field.get("org_type") or "store",
                "status": "CONNECTED",
                "camera_count": camera_count,
                "synced_at": now_iso(),
            }
        )
    return {
        "integration_id": f"env:{tenant_code}",
        "tenant_code": tenant_code,
        "tenant_name": tenant_name,
        "app_key_masked": mask_app_key(app_key),
        "source": "ENVIRONMENT",
        "status": "CONNECTED",
        "store_count": len(stores),
        "last_synced_at": now_iso(),
        "last_error": None,
        "created_at": None,
        "stores": stores,
        "credentials_managed_externally": True,
        "org_count": len(orgs),
    }


def list_integrations(conn: sqlite3.Connection) -> list[dict]:
    result = []
    env_profile = environment_integration()
    if env_profile:
        result.append(env_profile)
    result.extend(
        serialize_integration_row(conn, item)
        for item in rows(conn, "SELECT * FROM tenant_integrations ORDER BY created_at DESC")
    )
    return result


def create_tenant_integration(conn: sqlite3.Connection, user: dict, params: dict) -> dict:
    if not role_can_manage_integrations(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    tenant_name = re.sub(r"\s+", " ", str(params.get("tenant_name") or "")).strip()
    tenant_code = str(params.get("tenant_code") or "").strip()
    app_key = str(params.get("app_key") or "").strip()
    app_secret = str(params.get("app_secret") or "").strip()
    if not tenant_name or len(tenant_name) > 100:
        raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "租户名称必填且不超过 100 字"})
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", tenant_code):
        raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "租户编码仅支持 2-64 位字母、数字、点、下划线或连字符"})
    if not 8 <= len(app_key) <= 128 or not 16 <= len(app_secret) <= 256:
        raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "AppKey 或 AppSecret 格式不正确"})
    env_code = os.environ.get("DEEPVISION_TENANT_CODE", "").strip()
    if tenant_code == env_code or one(conn, "SELECT integration_id FROM tenant_integrations WHERE tenant_code=?", (tenant_code,)):
        raise ApiError("INTEGRATION_ALREADY_EXISTS", HTTPStatus.CONFLICT)

    client = DeepVisionPaaSClient(
        app_key=app_key,
        app_secret=app_secret,
        tenant_code=tenant_code,
        base_url=os.environ.get("DEEPVISION_BASE_URL", "https://api.deepeleph.com"),
    )
    candidate_agent = OnlineInspectionAgent(client)
    try:
        _orgs, fields = candidate_agent._organization_inventory()
    except OnlineAgentError as exc:
        raise ApiError(
            "INTEGRATION_VALIDATION_FAILED",
            HTTPStatus.BAD_GATEWAY,
            {"message": f"DeepVision 验证失败：{exc.message}", "upstream_code": exc.code},
        ) from exc

    deduped_fields = []
    seen_field_ids = set()
    for field in fields:
        org_id = str(field.get("org_id") or "").strip()
        if not org_id or org_id in seen_field_ids:
            continue
        seen_field_ids.add(org_id)
        deduped_fields.append(field)
    if not deduped_fields:
        raise ApiError("VALIDATION_FAILED", HTTPStatus.UNPROCESSABLE_ENTITY, {"message": "连接成功，但未读取到可用门店"})
    fields = deduped_fields

    integration_id = f"int_{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    try:
        encrypted = credential_vault().encrypt({"app_key": app_key, "app_secret": app_secret})
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            "SECURE_STORAGE_UNAVAILABLE",
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"message": "安全凭证存储不可用，请检查本地密钥配置后重试"},
        ) from exc
    fingerprint = hashlib.sha256(f"{tenant_code}:{app_key}:{app_secret}".encode("utf-8")).hexdigest()[:20]
    try:
        conn.execute(
            """INSERT INTO tenant_integrations(
                 integration_id, tenant_code, tenant_name, app_key_masked, encrypted_credentials,
                 credential_fingerprint, source, status, store_count, last_synced_at, last_error,
                 created_by, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                integration_id, tenant_code, tenant_name, mask_app_key(app_key), encrypted,
                fingerprint, "CHAT_SECURE_FORM", "CONNECTED", len(fields), timestamp, None,
                user["user_id"], timestamp, timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ApiError("INTEGRATION_ALREADY_EXISTS", HTTPStatus.CONFLICT) from exc
    for field in fields:
        camera_count = None
        try:
            camera_count = len(candidate_agent._camera_rows(field))
        except OnlineAgentError:
            pass
        conn.execute(
            """INSERT INTO tenant_integration_stores(
                 integration_id, org_id, parent_id, name, org_type, status, camera_count, synced_at
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                integration_id, field["org_id"], field.get("parent_id"), field["name"],
                field.get("org_type") or "store", "CONNECTED", camera_count, timestamp,
            ),
        )
    integration = one(conn, "SELECT * FROM tenant_integrations WHERE integration_id=?", (integration_id,))
    with _TENANT_AGENT_LOCK:
        _TENANT_AGENT_CACHE[tenant_code] = (fingerprint, candidate_agent)
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "integration.create",
        "tenant_integration",
        integration_id,
        None,
        {"tenant_code": tenant_code, "tenant_name": tenant_name, "store_count": len(fields), "credential_fingerprint": fingerprint},
        "agent_secure_form",
        None,
    )
    return serialize_integration_row(conn, integration)


def descendant_org_ids(conn: sqlite3.Connection, org_id: str) -> list[str]:
    found = [org_id]
    queue = [org_id]
    while queue:
        current = queue.pop(0)
        children = [row["org_id"] for row in rows(conn, "SELECT org_id FROM orgs WHERE parent_id=?", (current,))]
        found.extend(children)
        queue.extend(children)
    return found


def integrated_store_org_ids(conn: sqlite3.Connection, tenant_id: str) -> set[str]:
    integration = one(
        conn,
        "SELECT integration_id FROM tenant_integrations WHERE tenant_code=? AND status='CONNECTED'",
        (tenant_id,),
    )
    if not integration:
        return set()
    return {
        row["org_id"]
        for row in rows(
            conn,
            "SELECT org_id FROM tenant_integration_stores WHERE integration_id=? AND org_type='store'",
            (integration["integration_id"],),
        )
    }


def allowed_org_ids(conn: sqlite3.Connection, user) -> set[str]:
    configured = json_loads(user["allowed_org_ids"], [])
    if "*" in configured:
        allowed = {row["org_id"] for row in rows(conn, "SELECT org_id FROM orgs WHERE tenant_id=?", (user["tenant_id"],))}
        allowed.update(integrated_store_org_ids(conn, user["tenant_id"]))
        return allowed
    allowed = set()
    for org_id in configured:
        if not org_id:
            continue
        allowed.update(descendant_org_ids(conn, str(org_id)))
    return allowed


def assert_org_access(conn: sqlite3.Connection, user, org_ids: list[str]):
    allowed = allowed_org_ids(conn, user)
    denied = [org_id for org_id in org_ids if org_id not in allowed]
    if denied:
        raise ApiError("TENANT_SCOPE_DENIED", HTTPStatus.FORBIDDEN, {"denied_org_ids": denied})


def log_audit(conn, user_id: str, tenant_id: str, action: str, object_type: str, object_id: str, before, after, source: str, plan_id: str | None):
    conn.execute(
        "INSERT INTO audit_logs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"audit_{uuid.uuid4().hex[:12]}",
            user_id,
            tenant_id,
            action,
            object_type,
            object_id,
            json_dumps(before) if before is not None else None,
            json_dumps(after) if after is not None else None,
            source,
            plan_id,
            now_iso(),
        ),
    )


def serialize_camera(camera: dict) -> dict:
    return {
        "camera_id": camera["camera_id"],
        "tenant_id": camera["tenant_id"],
        "org_id": camera["org_id"],
        "name": camera["name"],
        "point_label": camera["point_label"],
        "vendor": camera["vendor"],
        "stream_protocol": camera["stream_protocol"],
        "stream_status": camera["stream_status"],
        "snapshot_url": camera["snapshot_url"],
        "last_online_at": camera["last_online_at"],
        "calibration_status": camera["calibration_status"],
    }


def org_label(conn: sqlite3.Connection, org_id: str) -> str:
    org = one(conn, "SELECT name FROM orgs WHERE org_id=?", (org_id,))
    return org["name"] if org else org_id


def find_org_candidates(conn: sqlite3.Connection, text: str, context_org_id: str | None, user) -> tuple[list[str], dict | None]:
    alias_map = {
        "华东区": ["org_hd"],
        "华南区": ["org_hn"],
        "北京区域": ["org_bj"],
        "北京区": ["org_bj"],
        "北京": ["org_bj"],
        "广州悦汇城": ["org_gz"],
        "广州": ["org_gz"],
        "杭州大悦城": ["org_hz1"],
        "杭州西湖店": ["org_hz2"],
        "杭州店": ["org_hz1", "org_hz2"],
        "上海旗舰店": ["org_sh"],
        "深圳前海店": ["org_sz_store"],
        "深圳": ["org_sz_store"],
    }
    matched = []
    ambiguous = None
    for alias, ids in alias_map.items():
        if alias in text:
            if len(ids) > 1:
                ambiguous = {"raw": alias, "candidates": [{"org_id": i, "name": org_label(conn, i)} for i in ids]}
            matched.extend(ids)
    if is_all_store_scope_request(text):
        configured = json_loads(user["allowed_org_ids"], [])
        base_ids = matched
        if not base_ids:
            base_ids = [row["org_id"] for row in rows(conn, "SELECT org_id FROM orgs WHERE tenant_id=?", (user["tenant_id"],))] if "*" in configured else list(allowed_org_ids(conn, user))
        expanded = []
        for org_id in base_ids:
            expanded.extend(descendant_org_ids(conn, org_id))
        stores = []
        for org_id in expanded:
            org = one(conn, "SELECT org_type FROM orgs WHERE org_id=?", (org_id,))
            if org and org["org_type"] == "store":
                stores.append(org_id)
        return sorted(set(stores)), None
    if not matched and context_org_id:
        matched.append(context_org_id)
    return sorted(set(matched)), ambiguous


def expand_scope(conn: sqlite3.Connection, org_ids: list[str]) -> list[str]:
    expanded = set()
    for org_id in org_ids:
        expanded.update(descendant_org_ids(conn, org_id))
    return sorted(expanded)


def find_capability(conn: sqlite3.Connection, text: str) -> dict | None:
    caps = rows(conn, "SELECT * FROM capabilities WHERE status='ACTIVE'")
    for cap in caps:
        names = [cap["name"]] + json_loads(cap["aliases"], [])
        if any(alias in text for alias in names):
            return cap
    return None


def parse_time_range(text: str) -> dict | None:
    if "昨天" in text:
        day = CURRENT_DATE - timedelta(days=1)
        return {"raw": "昨天", "start": f"{day.isoformat()}T00:00:00+08:00", "end": f"{day.isoformat()}T23:59:59+08:00"}
    if "上周" in text:
        monday = CURRENT_DATE - timedelta(days=CURRENT_DATE.weekday())
        start = monday - timedelta(days=7)
        end = monday - timedelta(days=1)
        return {"raw": "上周", "start": f"{start.isoformat()}T00:00:00+08:00", "end": f"{end.isoformat()}T23:59:59+08:00"}
    if "下周" in text:
        monday = CURRENT_DATE - timedelta(days=CURRENT_DATE.weekday())
        start = monday + timedelta(days=7)
        end = start + timedelta(days=6)
        return {"raw": "下周", "start": f"{start.isoformat()}T00:00:00+08:00", "end": f"{end.isoformat()}T23:59:59+08:00"}
    if "近7天" in text or "近 7 天" in text:
        start = CURRENT_DATE - timedelta(days=6)
        return {"raw": "近7天", "start": f"{start.isoformat()}T00:00:00+08:00", "end": f"{CURRENT_DATE.isoformat()}T23:59:59+08:00"}
    return None


TIME_RANGE_PATTERN = re.compile(
    r"(?:(?:每天|每日|天天)\s*)?"
    r"(?P<start_period>凌晨|早上|上午|中午|下午|晚上)?\s*"
    r"(?P<start_hour>\d{1,2})\s*"
    r"(?:(?:[:：]\s*(?P<start_minute_colon>\d{1,2}))|(?:(?P<start_unit>点|时)\s*(?:(?P<start_minute_cn>\d{1,2})\s*分?|(?P<start_half>半))?)?)"
    r"\s*(?:到|至|~|～|-|—)\s*"
    r"(?P<end_period>凌晨|早上|上午|中午|下午|晚上)?\s*"
    r"(?P<end_hour>\d{1,2})\s*"
    r"(?:(?:[:：]\s*(?P<end_minute_colon>\d{1,2}))|(?:(?P<end_unit>点|时)\s*(?:(?P<end_minute_cn>\d{1,2})\s*分?|(?P<end_half>半))?)?)"
)


def parse_clock_minutes(hour_text: str, minute_text: str | None, period: str | None) -> tuple[int, int] | None:
    hour = normalize_cn_hour(hour_text, period)
    minute = int(minute_text or 0)
    if hour > 24 or minute > 59:
        return None
    if hour == 24 and minute != 0:
        return None
    return hour, minute


def _time_range_minute(match: re.Match, prefix: str) -> str | None:
    if match.group(f"{prefix}_half"):
        return "30"
    return match.group(f"{prefix}_minute_colon") or match.group(f"{prefix}_minute_cn")


def parse_explicit_daily_window(text: str) -> dict | None:
    for match in TIME_RANGE_PATTERN.finditer(text):
        raw = match.group(0)
        has_time_marker = any(
            [
                match.group("start_period"),
                match.group("end_period"),
                match.group("start_unit"),
                match.group("end_unit"),
                ":" in raw,
                "：" in raw,
            ]
        )
        if not has_time_marker:
            continue
        start_minute = _time_range_minute(match, "start")
        end_minute = _time_range_minute(match, "end")
        start_period = match.group("start_period")
        end_period = match.group("end_period") or start_period
        start = parse_clock_minutes(match.group("start_hour"), start_minute, start_period)
        end = parse_clock_minutes(match.group("end_hour"), end_minute, end_period)
        if not start or not end:
            continue
        start_hour, start_min = start
        end_hour, end_min = end
        if start_hour * 60 + start_min >= end_hour * 60 + end_min:
            continue
        return {
            "mode": "daily_window",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "start_time": f"{start_hour:02d}:{start_min:02d}",
            "end_time": f"{end_hour:02d}:{end_min:02d}",
            "label": f"每天 {start_hour:02d}:{start_min:02d}-{end_hour:02d}:{end_min:02d}",
        }
    return None


def parse_schedule(text: str) -> dict | None:
    explicit = parse_explicit_daily_window(text)
    if explicit:
        return explicit
    if "营业时间" in text or "店时" in text:
        return {"mode": "business_hours", "label": "按门店营业时间"}
    return None


def normalize_cn_hour(value: str, period: str | None) -> int:
    hour = int(value)
    if period in {"下午", "晚上"} and hour < 12:
        return hour + 12
    if period == "中午" and hour < 11:
        return hour + 12
    if period in {"上午", "早上", "凌晨"} and hour == 12:
        return 0
    return hour


def parse_threshold(text: str, capability: dict | None = None) -> dict:
    threshold = json_loads(capability["thresholds_default"], {}) if capability else {}
    minute = re.search(r"超过\s*(\d+)\s*分钟", text)
    second = re.search(r"超过\s*(\d+)\s*秒", text)
    if minute:
        threshold["duration_seconds"] = int(minute.group(1)) * 60
    if second:
        threshold["duration_seconds"] = int(second.group(1))
    if capability and capability["capability_id"] == VISUAL_COMPLIANCE_CAPABILITY_ID:
        threshold["visual_compliance"] = extract_visual_compliance_pack(text)
        threshold["low_confidence_to_pending"] = True
        threshold["require_marked_anomaly_image"] = True
    return threshold


def classify_intent(text: str) -> tuple[str, float]:
    if any(word in text for word in ["误报", "真警", "忽略", "badcase", "反馈"]):
        return "FEEDBACK_CREATE", 0.91
    subscription_verbs = ["订阅", "创建", "上线", "开通", "启用", "部署", "配置", "接入"]
    inspection_terms = ["离岗", "抽烟", "迎宾", "消防通道", "巡检", "检测能力", "能力", "视觉合规", "物料合规", "其他品牌", "竞品", "Logo", "logo", "海报", "立牌", "展架", "座椅", "电视广告", "其他品牌汽车"]
    if is_visual_compliance_request(text) and any(word in text for word in [*subscription_verbs, "巡检", "布防"]):
        return "SUBSCRIPTION_CREATE", 0.94
    if any(word in text for word in subscription_verbs) and any(word in text for word in inspection_terms):
        return "SUBSCRIPTION_CREATE", 0.93
    if any(word in text for word in ["统计", "排行", "最多", "Top", "TOP", "趋势", "误报率"]):
        return "DATA_STATS", 0.90
    if any(word in text for word in ["告警", "事件", "有哪些", "证据"]) and any(word in text for word in ["昨天", "上周", "近7天", "离岗", "抽烟", "消防"]):
        return "RESULT_QUERY", 0.88
    if "摄像头" in text:
        return "CAMERA_SEARCH", 0.86
    if any(word in text for word in ["帮助", "怎么", "能做什么"]):
        return "HELP", 0.95
    return "HELP", 0.55


def create_conversation(conn, user, title="新的巡检对话", page_code="agi-inspection", org_id=None) -> dict:
    conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    conn.execute(
        "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?)",
        (conversation_id, user["user_id"], user["tenant_id"], title, "ACTIVE", page_code, org_id, ts, ts),
    )
    return one(conn, "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,))


SIGNED_MEDIA_URL_MARKERS = (
    "ossaccesskeyid=",
    "signature=",
    "expires=",
    "x-amz-signature=",
    "x-amz-credential=",
    "x-oss-",
)


def _image_mime_type(value: str | None) -> str:
    mime_type = str(value or "").split(";", 1)[0].strip().lower()
    return mime_type if mime_type in {"image/jpeg", "image/png", "image/webp"} else "image/jpeg"


def archive_online_snapshot(conn: sqlite3.Connection, tenant_id: str, media: dict) -> dict:
    """Persist a just-captured vendor image and expose it only through our proxy.

    Vendor snapshot links are signed, short-lived URLs.  Persisting the link is
    both unsafe and unreliable: storage redacts the signature, which means the
    browser can no longer load it.  Archive the image while the link is valid and
    replace every UI-facing URL with a tenant-bound, random-token proxy URL.
    """
    source_url = str(media.get("snapshot_url") or "").strip()
    if not source_url:
        return media
    if source_url.startswith("/api/online-snapshot-evidence/"):
        return media
    if source_url.startswith("data:image/"):
        return media
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return media

    request_obj = urlrequest.Request(
        source_url,
        headers={"User-Agent": "WanxiangAGIInspection/0.3", "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8"},
    )
    content = b""
    mime_type = "image/jpeg"
    last_error = None
    for attempt in range(2):
        try:
            with urlrequest.urlopen(request_obj, timeout=20) as response:
                content = response.read(12 * 1024 * 1024 + 1)
                mime_type = _image_mime_type(response.headers.get("Content-Type"))
            break
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
    if not content:
        raise OnlineAgentError("UPSTREAM_UNAVAILABLE", "监控快照安全归档失败") from last_error
    if len(content) > 12 * 1024 * 1024:
        raise OnlineAgentError("UPSTREAM_INVALID_RESPONSE", "监控快照大小异常")

    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime_type]
    evidence_id = f"os_{uuid.uuid4().hex[:16]}"
    access_token = secrets.token_urlsafe(32)
    tenant_folder = re.sub(r"[^A-Za-z0-9_.-]+", "_", tenant_id)[:80] or "tenant"
    folder = ONLINE_SNAPSHOT_EVIDENCE_DIR / tenant_folder
    folder.mkdir(parents=True, exist_ok=True)
    storage_path = folder / f"{evidence_id}{extension}"
    storage_path.write_bytes(content)
    captured_at = str(media.get("captured_at") or now_iso())[:64]
    conn.execute(
        "INSERT INTO online_snapshot_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            tenant_id,
            str(media.get("org_id") or "")[:128],
            str(media.get("camera_id") or "")[:128],
            str(media.get("camera_name") or "未知镜头")[:160],
            captured_at,
            str(storage_path),
            mime_type,
            hashlib.sha256(content).hexdigest(),
            access_token,
            len(content),
            now_iso(),
        ),
    )
    return {
        **media,
        "snapshot_url": f"/api/online-snapshot-evidence/{evidence_id}?access_token={access_token}",
        "evidence_id": evidence_id,
    }


def archive_online_response_snapshots(conn: sqlite3.Connection, tenant_id: str, response: dict) -> None:
    """Replace every immediate inspection image with a durable proxy URL."""
    media = response.get("media")
    archived_by_source: dict[str, dict] = {}

    def archive(media_item: dict) -> dict:
        source_url = str(media_item.get("snapshot_url") or "")
        if not source_url:
            return media_item
        if source_url in archived_by_source:
            return {**media_item, **archived_by_source[source_url]}
        archived = archive_online_snapshot(conn, tenant_id, media_item)
        replacement = {
            key: archived.get(key)
            for key in ("snapshot_url", "evidence_id")
            if archived.get(key) is not None
        }
        archived_by_source[source_url] = replacement
        return archived

    if isinstance(media, dict) and media.get("kind") == "IMAGE":
        response["media"] = archive(media)
    gallery = response.get("media_gallery")
    if isinstance(gallery, list):
        response["media_gallery"] = [archive(item) if isinstance(item, dict) else item for item in gallery]
    visual_context = response.get("_visual_context")
    if isinstance(visual_context, dict) and isinstance(visual_context.get("images"), list):
        visual_context["images"] = [
            archive(item) if isinstance(item, dict) else item
            for item in visual_context["images"]
        ]


def restore_archived_context_public_urls(conn: sqlite3.Connection, tenant_id: str, response: dict) -> None:
    """Replace internal model-only data URLs with tenant-scoped proxy URLs."""

    def restore(item: dict) -> dict:
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id or not str(item.get("snapshot_url") or "").startswith("data:image/"):
            return item
        evidence = one(
            conn,
            "SELECT access_token FROM online_snapshot_evidence WHERE evidence_id=? AND tenant_id=?",
            (evidence_id, tenant_id),
        )
        if not evidence:
            return {key: value for key, value in item.items() if key != "snapshot_url"}
        return {
            **item,
            "snapshot_url": f"/api/online-snapshot-evidence/{evidence_id}?access_token={evidence['access_token']}",
        }

    if isinstance(response.get("media"), dict):
        response["media"] = restore(response["media"])
    if isinstance(response.get("media_gallery"), list):
        response["media_gallery"] = [restore(item) if isinstance(item, dict) else item for item in response["media_gallery"]]
    visual_context = response.get("_visual_context")
    if isinstance(visual_context, dict) and isinstance(visual_context.get("images"), list):
        visual_context["images"] = [restore(item) if isinstance(item, dict) else item for item in visual_context["images"]]


def sanitize_linked_object_for_storage(value):
    if isinstance(value, dict):
        sanitized = {str(key): sanitize_linked_object_for_storage(item) for key, item in value.items()}
        setup = (sanitized.get("artifact") or {}).get("integrationSetup") if isinstance(sanitized.get("artifact"), dict) else None
        if isinstance(setup, dict):
            prefill = setup.get("prefill")
            if isinstance(prefill, dict):
                secure_fields = sorted(
                    {
                        key
                        for key in INTEGRATION_SECRET_FIELDS
                        if prefill.get(key) or key in set(setup.get("secure_prefill_fields") or [])
                    }
                )
                setup["prefill"] = {
                    key: item
                    for key, item in prefill.items()
                    if key not in INTEGRATION_SECRET_FIELDS
                }
                if secure_fields:
                    setup["secure_prefill_fields"] = secure_fields
                else:
                    setup.pop("secure_prefill_fields", None)
            setup.pop("transient_secret_prefill", None)
        return sanitized
    if isinstance(value, list):
        return [sanitize_linked_object_for_storage(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if "?" in value and any(marker in lowered for marker in SIGNED_MEDIA_URL_MARKERS):
            return f"{value.split('?', 1)[0]}?signature_redacted=1"
    return value


def add_message(conn, conversation_id: str, sender: str, content: str, linked_plan_id=None, linked_object=None) -> dict:
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    stored_linked_object = sanitize_linked_object_for_storage(linked_object) if linked_object is not None else None
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
        (
            message_id,
            conversation_id,
            sender,
            content,
            linked_plan_id,
            json_dumps(stored_linked_object) if stored_linked_object is not None else None,
            now_iso(),
        ),
    )
    conn.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", (now_iso(), conversation_id))
    return one(conn, "SELECT * FROM messages WHERE message_id=?", (message_id,))


def serialize_message(message: dict, conn: sqlite3.Connection | None = None) -> dict:
    item = dict(message)
    linked_object = json_loads(item.get("linked_object"), None)
    linked_plan_id = item.get("linked_plan_id")
    if not linked_plan_id and isinstance(linked_object, dict):
        linked_plan = linked_object.get("plan")
        if isinstance(linked_plan, dict):
            linked_plan_id = linked_plan.get("plan_id")
    if isinstance(linked_object, dict):
        linked_object.pop("visual_context", None)
        scheduled_run = (linked_object.get("artifact") or {}).get("scheduledRun")
        run_id = scheduled_run.get("run_id") if isinstance(scheduled_run, dict) else None
        if conn is not None and run_id:
            run = one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,))
            if run:
                task = one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (run["task_id"],))
                evidence_ids = json_loads(run.get("evidence_ids"), [])
                evidence = rows(
                    conn,
                    f"SELECT * FROM scheduled_evidence WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)}) ORDER BY captured_at",
                    evidence_ids,
                ) if evidence_ids else []
                if task:
                    linked_object["artifact"]["scheduledRun"] = scheduled_run_artifact(task, run, evidence)
    elif linked_plan_id and conn is not None:
        linked_object = {}
    if conn is not None and linked_plan_id and isinstance(linked_object, dict):
        current_plan = one(conn, "SELECT * FROM plans WHERE plan_id=?", (linked_plan_id,))
        if current_plan:
            linked_object["plan"] = serialize_plan(current_plan)
    item["linked_object"] = linked_object
    return item


def conversation_artifact(response: dict) -> dict | None:
    artifact = {}
    conversation_context = response.get("conversation_context") if isinstance(response.get("conversation_context"), dict) else None
    if conversation_context and conversation_context.get("status") == "ACTIVE":
        artifact["conversationScope"] = {
            "context_id": trace_text(conversation_context.get("context_id"), 64),
            "version": conversation_context.get("version"),
            "domain": trace_text(conversation_context.get("domain"), 48),
            "page_scope": conversation_context.get("page_scope") if isinstance(conversation_context.get("page_scope"), dict) else {},
            "task_scope": conversation_context.get("task_scope") if isinstance(conversation_context.get("task_scope"), dict) else {},
            "scope_operation": trace_text(conversation_context.get("scope_operation"), 48),
            "evidence_mode": trace_text(conversation_context.get("evidence_mode"), 48),
            "reason_code": trace_text(conversation_context.get("reason_code"), 80),
        }
    web_search = response.get("web_search") if isinstance(response.get("web_search"), dict) else None
    if web_search:
        citations = web_search.get("citations") if isinstance(web_search.get("citations"), list) else []
        temporal_context = web_search.get("temporal_context") if isinstance(web_search.get("temporal_context"), dict) else {}
        artifact["webSearch"] = {
            "query": trace_text(web_search.get("query"), 320),
            "provider": trace_text(web_search.get("provider"), 32),
            "topic": trace_text(web_search.get("topic"), 32) or None,
            "status": trace_text(web_search.get("status"), 32) or None,
            "error_code": trace_text(web_search.get("error_code"), 64) or None,
            "fetched_at": trace_text(web_search.get("fetched_at"), 64),
            "request_id": trace_text(web_search.get("request_id"), 128) or None,
            "freshness": trace_text(web_search.get("freshness"), 32),
            "temporal_context": {
                "reference_time": trace_text(temporal_context.get("reference_time"), 64) or None,
                "target_date": trace_text(temporal_context.get("target_date"), 16) or None,
                "timezone": trace_text(temporal_context.get("timezone"), 32) or None,
                "scope": trace_text(temporal_context.get("scope"), 32) or None,
                "query_rewrite": trace_text(temporal_context.get("query_rewrite"), 48) or None,
                "weather_location": trace_text(temporal_context.get("weather_location"), 64) or None,
                "travel_year": temporal_context.get("travel_year") if isinstance(temporal_context.get("travel_year"), int) else None,
            } if temporal_context else None,
            "citations": [
                {
                    "title": trace_text(item.get("title"), 180),
                    "url": trace_text(item.get("url"), 500),
                    "snippet": trace_text(item.get("snippet"), 600),
                    "published_at": trace_text(item.get("published_at"), 48) or None,
                    "domain": trace_text(item.get("domain"), 255),
                }
                for item in citations[:8]
                if isinstance(item, dict) and str(item.get("url") or "").startswith(("https://", "http://"))
            ],
        }
    travel_guide = response.get("travel_guide") if isinstance(response.get("travel_guide"), dict) else None
    if travel_guide:
        def safe_place_items(key: str) -> list[dict]:
            items = travel_guide.get(key) if isinstance(travel_guide.get(key), list) else []
            return [
                {
                    "name": trace_text(item.get("name"), 120),
                    "address": trace_text(item.get("address"), 180),
                    "address_verified": bool(item.get("address_verified")),
                    "summary": trace_text(item.get("summary"), 500),
                    "source_title": trace_text(item.get("source_title"), 180),
                    "source_url": trace_text(item.get("source_url"), 500),
                    "place_data_url": trace_text(item.get("place_data_url"), 500) or None,
                    "map_url": trace_text(item.get("map_url"), 500),
                }
                for item in items[:4]
                if isinstance(item, dict)
                and bool(item.get("address_verified"))
                and is_specific_venue_name(
                    str(item.get("name") or ""),
                    key,
                    str(item.get("summary") or ""),
                    str(travel_guide.get("destination") or ""),
                )
                and is_precise_venue_address(str(item.get("address") or ""))
                and str(item.get("source_url") or "").startswith(("https://", "http://"))
                and str(item.get("map_url") or "").startswith("https://www.google.com/maps/")
            ]

        images = travel_guide.get("images") if isinstance(travel_guide.get("images"), list) else []
        artifact["travelGuide"] = {
            "destination": trace_text(travel_guide.get("destination"), 80),
            "days": bounded_int(travel_guide.get("days"), 5, 1, 14),
            "travel_year": travel_guide.get("travel_year") if isinstance(travel_guide.get("travel_year"), int) else None,
            "hotels": safe_place_items("hotels"),
            "restaurants": safe_place_items("restaurants"),
            "images": [
                {
                    "title": trace_text(item.get("title"), 180),
                    "thumbnail_url": trace_text(item.get("thumbnail_url"), 500),
                    "author": trace_text(item.get("author"), 180),
                    "license": trace_text(item.get("license"), 80),
                    "source_url": trace_text(item.get("source_url"), 500),
                    "kind": trace_text(item.get("kind"), 24),
                }
                for item in images[:3]
                if isinstance(item, dict)
                and str(item.get("thumbnail_url") or "").startswith("https://upload.wikimedia.org/")
                and str(item.get("source_url") or "").startswith("https://commons.wikimedia.org/")
            ],
            "recommendation_notice": trace_text(travel_guide.get("recommendation_notice"), 300),
            "places_provider": trace_text(travel_guide.get("places_provider"), 40) or None,
            "places_attribution_url": trace_text(travel_guide.get("places_attribution_url"), 500) or None,
        }
    generated_document = response.get("generated_document") if isinstance(response.get("generated_document"), dict) else None
    if generated_document:
        artifact["generatedDocument"] = {
            "document_id": trace_text(generated_document.get("document_id"), 64),
            "title": trace_text(generated_document.get("title"), 120),
            "filename": trace_text(generated_document.get("filename"), 160),
            "mime_type": trace_text(generated_document.get("mime_type"), 80),
            "size_bytes": int(generated_document.get("size_bytes") or 0),
            "created_at": trace_text(generated_document.get("created_at"), 64),
            "download_url": trace_text(generated_document.get("download_url"), 240),
        }
    media = response.get("media") if isinstance(response.get("media"), dict) else None
    if media:
        allowed_media = {
            key: media.get(key)
            for key in ("session_id", "kind", "camera_name", "org_name", "snapshot_url", "captured_at", "expires_at", "time_range")
            if media.get(key) is not None
        }
        if media.get("kind") in {"LIVE", "PLAYBACK"}:
            allowed_media["status"] = "EXPIRED"
        artifact["media"] = allowed_media
    media_gallery = response.get("media_gallery") if isinstance(response.get("media_gallery"), list) else []
    if media_gallery:
        artifact["mediaGallery"] = [
            {
                key: item.get(key)
                for key in (
                    "session_id",
                    "kind",
                    "camera_name",
                    "org_name",
                    "snapshot_url",
                    "captured_at",
                    "expires_at",
                    "is_anomalous",
                    "is_target_evidence",
                    "sku_labels",
                    "analysis_pending",
                    "analysis_note",
                )
                if item.get(key) is not None
            }
            for item in media_gallery
            if isinstance(item, dict) and item.get("snapshot_url")
        ]
    if isinstance(response.get("visual_result"), dict):
        artifact["visualResult"] = response["visual_result"]
    if isinstance(response.get("device_status"), dict):
        device_status = response["device_status"]
        artifact["deviceStatus"] = {
            "summary": device_status.get("summary") or {},
            "cameras": [
                {
                    "name": item.get("name"),
                    "point_label": item.get("point_label"),
                    "stream_status": item.get("stream_status"),
                }
                for item in device_status.get("cameras") or []
                if isinstance(item, dict)
            ],
            "servers": device_status.get("servers") or [],
            "source": device_status.get("source"),
        }
    if isinstance(response.get("applications"), list):
        artifact["applications"] = [
            {
                key: item.get(key)
                for key in ("org_name", "capability_id", "name", "status", "scene", "version")
                if item.get(key) is not None
            }
            for item in response["applications"]
            if isinstance(item, dict)
        ]
    if isinstance(response.get("pipeline"), dict):
        artifact["pipeline"] = response["pipeline"]
    if isinstance(response.get("choices"), dict):
        artifact["choices"] = response["choices"]
    return artifact or None


def trace_text(value, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def trace_redact(value):
    if isinstance(value, dict):
        safe = {}
        for key, child in value.items():
            lower_key = str(key).lower()
            if any(secret in lower_key for secret in ("secret", "password", "token", "authorization")):
                safe[key] = "[已隐藏]"
            else:
                safe[key] = trace_redact(child)
        return safe
    if isinstance(value, list):
        return [trace_redact(item) for item in value[:20]]
    if isinstance(value, str):
        return value[:1200]
    return value


def trace_confidence(value) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "未知"
    if score <= 1:
        return f"{round(score * 100)}%"
    return f"{round(score)}%"


def trace_is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) == 0
    if isinstance(value, str):
        return not value.strip()
    return False


def trace_camera_labels(items, limit: int = 12) -> list[str]:
    labels = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        camera = item.get("camera_name") or item.get("name") or "未知镜头"
        org = item.get("org_name")
        captured_at = item.get("captured_at")
        label = str(camera)
        if org:
            label = f"{org} · {label}"
        if captured_at:
            label = f"{label} · {captured_at}"
        labels.append(trace_text(label, 160))
        if len(labels) >= limit:
            break
    return labels


def trace_visual_brief(visual_result: dict) -> dict:
    return {
        key: visual_result.get(key)
        for key in (
            "question",
            "status",
            "conclusion",
            "confidence",
            "business_policy",
            "business_reason",
            "target_observed",
            "subject_present",
            "evidence_type",
            "image_count",
            "failed_image_count",
            "failed_camera_names",
            "batch_count",
            "candidate_batch_size",
            "model",
            "source",
        )
        if visual_result.get(key) is not None
    }


def trace_node(
    node_id: str,
    title: str,
    kind: str,
    status: str,
    summary: str,
    detail=None,
    input_data=None,
    output_data=None,
    reasoning=None,
) -> dict:
    node = {
        "node_id": node_id,
        "title": title,
        "kind": kind,
        "status": status,
        "summary": trace_text(summary, 600),
        "detail": trace_redact(detail or {}),
    }
    if not trace_is_empty(input_data):
        node["input"] = trace_redact(input_data)
    if not trace_is_empty(output_data):
        node["output"] = trace_redact(output_data)
    if not trace_is_empty(reasoning):
        node["reasoning"] = trace_text(reasoning, 900)
    return node


def trace_tool_summary(tool_name: str, artifact: dict | None) -> tuple[str, dict, dict, dict, str]:
    artifact = artifact or {}
    plan = artifact.get("plan") if isinstance(artifact.get("plan"), dict) else {}
    plan_slots = plan.get("slots") if isinstance(plan.get("slots"), dict) else {}
    visual_result = artifact.get("visualResult") if isinstance(artifact.get("visualResult"), dict) else {}
    scheduled_run = artifact.get("scheduledRun") if isinstance(artifact.get("scheduledRun"), dict) else {}
    batch_inspection = artifact.get("batchInspection") if isinstance(artifact.get("batchInspection"), dict) else {}
    media_gallery = artifact.get("mediaGallery") if isinstance(artifact.get("mediaGallery"), list) else []
    media = artifact.get("media") if isinstance(artifact.get("media"), dict) else {}
    device_status = artifact.get("deviceStatus") if isinstance(artifact.get("deviceStatus"), dict) else {}
    applications = artifact.get("applications") if isinstance(artifact.get("applications"), list) else []
    choices = artifact.get("choices") if isinstance(artifact.get("choices"), dict) else {}
    web_search = artifact.get("webSearch") if isinstance(artifact.get("webSearch"), dict) else {}
    generated_document = artifact.get("generatedDocument") if isinstance(artifact.get("generatedDocument"), dict) else {}
    conversation_scope = artifact.get("conversationScope") if isinstance(artifact.get("conversationScope"), dict) else {}
    base_tool = tool_name.split(":", 1)[0]
    detail = {"tool": tool_name}
    input_data = {"tool_call": tool_name}
    output_data = {}
    reasoning = "Agent 根据已识别意图和当前租户、门店上下文选择该工具执行。"
    if base_tool == "conversation.context.resolve":
        summary = "解析当前输入与活动任务的多轮关系。"
        detail.update(
            {
                "context_id": conversation_scope.get("context_id"),
                "version": conversation_scope.get("version"),
                "scope_operation": conversation_scope.get("scope_operation"),
            }
        )
        input_data.update({"page_scope": conversation_scope.get("page_scope")})
        output_data.update(
            {
                "domain": conversation_scope.get("domain"),
                "reason_code": conversation_scope.get("reason_code"),
                "scope_operation": conversation_scope.get("scope_operation"),
            }
        )
        reasoning = "先判断本轮是新任务还是上一轮的对象、属性、关系、位置或范围续问，避免开放问答提前抢路由。"
    elif base_tool == "conversation.context.recover":
        summary = "从最近可信视觉消息恢复旧会话的任务语义。"
        detail.update({"migration": "lazy", "trusted_source": conversation_scope.get("reason_code")})
        input_data.update({"source": "persisted_revision_or_visual_message", "vendor_url_reused": False})
        output_data.update({"semantic_context_recovered": True, "evidence_reauthorized": True})
        reasoning = "旧会话可能创建于 context revision 上线前；只恢复任务语义和受控引用，范围、权限和证据时效仍在本轮重新校验。"
    elif base_tool == "permission.scope.check":
        summary = "按当前用户实时组织权限校验本轮门店范围。"
        detail.update({"result": "authorized"})
        input_data.update({"tenant_bound": True, "task_scope": conversation_scope.get("task_scope")})
        output_data.update({"authorized": True, "denied_store_names_exposed": False})
        reasoning = "单店和多店共用服务端权限门禁；权限检查先于摄像头、抓图和模型调用。"
    elif base_tool == "scope.resolve":
        task_scope = conversation_scope.get("task_scope") if isinstance(conversation_scope.get("task_scope"), dict) else {}
        names = task_scope.get("org_names") if isinstance(task_scope.get("org_names"), list) else []
        summary = f"解析本轮实际范围：{len(names)} 家门店。"
        detail.update({"scope_type": task_scope.get("type"), "scope_source": task_scope.get("source")})
        input_data.update({"page_scope": conversation_scope.get("page_scope"), "scope_operation": conversation_scope.get("scope_operation")})
        output_data.update({"store_count": len(names), "store_names": names})
        reasoning = "页面当前门店只作为默认值；显式门店和对话范围操作优先形成实际执行范围。"
    elif base_tool == "evidence.resolve":
        summary = "读取并校验上一轮归档证据，用于明确的同帧追问。"
        detail.update({"evidence_mode": conversation_scope.get("evidence_mode")})
        input_data.update({"source": "tenant_scoped_archived_evidence", "signed_vendor_url_reused": False})
        output_data.update({"integrity_checked": True, "permission_rechecked": True})
        reasoning = "只按证据 ID 读取受控归档文件，并校验租户、门店权限、路径和 SHA-256；不复用供应商临时地址。"
    elif base_tool == "web.search":
        citations = web_search.get("citations") if isinstance(web_search.get("citations"), list) else []
        search_status = str(web_search.get("status") or "").upper()
        if ":blocked" in tool_name:
            summary = "公共网页检索未执行：问题包含不允许外发的上下文或敏感信息。"
        elif ":unavailable" in tool_name:
            summary = "公共网页检索未执行：服务尚未配置。"
        elif ":failed" in tool_name:
            summary = "公共网页检索执行失败，未使用未核验的实时信息作答。"
        elif citations:
            summary = f"检索公开网页并保留 {len(citations)} 条可访问来源。"
        else:
            summary = "已检索公开网页，但未找到可用于核验的可靠来源。"
        detail.update(
            {
                "provider": web_search.get("provider"),
                "citation_count": len(citations),
                "fetched_at": web_search.get("fetched_at"),
            }
        )
        input_data.update(
            {
                "query": web_search.get("query"),
                "freshness": web_search.get("freshness"),
                "topic": web_search.get("topic"),
                "temporal_context": web_search.get("temporal_context"),
                "tenant_context_sent": False,
            }
        )
        output_data.update(
            {
                "provider": web_search.get("provider"),
                "citation_count": len(citations),
                "citations": [
                    {
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "published_at": item.get("published_at"),
                    }
                    for item in citations[:8]
                    if isinstance(item, dict)
                ],
                "request_id": web_search.get("request_id"),
                "status": (
                    "blocked" if ":blocked" in tool_name
                    else "unavailable" if ":unavailable" in tool_name
                    else "failed" if ":failed" in tool_name or search_status == "FAILED"
                    else "no_results" if search_status == "NO_RESULTS"
                    else "completed"
                ),
            }
        )
        reasoning = "仅在开放问答判断为需要实时公共事实时调用；工具只接收用户问题文本，返回后通过来源链接供用户核验。"
    elif base_tool == "document.generate_pdf":
        failed = ":failed" in tool_name
        summary = "PDF 文档生成失败，文本回答仍然可用。" if failed else "已把本轮开放问答结果生成 PDF 文档。"
        detail.update(
            {
                "document_id": generated_document.get("document_id"),
                "filename": generated_document.get("filename"),
                "mime_type": generated_document.get("mime_type"),
            }
        )
        input_data.update({"format": "PDF", "enterprise_context_included": False})
        output_data.update(
            {
                "status": "failed" if failed else "completed",
                "document_id": generated_document.get("document_id"),
                "size_bytes": generated_document.get("size_bytes"),
                "download_url": generated_document.get("download_url"),
            }
        )
        reasoning = "仅根据本轮开放问答文本与公开来源生成文档，不读取或写入巡检业务数据。"
    elif base_tool == "paas.media.snapshot":
        evidence = scheduled_run.get("evidence") if isinstance(scheduled_run.get("evidence"), list) else []
        count = len(media_gallery) or len(evidence) or scheduled_run.get("image_count") or (1 if media.get("snapshot_url") else 0)
        summary = f"抓取监控快照 {count} 张，用于后续视觉分析。"
        detail.update({"snapshot_count": count, "camera": media.get("camera_name")})
        input_data.update(
            {
                "scope": "当前租户/门店/匹配点位",
                "requested_media": "snapshot",
                "inspection_goal": visual_result.get("question") or scheduled_run.get("inspection_goal"),
            }
        )
        output_data.update(
            {
                "snapshot_count": count,
                "cameras": trace_camera_labels(media_gallery or evidence or ([media] if media else [])),
                "failed_image_count": visual_result.get("failed_image_count"),
            }
        )
        reasoning = "先把用户目标转成可供视觉模型识别的画面输入；多镜头任务会保留同一批次快照，保证后续判断可追溯。"
    elif base_tool in {"vlm.image.inspect", "vlm.reasoner"}:
        summary = f"调用视觉大模型 {visual_result.get('model') or scheduled_run.get('model_version') or '未记录'} 完成图像判断。"
        detail.update(
            {
                "model": visual_result.get("model") or scheduled_run.get("model_version"),
                "image_count": visual_result.get("image_count") or scheduled_run.get("image_count"),
                "source": visual_result.get("source"),
            }
        )
        input_data.update(
            {
                "question": visual_result.get("question") or scheduled_run.get("inspection_goal"),
                "model": visual_result.get("model") or scheduled_run.get("model_version"),
                "image_count": visual_result.get("image_count") or scheduled_run.get("image_count"),
                "selected_camera_names": visual_result.get("selected_camera_names"),
            }
        )
        output_data.update(trace_visual_brief(visual_result) or {
            "result_status": scheduled_run.get("result_status"),
            "conclusion": scheduled_run.get("conclusion"),
            "confidence": scheduled_run.get("confidence"),
        })
        reasoning = "视觉模型只基于本批次监控图片输出结构化结论；节点保留模型返回摘要，便于核对模型是否产生了异常描述。"
    elif base_tool == "evidence.archive":
        count = scheduled_run.get("image_count") or len(media_gallery)
        summary = f"归档证据 {count} 张，后续可在告警与证据中追溯。"
        detail.update({"evidence_count": count, "run_id": scheduled_run.get("run_id")})
        input_data.update({"run_id": scheduled_run.get("run_id"), "evidence_count": count})
        output_data.update({"archived": True, "run_id": scheduled_run.get("run_id"), "anomaly_evidence_ids": scheduled_run.get("anomaly_evidence_ids")})
        reasoning = "把本次巡检的画面和异常标记写入证据归档，避免聊天关闭后过程数据丢失。"
    elif base_tool == "scheduler.run.persist":
        summary = f"保存周期巡检结果：{scheduled_run.get('run_id') or '当前批次'}。"
        detail.update({"run_id": scheduled_run.get("run_id"), "status": scheduled_run.get("status")})
        input_data.update({"task_id": scheduled_run.get("task_id"), "run_id": scheduled_run.get("run_id"), "result_status": scheduled_run.get("result_status")})
        output_data.update({"status": scheduled_run.get("status"), "conclusion": scheduled_run.get("conclusion"), "completed_at": scheduled_run.get("completed_at")})
        reasoning = "将模型判断和业务复核后的结果落库，供订阅任务、历史巡检和告警证据页面复用。"
    elif base_tool == "paas.camera.page":
        camera_scope = plan_slots.get("camera_scope") if isinstance(plan_slots.get("camera_scope"), dict) else {}
        org_scope = plan_slots.get("org_scope") if isinstance(plan_slots.get("org_scope"), dict) else {}
        store_tasks = camera_scope.get("store_tasks") if isinstance(camera_scope.get("store_tasks"), list) else []
        if camera_scope:
            total = camera_scope.get("total_camera_count")
            if total is None:
                total = len(camera_scope.get("resolved_ids") or [])
            online = camera_scope.get("online_camera_count")
            offline = camera_scope.get("offline_camera_count")
            store_count = org_scope.get("store_count") or len(store_tasks)
            summary = f"解析巡检范围：{store_count} 家门店，{total or 0} 路摄像头。"
            if online is not None or offline is not None:
                summary = f"解析巡检范围：{store_count} 家门店，{online or 0} 路在线、{offline or 0} 路离线。"
            stores = []
            for item in store_tasks[:12]:
                if not isinstance(item, dict):
                    continue
                stores.append(
                    {
                        "org_id": item.get("org_id"),
                        "org_name": item.get("org_name"),
                        "online_camera_count": item.get("online_camera_count"),
                        "offline_camera_count": item.get("offline_camera_count"),
                        "total_camera_count": item.get("total_camera_count"),
                        "status": item.get("status"),
                    }
                )
            detail.update(
                {
                    "store_count": store_count,
                    "camera_count": total or 0,
                    "online_camera_count": online,
                    "offline_camera_count": offline,
                }
            )
            input_data.update(
                {
                    "scope": org_scope or "当前租户/门店",
                    "query_type": "multi_store_camera_scope",
                }
            )
            output_data.update(
                {
                    "store_count": store_count,
                    "online_camera_count": online,
                    "offline_camera_count": offline,
                    "total_camera_count": total or 0,
                    "stores": stores,
                    "candidate_cameras": [
                        trace_text(str(name), 160)
                        for name in (camera_scope.get("resolved_names") or [])[:12]
                    ],
                }
            )
            reasoning = "根据用户指定的门店范围解析可执行门店和在线镜头；多门店任务会按门店拆分子任务，并把不可执行门店留在计划中提示。"
        else:
            count = len(media_gallery) or len(device_status.get("cameras") or []) or 0
            if choices.get("locations"):
                count = len(choices.get("locations") or [])
            summary = f"查询并解析当前范围点位/摄像头，返回 {count} 个候选。"
            detail.update({"candidate_count": count})
            input_data.update({"scope": "当前租户/门店", "query_type": "camera_or_location_candidates"})
            output_data.update(
                {
                    "candidate_count": count,
                    "candidate_cameras": trace_camera_labels(media_gallery or device_status.get("cameras") or choices.get("locations") or []),
                }
            )
            reasoning = "先根据当前门店和用户的位置词匹配候选镜头；如果匹配不确定，进入多轮确认。"
    elif base_tool == "paas.device.status":
        summary = f"查询设备状态，摄像头 {len(device_status.get('cameras') or [])} 路。"
        detail.update({"summary": device_status.get("summary")})
        input_data.update({"scope": "当前租户/门店", "query_type": "device_status"})
        output_data.update({"summary": device_status.get("summary"), "camera_count": len(device_status.get("cameras") or [])})
        reasoning = "设备状态查询只读取线上 PaaS 当前状态，不做视觉模型推断。"
    elif base_tool == "paas.application.list":
        summary = f"查询已上线应用 {len(applications)} 个。"
        detail.update({"application_count": len(applications)})
        input_data.update({"scope": "当前租户/门店", "query_type": "application_list"})
        output_data.update({"application_count": len(applications), "applications": [item.get("name") for item in applications[:12] if isinstance(item, dict)]})
        reasoning = "用于回答门店已订阅或已上线的应用能力，属于只读查询。"
    elif base_tool in {"batch_inspection.execute", "batch_inspection.create"}:
        scope = batch_inspection.get("scope_snapshot") if isinstance(batch_inspection.get("scope_snapshot"), dict) else {}
        schedule = scope.get("schedule") if isinstance(scope.get("schedule"), dict) else {}
        total = batch_inspection.get("total_store_count") or 0
        success = batch_inspection.get("success_store_count") or 0
        failed = batch_inspection.get("failed_store_count") or 0
        skipped = batch_inspection.get("skipped_store_count") or 0
        summary = f"执行批量巡检编排：{total} 家门店，成功 {success} 家，失败 {failed} 家，跳过 {skipped} 家。"
        detail.update(
            {
                "batch_id": batch_inspection.get("batch_id"),
                "execution_mode": batch_inspection.get("execution_mode"),
                "status": batch_inspection.get("status"),
                "item_count": len(batch_inspection.get("items") or []),
            }
        )
        input_data.update(
            {
                "scope": scope.get("org_scope"),
                "inspection_goal": scope.get("inspection_goal"),
                "schedule": schedule,
                "failure_policy": scope.get("failure_policy"),
            }
        )
        output_data.update(
            {
                "batch_id": batch_inspection.get("batch_id"),
                "status": batch_inspection.get("status"),
                "success_store_count": success,
                "failed_store_count": failed,
                "skipped_store_count": skipped,
            }
        )
        reasoning = "批量执行先创建父任务，再按门店拆分子任务；单店失败不会阻断其他门店，最终按门店维度保留成功、失败、跳过和重试线索。"
    elif base_tool.startswith("paas.auth.verify"):
        summary = "验证租户凭证并尝试同步门店。"
        input_data.update({"credential_fields": ["tenant_name", "tenant_code", "app_key", "app_secret"]})
        output_data.update({"verification": "按在线接口返回为准"})
        reasoning = "凭证只在后端验证和加密保存，执行链路不会展示密钥明文。"
    elif base_tool == "credential.redact":
        summary = "对聊天中的接入凭证做脱敏处理，避免明文进入历史记录。"
        input_data.update({"source": "用户输入中的接入配置"})
        output_data.update({"redacted": True})
        reasoning = "先脱敏再入库，避免 AppKey/AppSecret 出现在聊天历史、审计日志或前端链路详情。"
    else:
        summary = f"执行工具 {tool_name}。"
        output_data.update({"status": "completed" if ":failed" not in tool_name else "failed"})
    return summary, detail, input_data, output_data, reasoning


def build_agent_trace(user_text: str, agent: dict | None, artifact: dict | None = None, source: str | None = None) -> dict | None:
    if not isinstance(agent, dict):
        return None
    artifact = artifact or {}
    nodes = []
    analysis = agent.get("analysis") if isinstance(agent.get("analysis"), dict) else {}
    intent = agent.get("intent") or analysis.get("intent") or "UNKNOWN"
    skill = agent.get("skill") if isinstance(agent.get("skill"), dict) else {"name": agent.get("skill")}
    route = agent.get("route") if isinstance(agent.get("route"), dict) else {}
    route_skill = route.get("skill") if isinstance(route.get("skill"), dict) else {}
    route_tool = route.get("tool") if isinstance(route.get("tool"), dict) else {}
    skill_name = (
        skill.get("name")
        or skill.get("skill")
        or skill.get("id")
        or route_skill.get("name")
        or agent.get("skill")
        or "未命名 Skill"
    )
    confidence = analysis.get("confidence", agent.get("confidence"))
    nodes.append(
        trace_node(
            "intent",
            "意图识别",
            "INTENT",
            "SUCCEEDED" if agent.get("status") != "BLOCKED" else "BLOCKED",
            f"识别为 {intent}，置信度 {trace_confidence(confidence)}。",
            {
                "user_input": user_text,
                "engine": agent.get("engine"),
                "analysis": analysis,
                "warning": agent.get("warning"),
            },
            input_data={
                "user_input": user_text,
                "source": source or "conversation",
                "context": agent.get("context"),
            },
            output_data={
                "intent": intent,
                "confidence": trace_confidence(confidence),
                "engine": agent.get("engine"),
                "status": agent.get("status"),
                "analysis": analysis,
                "warning": agent.get("warning"),
            },
            reasoning="根据用户原始输入、当前租户/门店上下文和历史对话状态识别本轮任务类型，后续节点按该意图选择 Skill 和工具。",
        )
    )
    nodes.append(
        trace_node(
            "skill",
            "Skill 路由",
            "SKILL",
            "SUCCEEDED",
            f"路由到 {skill_name}。",
            {"skill": skill, "standard_route": route},
            input_data={"intent": intent, "analysis": analysis, "similar_intents": route.get("similar_intents")},
            output_data={
                "skill": skill,
                "standard_skill": route_skill,
                "standard_tool": route_tool,
                "risk": route.get("risk"),
                "required_slots": route.get("required_slots"),
                "tool_calls": agent.get("tool_calls") or [],
            },
            reasoning=route.get("reason") or "将意图映射到可执行 Skill；若没有已有能力，会进入 Pipeline 编排或补槽确认。",
        )
    )
    # OPEN_QA is deliberately isolated from tenant memory and knowledge. Do
    # not even render these as no-op steps: the trace is part of the contract.
    if agent.get("mode") != "OPEN_QA":
        memory_hits = (
            agent.get("memory_hits")
            if isinstance(agent.get("memory_hits"), list)
            else artifact.get("memoryHits") if isinstance(artifact.get("memoryHits"), list)
            else artifact.get("memory_hits") if isinstance(artifact.get("memory_hits"), list)
            else []
        )
        knowledge_hits = (
            agent.get("knowledge_hits")
            if isinstance(agent.get("knowledge_hits"), list)
            else artifact.get("knowledgeHits") if isinstance(artifact.get("knowledgeHits"), list)
            else artifact.get("knowledge_hits") if isinstance(artifact.get("knowledge_hits"), list)
            else []
        )
        nodes.append(
            trace_node(
                "memory_retrieve",
                "长期记忆召回",
                "MEMORY",
                "SUCCEEDED",
                f"检索长期记忆，命中 {len(memory_hits)} 条。",
                {"hits": memory_hits[:10]},
                input_data={"intent": intent, "tenant_id": agent.get("tenant_id"), "user_input": user_text},
                output_data={
                    "hit_count": len(memory_hits),
                    "hit_keys": [
                        item.get("key") or item.get("memory_key") or item.get("title")
                        for item in memory_hits[:10]
                        if isinstance(item, dict)
                    ],
                },
                reasoning="长期记忆用于注入用户偏好、别名和业务判断口径；命中内容只作为上下文，最终结论仍需由工具和模型结果支撑。",
            )
        )
        nodes.append(
            trace_node(
                "knowledge_recall",
                "知识库召回",
                "KNOWLEDGE",
                "SUCCEEDED",
                f"检索多模态知识库，命中 {len(knowledge_hits)} 条。",
                {"hits": knowledge_hits[:10]},
                input_data={"intent": intent, "tenant_id": agent.get("tenant_id"), "user_input": user_text},
                output_data={
                    "hit_count": len(knowledge_hits),
                    "hit_titles": [
                        item.get("title") or item.get("name")
                        for item in knowledge_hits[:10]
                        if isinstance(item, dict)
                    ],
                },
                reasoning="知识库用于召回 SOP、品牌规范、参考物料和门店平面图等资料；召回结果会进入后续工具或模型调用上下文。",
            )
        )
    for index, tool_name in enumerate(agent.get("tool_calls") or [], start=1):
        status = "BLOCKED" if any(flag in tool_name for flag in (":failed", ":unavailable", ":blocked")) else "SUCCEEDED"
        summary, detail, input_data, output_data, reasoning = trace_tool_summary(str(tool_name), artifact)
        nodes.append(
            trace_node(
                f"tool_{index}",
                "工具调用",
                "TOOL",
                status,
                summary,
                detail,
                input_data=input_data,
                output_data=output_data,
                reasoning=reasoning,
            )
        )

    visual_result = artifact.get("visualResult") if isinstance(artifact.get("visualResult"), dict) else None
    scheduled_run = artifact.get("scheduledRun") if isinstance(artifact.get("scheduledRun"), dict) else None
    if visual_result:
        raw_model = visual_result.get("model_raw_output") if isinstance(visual_result.get("model_raw_output"), dict) else {}
        candidate_outputs = (
            visual_result.get("candidate_model_outputs")
            if isinstance(visual_result.get("candidate_model_outputs"), list)
            else []
        )
        nodes.append(
            trace_node(
                "model_output",
                "大模型原始输出",
                "MODEL",
                "SUCCEEDED" if raw_model else "UNKNOWN",
                raw_model.get("conclusion") or "已记录模型结构化返回。",
                {
                    "model": visual_result.get("model"),
                    "source": visual_result.get("source"),
                    "raw_output": raw_model,
                    "candidate_outputs": candidate_outputs,
                },
                input_data={
                    "question": visual_result.get("question"),
                    "model": visual_result.get("model"),
                    "image_count": visual_result.get("image_count"),
                    "failed_image_count": visual_result.get("failed_image_count"),
                    "failed_camera_names": visual_result.get("failed_camera_names"),
                    "batch_count": visual_result.get("batch_count"),
                    "candidate_batch_size": visual_result.get("candidate_batch_size"),
                    "selected_camera_names": visual_result.get("selected_camera_names"),
                    "candidate_count": len(candidate_outputs),
                },
                output_data={
                    "raw_output": raw_model,
                    "candidate_outputs": candidate_outputs,
                },
                reasoning="该节点展示视觉大模型返回的结构化 JSON 摘要，用于核对异常描述是否来自模型输出；不展示模型内部不可见思维链。",
            )
        )
        nodes.append(
            trace_node(
                "business_review",
                "业务规则复核",
                "RULE",
                visual_result.get("status") or "UNKNOWN",
                visual_result.get("conclusion") or "已完成规则复核。",
                {
                    "business_policy": visual_result.get("business_policy"),
                    "business_reason": visual_result.get("business_reason"),
                    "status": visual_result.get("status"),
                    "confidence": visual_result.get("confidence"),
                    "target_observed": visual_result.get("target_observed"),
                    "subject_present": visual_result.get("subject_present"),
                    "evidence_type": visual_result.get("evidence_type"),
                    "selected_camera_names": visual_result.get("selected_camera_names"),
                    "anomaly_camera_names": visual_result.get("anomaly_camera_names"),
                    "sku_comparison": visual_result.get("sku_comparison"),
                    "failed_camera_names": visual_result.get("failed_camera_names"),
                    "observations": visual_result.get("observations"),
                    "exclusions": visual_result.get("exclusions"),
                },
                input_data={
                    "model_status": raw_model.get("status") or visual_result.get("status"),
                    "model_conclusion": raw_model.get("conclusion") or visual_result.get("conclusion"),
                    "business_policy": visual_result.get("business_policy"),
                    "target_observed": visual_result.get("target_observed"),
                    "subject_present": visual_result.get("subject_present"),
                    "evidence_type": visual_result.get("evidence_type"),
                },
                output_data={
                    "final_status": visual_result.get("status"),
                    "final_conclusion": visual_result.get("conclusion"),
                    "confidence": trace_confidence(visual_result.get("confidence")),
                    "anomaly_camera_names": visual_result.get("anomaly_camera_names"),
                    "sku_comparison": visual_result.get("sku_comparison"),
                    "failed_camera_names": visual_result.get("failed_camera_names"),
                    "observations": visual_result.get("observations"),
                    "exclusions": visual_result.get("exclusions"),
                },
                reasoning="按业务规则复核模型输出：禁止类目标出现即异常，必需服务行为需结合服务对象是否在场和证据类型判断，证据不足时保留待复核。",
            )
        )
    elif scheduled_run:
        nodes.append(
            trace_node(
                "scheduled_result",
                "巡检结果落库",
                "PERSIST",
                scheduled_run.get("result_status") or scheduled_run.get("status") or "UNKNOWN",
                scheduled_run.get("conclusion") or scheduled_run.get("error_message") or "周期巡检结果已记录。",
                {
                    "run_id": scheduled_run.get("run_id"),
                    "model_version": scheduled_run.get("model_version"),
                    "business_reason": scheduled_run.get("business_reason"),
                    "observations": scheduled_run.get("observations"),
                    "anomaly_evidence_ids": scheduled_run.get("anomaly_evidence_ids"),
                },
                input_data={
                    "task_id": scheduled_run.get("task_id"),
                    "run_id": scheduled_run.get("run_id"),
                    "inspection_goal": scheduled_run.get("inspection_goal"),
                    "image_count": scheduled_run.get("image_count"),
                },
                output_data={
                    "status": scheduled_run.get("status"),
                    "result_status": scheduled_run.get("result_status"),
                    "conclusion": scheduled_run.get("conclusion"),
                    "confidence": trace_confidence(scheduled_run.get("confidence")),
                    "anomaly_evidence_ids": scheduled_run.get("anomaly_evidence_ids"),
                },
                reasoning="周期任务完成后将本批次巡检结论、证据图片和异常图片标记保存到历史记录。",
            )
        )
    if artifact.get("pipeline"):
        nodes.append(
            trace_node(
                "pipeline",
                "Pipeline 编排",
                "PIPELINE",
                artifact["pipeline"].get("status") or "DRAFT",
                artifact["pipeline"].get("title") or artifact["pipeline"].get("summary") or "已生成能力编排草案。",
                artifact["pipeline"],
                input_data={"user_input": user_text, "intent": intent},
                output_data=artifact["pipeline"],
                reasoning="当目标能力不能直接命中已有订阅能力时，按视频/图片巡检 SOP 输出可实现的模型与工具编排草案。",
            )
        )
    return {
        "title": "Agent 执行链路",
        "source": source or "conversation",
        "generated_at": now_iso(),
        "nodes": nodes,
    }


def attach_agent_trace(linked_object: dict, user_text: str, artifact_override: dict | None = None) -> dict:
    if not isinstance(linked_object, dict):
        return linked_object
    agent = linked_object.get("agent") if isinstance(linked_object.get("agent"), dict) else None
    if not agent:
        return linked_object
    if isinstance(agent.get("trace"), dict) and agent["trace"].get("nodes"):
        return linked_object
    artifact = artifact_override or linked_object.get("artifact") or {}
    if isinstance(linked_object.get("plan"), dict):
        artifact = {**artifact, "plan": linked_object["plan"]} if isinstance(artifact, dict) else {"plan": linked_object["plan"]}
    trace = build_agent_trace(user_text, agent, artifact, linked_object.get("source"))
    if trace:
        agent["trace"] = trace
        linked_object["agent"] = agent
    return linked_object


def apply_web_search_usage_to_response(conn, user: dict, conversation_id: str, response: dict) -> dict | None:
    """Persist public-search usage and add an in-chat warning below the quota floor."""
    agent = response.get("agent") if isinstance(response.get("agent"), dict) else {}
    tool_calls = agent.get("tool_calls") if isinstance(agent.get("tool_calls"), list) else []
    search = response.get("web_search") if isinstance(response.get("web_search"), dict) else None
    account_usage = response.get("web_search_usage") if isinstance(response.get("web_search_usage"), dict) else None
    has_search_attempt = bool(search) or any(str(item).startswith("web.search") for item in tool_calls)
    if not has_search_attempt:
        return None
    try:
        config, _cache_token = web_search_runtime_config(conn, user["tenant_id"])
    except ApiError:
        return None
    if str(config.get("provider") or "").lower() not in {"tavily", "brave"}:
        return None
    is_failed = any(str(item) == "web.search:failed" for item in tool_calls)
    if not search and not account_usage and not is_failed:
        return None
    summary = record_web_search_usage(
        conn,
        tenant_id=user["tenant_id"],
        conversation_id=conversation_id,
        config=config,
        operation="SEARCH",
        status="FAILED" if is_failed else "SUCCEEDED",
        search=search,
        account_usage=account_usage,
    )
    response["web_search_budget"] = summary
    if summary.get("low_balance") and isinstance(summary.get("remaining_credits"), int):
        remaining = summary["remaining_credits"]
        limit = summary["credit_limit"]
        warning = f"额度提醒：公共网页检索可用额度剩余 {remaining}/{limit} Credits，已低于 10% 水位。"
        content = str(response.get("assistant_content") or "").rstrip()
        if warning not in content:
            response["assistant_content"] = f"{content}\n\n{warning}".strip()
    return summary


def open_qa_history(conn, conversation_id: str, limit: int = 20) -> list[dict]:
    history = rows(
        conn,
        """
        SELECT sender, content, linked_object
        FROM (
          SELECT sender, content, linked_object, created_at, rowid AS message_order
          FROM messages
          WHERE conversation_id=?
          ORDER BY created_at DESC, message_order DESC
          LIMIT ?
        )
        ORDER BY created_at ASC, message_order ASC
        """,
        (conversation_id, max(1, min(int(limit), 50))),
    )
    return [{**item, "linked_object": json_loads(item.get("linked_object"), None)} for item in history]


CONVERSATION_CONTEXT_TTL_MINUTES = max(
    5,
    min(int(os.environ.get("AGI_CONVERSATION_CONTEXT_TTL_MINUTES", "30")), 24 * 60),
)
CONVERSATION_SEMANTIC_CONTEXT_DAYS = max(
    1,
    min(int(os.environ.get("AGI_CONVERSATION_SEMANTIC_CONTEXT_DAYS", "30")), 365),
)


def _deserialize_conversation_context(row: dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for column, target, fallback in (
        ("page_scope_json", "page_scope", {}),
        ("task_scope_json", "task_scope", {}),
        ("scope_history_json", "scope_history", []),
        ("predicate_json", "predicate", {}),
        ("temporal_json", "temporal", {}),
        ("evidence_refs_json", "evidence_refs", []),
        ("result_refs_json", "result_refs", []),
        ("decision_json", "decision", {}),
    ):
        item[target] = json_loads(item.get(column), fallback)
    return item


def load_active_conversation_context(
    conn: sqlite3.Connection,
    user: dict,
    conversation_id: str,
) -> dict | None:
    row = one(
        conn,
        """SELECT * FROM conversation_contexts
           WHERE conversation_id=? AND tenant_id=? AND user_id=? AND state='ACTIVE'
           ORDER BY version DESC, created_at DESC LIMIT 1""",
        (conversation_id, user["tenant_id"], user["user_id"]),
    )
    context = _deserialize_conversation_context(row)
    if not context:
        return None
    try:
        expired = datetime.fromisoformat(str(context.get("expires_at") or "")) <= datetime.now(CN_TZ)
    except ValueError:
        expired = True
    if expired:
        conn.execute(
            "UPDATE conversation_contexts SET state='EXPIRED', updated_at=? WHERE context_id=? AND state='ACTIVE'",
            (now_iso(), context["context_id"]),
        )
        return None
    return context


def _conversation_context_is_recent(value: str | None) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return False
    return timestamp >= datetime.now(CN_TZ) - timedelta(days=CONVERSATION_SEMANTIC_CONTEXT_DAYS)


def _legacy_visual_evidence_ref(
    conn: sqlite3.Connection,
    tenant_id: str,
    image: dict,
) -> dict | None:
    evidence_id = str(image.get("evidence_id") or "")
    evidence = None
    if evidence_id:
        evidence = one(
            conn,
            "SELECT * FROM online_snapshot_evidence WHERE evidence_id=? AND tenant_id=?",
            (evidence_id, tenant_id),
        )
    if not evidence and image.get("camera_id") and image.get("captured_at"):
        evidence = one(
            conn,
            """SELECT * FROM online_snapshot_evidence
               WHERE tenant_id=? AND org_id=? AND camera_id=? AND captured_at=?
               ORDER BY created_at DESC LIMIT 1""",
            (
                tenant_id,
                str(image.get("org_id") or ""),
                str(image.get("camera_id") or ""),
                str(image.get("captured_at") or ""),
            ),
        )
    if not evidence:
        return None
    return {
        "evidence_id": evidence["evidence_id"],
        "org_id": evidence["org_id"],
        "org_name": str(image.get("org_name") or evidence["org_id"]),
        "camera_id": evidence["camera_id"],
        "camera_name": evidence["camera_name"],
        "captured_at": evidence["captured_at"],
    }


def load_recoverable_conversation_context(
    conn: sqlite3.Connection,
    user: dict,
    conversation_id: str,
) -> dict | None:
    """Recover task semantics for pre-rollout or expired visual conversations.

    Recovery is deliberately lazy and read-only.  The caller must still prove
    that the new utterance is a visual continuation before using this value.
    Evidence freshness is handled separately and stale contexts force a fresh
    capture instead of reviving old frames.
    """

    conversation = one(
        conn,
        "SELECT tenant_id, user_id, org_id FROM conversations WHERE conversation_id=?",
        (conversation_id,),
    )
    if not conversation or conversation["tenant_id"] != user["tenant_id"] or conversation["user_id"] != user["user_id"]:
        return None

    persisted_row = one(
        conn,
        """SELECT * FROM conversation_contexts
           WHERE conversation_id=? AND tenant_id=? AND user_id=?
             AND domain='VISUAL_INSPECTION'
           ORDER BY version DESC, updated_at DESC LIMIT 1""",
        (conversation_id, user["tenant_id"], user["user_id"]),
    )
    persisted = _deserialize_conversation_context(persisted_row)
    if persisted and _conversation_context_is_recent(persisted.get("updated_at")):
        return {
            **persisted,
            "state": "ACTIVE",
            "_recovered_from": "EXPIRED_CONTEXT_REVISION",
            "_recovery_requires_recapture": True,
        }

    message_rows = rows(
        conn,
        """SELECT rowid AS message_order, * FROM messages
           WHERE conversation_id=? AND sender='assistant'
           ORDER BY created_at DESC, rowid DESC LIMIT 100""",
        (conversation_id,),
    )
    for message in message_rows:
        if not _conversation_context_is_recent(message.get("created_at")):
            continue
        linked = json_loads(message.get("linked_object"), {}) or {}
        agent = linked.get("agent") if isinstance(linked.get("agent"), dict) else {}
        artifact = linked.get("artifact") if isinstance(linked.get("artifact"), dict) else {}
        visual_result = artifact.get("visualResult") if isinstance(artifact.get("visualResult"), dict) else {}
        if str(agent.get("intent") or "") != "ANALYZE_VISUAL" and not visual_result:
            continue
        visual_context = linked.get("visual_context") if isinstance(linked.get("visual_context"), dict) else {}
        images = visual_context.get("images") if isinstance(visual_context.get("images"), list) else []
        if not images and isinstance(artifact.get("mediaGallery"), list):
            images = artifact["mediaGallery"]
        context_summary = artifact.get("conversationScope") if isinstance(artifact.get("conversationScope"), dict) else {}
        task_scope = context_summary.get("task_scope") if isinstance(context_summary.get("task_scope"), dict) else {}
        org_names: dict[str, str] = {}
        evidence_refs = []
        for image in images:
            if not isinstance(image, dict):
                continue
            org_id = str(image.get("org_id") or "")
            if org_id:
                org_names[org_id] = str(image.get("org_name") or org_id)
            evidence_ref = _legacy_visual_evidence_ref(conn, user["tenant_id"], image)
            if evidence_ref:
                evidence_refs.append(evidence_ref)
        if not task_scope:
            if not org_names:
                continue
            task_scope = {
                "type": "MULTI_STORE" if len(org_names) > 1 else "SINGLE_STORE",
                "source": "LEGACY_VISUAL_MESSAGE",
                "org_ids": list(org_names),
                "org_names": list(org_names.values()),
            }
        effective_query = str(visual_result.get("question") or "").strip()
        if not effective_query:
            prior_user = one(
                conn,
                """SELECT content FROM messages
                   WHERE conversation_id=? AND sender='user' AND rowid<?
                   ORDER BY rowid DESC LIMIT 1""",
                (conversation_id, int(message["message_order"])),
            )
            effective_query = str((prior_user or {}).get("content") or "").strip()
        if not effective_query:
            continue
        try:
            age = datetime.now(CN_TZ) - datetime.fromisoformat(str(message.get("created_at") or ""))
        except ValueError:
            age = timedelta(days=CONVERSATION_SEMANTIC_CONTEXT_DAYS)
        return {
            "context_id": f"legacy_{message['message_id']}",
            "conversation_id": conversation_id,
            "tenant_id": user["tenant_id"],
            "user_id": user["user_id"],
            "domain": "VISUAL_INSPECTION",
            "task_kind": "ANALYZE_VISUAL",
            "state": "ACTIVE",
            "version": 0,
            "effective_query": effective_query[:1800],
            "page_scope": {
                "org_id": conversation.get("org_id") or "",
                "org_name": "",
            },
            "task_scope": task_scope,
            "scope_history": [],
            "predicate": {"strategy": "LEGACY_VISUAL_MESSAGE", "effective_query": effective_query[:1800]},
            "temporal": {"mode": "CURRENT"},
            "evidence_refs": evidence_refs,
            "result_refs": [{"kind": "MESSAGE", "id": message["message_id"]}],
            "decision": {"reason_code": "LEGACY_VISUAL_MESSAGE"},
            "created_at": message.get("created_at"),
            "updated_at": message.get("created_at"),
            "_recovered_from": "LEGACY_VISUAL_MESSAGE",
            "_recovery_requires_recapture": age > timedelta(minutes=CONVERSATION_CONTEXT_TTL_MINUTES),
        }
    return None


def _context_page_scope(conn: sqlite3.Connection, tenant_id: str, org_id: str | None) -> dict:
    normalized = str(org_id or "")
    if not normalized:
        return {"org_id": "", "org_name": ""}
    org = one(conn, "SELECT name FROM orgs WHERE tenant_id=? AND org_id=?", (tenant_id, normalized))
    if not org:
        org = one(
            conn,
            """SELECT s.name FROM tenant_integration_stores s
               JOIN tenant_integrations i ON i.integration_id=s.integration_id
               WHERE i.tenant_code=? AND s.org_id=?""",
            (tenant_id, normalized),
        )
    return {"org_id": normalized, "org_name": str((org or {}).get("name") or normalized)}


def _context_evidence_images(
    conn: sqlite3.Connection,
    user: dict,
    refs: list[dict],
) -> tuple[list[dict], list[str]]:
    allowed = allowed_org_ids(conn, user)
    root = ONLINE_SNAPSHOT_EVIDENCE_DIR.resolve()
    images = []
    errors = []
    for ref in refs[:20]:
        if not isinstance(ref, dict) or not ref.get("evidence_id"):
            continue
        evidence = one(
            conn,
            "SELECT * FROM online_snapshot_evidence WHERE evidence_id=? AND tenant_id=?",
            (str(ref["evidence_id"]), user["tenant_id"]),
        )
        if not evidence:
            errors.append(f"{ref.get('evidence_id')}：证据不存在或租户不匹配")
            continue
        if evidence["org_id"] not in allowed:
            errors.append(f"{ref.get('evidence_id')}：当前用户已无权读取该证据")
            continue
        path = Path(str(evidence["storage_path"])).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(f"{evidence['evidence_id']}：证据路径不在受控目录")
            continue
        try:
            content = path.read_bytes()
        except OSError:
            errors.append(f"{evidence['evidence_id']}：证据文件不可读")
            continue
        if not content or len(content) > 12 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != evidence["sha256"]:
            errors.append(f"{evidence['evidence_id']}：证据完整性校验失败")
            continue
        images.append(
            {
                "kind": "IMAGE",
                "source_kind": "ARCHIVED_CONTEXT",
                "evidence_id": evidence["evidence_id"],
                "camera_id": evidence["camera_id"],
                "camera_name": evidence["camera_name"],
                "org_id": evidence["org_id"],
                "org_name": str(ref.get("org_name") or evidence["org_id"]),
                "snapshot_url": f"data:{evidence['mime_type']};base64,{base64.b64encode(content).decode('ascii')}",
                "captured_at": evidence["captured_at"],
                "sha256": evidence["sha256"],
            }
        )
    return images, errors


def prepare_conversation_turn_context(
    conn: sqlite3.Connection,
    user: dict,
    conversation_id: str,
    text: str,
    context: dict,
    mode_override: str,
) -> tuple[dict, dict | None, dict]:
    active = load_active_conversation_context(conn, user, conversation_id)
    page_scope = _context_page_scope(conn, user["tenant_id"], context.get("org_id"))
    recovered = None
    if not active:
        candidate = load_recoverable_conversation_context(conn, user, conversation_id)
        candidate_decision = decide_continuation(text, candidate, page_scope.get("org_id"), mode_override)
        if candidate and candidate_decision.get("decision") in {"CONTINUE", "CLARIFY"}:
            active = candidate
            recovered = str(candidate.get("_recovered_from") or "RECOVERED_CONTEXT")
            decision = candidate_decision
        else:
            decision = decide_continuation(text, None, page_scope.get("org_id"), mode_override)
    else:
        decision = decide_continuation(text, active, page_scope.get("org_id"), mode_override)
    if recovered and decision.get("decision") == "CONTINUE":
        decision = {
            **decision,
            "evidence_mode": (
                "RECAPTURE_RESOLVED_SCOPE"
                if active.get("_recovery_requires_recapture")
                else decision.get("evidence_mode")
            ),
            "reason_code": "RECOVERED_VISUAL_CONTEXT",
            "continuation_reason_code": candidate_decision.get("reason_code"),
            "recovered_from": recovered,
        }
    allowed = sorted(allowed_org_ids(conn, user))
    prepared = {
        **context,
        "authorized_org_ids": allowed,
        "page_scope": page_scope,
        "_conversation_continuation": decision,
    }
    if recovered:
        prepared["_conversation_context_recovered"] = recovered
    if active and not recovered:
        prepared["_expected_context"] = {
            "context_id": active["context_id"],
            "version": int(active["version"]),
        }
    if decision.get("decision") == "CONTINUE":
        active_task_org_ids = {
            str(item)
            for item in (decision.get("active_task_scope") or {}).get("org_ids") or []
            if item
        }
        scope_history = decision.get("scope_history") if isinstance(decision.get("scope_history"), list) else []
        previous_scope = next(
            (item for item in reversed(scope_history) if isinstance(item, dict) and item.get("org_ids")),
            {},
        )
        previous_task_org_ids = {str(item) for item in previous_scope.get("org_ids") or [] if item}
        operation = decision.get("scope_operation")
        if operation == "PREVIOUS_SCOPE":
            permission_target_ids = previous_task_org_ids
        elif operation == "COMPARE_SCOPE":
            permission_target_ids = active_task_org_ids | previous_task_org_ids
        else:
            permission_target_ids = active_task_org_ids
        denied = sorted(permission_target_ids - set(allowed))
        if denied and decision.get("scope_operation") in {"KEEP_SCOPE", "PREVIOUS_SCOPE", "COMPARE_SCOPE"}:
            prepared["_context_scope_denied"] = denied
        if decision.get("evidence_mode") == "REUSE_SAME_FRAME" and not denied:
            images, errors = _context_evidence_images(
                conn,
                user,
                decision.get("active_evidence_refs") or [],
            )
            prepared["continuation_images"] = images
            prepared["continuation_evidence_errors"] = errors
    return prepared, active, decision


def persist_online_conversation_context(
    conn: sqlite3.Connection,
    user: dict,
    conversation_id: str,
    prepared_context: dict,
    response: dict,
    visual_context: dict | None,
) -> dict | None:
    draft = response.pop("_conversation_context", None)
    if not isinstance(draft, dict):
        return None
    expected = prepared_context.get("_expected_context") if isinstance(prepared_context.get("_expected_context"), dict) else None
    prior = load_active_conversation_context(conn, user, conversation_id)
    if expected:
        cursor = conn.execute(
            """UPDATE conversation_contexts
               SET state='SUPERSEDED', superseded_at=?, updated_at=?
               WHERE context_id=? AND version=? AND state='ACTIVE'""",
            (now_iso(), now_iso(), expected.get("context_id"), int(expected.get("version") or 0)),
        )
        if cursor.rowcount != 1:
            return {"status": "STALE_CONTEXT", "expected": expected}
        version = int(expected.get("version") or 0) + 1
    else:
        active_now = one(
            conn,
            """SELECT context_id, version FROM conversation_contexts
               WHERE conversation_id=? AND tenant_id=? AND user_id=? AND state='ACTIVE'
               ORDER BY version DESC LIMIT 1""",
            (conversation_id, user["tenant_id"], user["user_id"]),
        )
        if active_now:
            return {"status": "STALE_CONTEXT", "expected": None, "active_context_id": active_now["context_id"]}
        latest_version = one(
            conn,
            """SELECT MAX(version) AS version FROM conversation_contexts
               WHERE conversation_id=? AND tenant_id=? AND user_id=?""",
            (conversation_id, user["tenant_id"], user["user_id"]),
        )
        version = int((latest_version or {}).get("version") or 0) + 1

    page_scope = prepared_context.get("page_scope") if isinstance(prepared_context.get("page_scope"), dict) else {}
    task_scope = draft.get("task_scope") if isinstance(draft.get("task_scope"), dict) else {}
    decision = draft.get("decision") if isinstance(draft.get("decision"), dict) else {}
    prior_history = list((prior or {}).get("scope_history") or [])
    prior_scope = (prior or {}).get("task_scope") if isinstance((prior or {}).get("task_scope"), dict) else None
    if prior_scope and prior_scope != task_scope:
        prior_history.append(prior_scope)
    scope_history = prior_history[-10:]
    evidence_refs = []
    for image in (visual_context or {}).get("images") or []:
        if not isinstance(image, dict) or not image.get("evidence_id"):
            continue
        evidence_refs.append(
            {
                "evidence_id": image.get("evidence_id"),
                "org_id": image.get("org_id"),
                "org_name": image.get("org_name"),
                "camera_id": image.get("camera_id"),
                "camera_name": image.get("camera_name"),
                "captured_at": image.get("captured_at"),
            }
        )
    timestamp = now_iso()
    expires_at = (datetime.now(CN_TZ) + timedelta(minutes=CONVERSATION_CONTEXT_TTL_MINUTES)).isoformat(timespec="seconds")
    context_id = f"ctx_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO conversation_contexts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            context_id,
            conversation_id,
            user["tenant_id"],
            user["user_id"],
            str(draft.get("domain") or "VISUAL_INSPECTION"),
            str(draft.get("task_kind") or response.get("intent") or "ANALYZE_VISUAL"),
            "ACTIVE",
            version,
            str(draft.get("effective_query") or "")[:1800],
            json_dumps(page_scope),
            json_dumps(task_scope),
            json_dumps(scope_history),
            json_dumps(draft.get("predicate") if isinstance(draft.get("predicate"), dict) else {}),
            json_dumps(draft.get("temporal") if isinstance(draft.get("temporal"), dict) else {}),
            json_dumps(evidence_refs),
            json_dumps(draft.get("result_refs") if isinstance(draft.get("result_refs"), list) else []),
            json_dumps(decision),
            timestamp,
            timestamp,
            expires_at,
            None,
        ),
    )
    stored = load_active_conversation_context(conn, user, conversation_id)
    if not stored or stored.get("context_id") != context_id:
        return {"status": "STALE_CONTEXT", "context_id": context_id}
    return {"status": "ACTIVE", **public_context_summary(stored)}


def persist_plan_conversation_context(
    conn: sqlite3.Connection,
    user: dict,
    conversation_id: str,
    prepared_context: dict,
    plan: dict,
    effective_query: str,
) -> dict | None:
    slots = plan.get("slots") if isinstance(plan.get("slots"), dict) else {}
    org_scope = slots.get("org_scope") if isinstance(slots.get("org_scope"), dict) else {}
    org_ids = list(org_scope.get("resolved_ids") or [])
    org_names = list(org_scope.get("resolved_names") or [])
    if not org_ids and org_scope.get("org_id"):
        org_ids = [org_scope["org_id"]]
        org_names = [org_scope.get("org_name") or org_scope["org_id"]]
    if not org_ids:
        return None
    continuation = prepared_context.get("_conversation_continuation") if isinstance(prepared_context.get("_conversation_continuation"), dict) else {}
    draft = {
        "domain": "VISUAL_INSPECTION",
        "task_kind": str(plan.get("intent") or "VISUAL_PLAN"),
        "effective_query": str(effective_query or slots.get("inspection_goal") or "")[:1800],
        "task_scope": {
            "type": "MULTI_STORE" if len(org_ids) > 1 else "SINGLE_STORE",
            "source": (
                "INHERITED_TASK"
                if continuation.get("decision") == "CONTINUE" and continuation.get("scope_operation") == "KEEP_SCOPE"
                else "DISCOURSE_REFERENCE"
                if continuation.get("decision") == "CONTINUE"
                else "EXPLICIT_QUERY"
            ),
            "org_ids": org_ids,
            "org_names": org_names,
        },
        "predicate": {
            "strategy": "LLM_DYNAMIC_PATCH" if continuation.get("decision") == "CONTINUE" else "CURRENT_QUERY",
            "effective_query": str(effective_query or "")[:1800],
        },
        "temporal": {"mode": "SCHEDULED" if "SCHEDULED" in str(plan.get("intent") or "") else "CURRENT"},
        "decision": {
            **continuation,
            "resolved_org_ids": org_ids,
            "evidence_mode": "RECAPTURE_RESOLVED_SCOPE",
        },
        "result_refs": [{"kind": "PLAN", "id": plan.get("plan_id"), "status": plan.get("status")}],
    }
    response = {"intent": plan.get("intent"), "_conversation_context": draft}
    return persist_online_conversation_context(
        conn,
        user,
        conversation_id,
        prepared_context,
        response,
        None,
    )


_PDF_TOKEN_PATTERN = re.compile(r"(?i)(?<![A-Za-z])p\s*d\s*f(?![A-Za-z])")


def is_pdf_instruction_response(text: str) -> bool:
    """Reject model replies that explain manual PDF creation instead of delivering it."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    markers = (
        "无法直接生成", "无法生成PDF", "无法生成 PDF", "不能直接生成",
        "复制粘贴到Word", "复制粘贴到 Word", "使用浏览器打印功能",
        "在线工具", "一键转PDF", "一键转 PDF", "导出为PDF", "导出为 PDF",
    )
    return any(marker in normalized for marker in markers)


def is_open_qa_pdf_followup(text: str) -> bool:
    """Recognize a short request to export the preceding open-QA answer."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not _PDF_TOKEN_PATTERN.search(normalized):
        return False
    compact = _PDF_TOKEN_PATTERN.sub("PDF", normalized)
    if len(compact) > 48:
        return False
    return any(
        marker in compact
        for marker in (
            "整理", "生成", "导出", "制作", "做成", "转成", "转为", "保存",
            "上面", "上述", "前面", "刚才", "上一", "这份", "一个", "一份",
        )
    )


def latest_open_qa_export_source(conn, conversation_id: str) -> dict | None:
    """Return the latest exportable open-QA answer without crossing mode boundaries."""
    candidates = rows(
        conn,
        """
        SELECT message_id, content, linked_object, created_at
        FROM messages
        WHERE conversation_id=? AND sender='assistant'
        ORDER BY created_at DESC
        LIMIT 12
        """,
        (conversation_id,),
    )
    fallback = None
    for candidate in candidates:
        linked = json_loads(candidate.get("linked_object"), {}) or {}
        agent = linked.get("agent") if isinstance(linked.get("agent"), dict) else {}
        if linked.get("source") != "open_qa" and str(agent.get("mode") or "").upper() != "OPEN_QA":
            continue
        artifact = linked.get("artifact") if isinstance(linked.get("artifact"), dict) else {}
        source_question = one(
            conn,
            """
            SELECT content
            FROM messages
            WHERE conversation_id=? AND sender='user' AND created_at<=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id, candidate.get("created_at")),
        )
        export_source = {
            "message_id": candidate["message_id"],
            "content": str(candidate.get("content") or "").strip(),
            "artifact": artifact,
            "created_at": candidate.get("created_at"),
            "question": str((source_question or {}).get("content") or "").strip(),
        }
        if is_pdf_instruction_response(export_source["content"]):
            continue
        if artifact.get("generatedDocument") or agent.get("engine") == "context_document_export":
            fallback = fallback or export_source
            continue
        return export_source
    return fallback


def open_qa_pdf_followup_response(source: dict, mode_selection: str = "AUTO") -> dict:
    """Build a deterministic document request from a previously persisted answer."""
    artifact = source.get("artifact") if isinstance(source.get("artifact"), dict) else {}
    agent = {
        "engine": "context_document_export",
        "confidence": 1.0,
        "intent": "OPEN_QA",
        "mode": "OPEN_QA",
        "status": "SUCCEEDED",
        "catalog_version": "agent-core-v1",
        "data_source": "conversation_context",
        "read_only": True,
        "tool_calls": ["conversation.context.read"],
        "skill": skill_descriptor("OPEN_QA"),
        "route": standard_agent_catalog().route("OPEN_QA").to_dict(),
        "decision": {
            "route": "OPEN_QA",
            "route_confidence": 1.0,
            "evidence_state": "CONTEXT_REUSED",
            "tool_state": "NOT_REQUESTED",
            "risk_level": "READ_ONLY",
            "response_strategy": "GENERATE_DOCUMENT",
            "allowed_tools": ["conversation.context.read"],
            "mode_selection": mode_selection,
            "next_actions": ["DOWNLOAD_DOCUMENT"],
        },
        "analysis": {
            "intent": "OPEN_QA",
            "confidence": 1.0,
            "state": "CONTEXT_DOCUMENT_EXPORT",
            "requested_output_format": "PDF",
            "source_message_id": source.get("message_id"),
        },
        "stages": ["UNDERSTAND", "REUSE_CONVERSATION_CONTEXT", "RETURN_GENERAL_ANSWER"],
    }
    response = {
        "assistant_content": source.get("content") or "上一轮开放问答结果。",
        "intent": "OPEN_QA",
        "confidence": 1.0,
        "agent": agent,
        "source": "open_qa",
        "requested_output_format": "PDF",
        "document_only_followup": True,
    }
    web_search = artifact.get("webSearch") if isinstance(artifact.get("webSearch"), dict) else None
    travel_guide = artifact.get("travelGuide") if isinstance(artifact.get("travelGuide"), dict) else None
    source_question = str(source.get("question") or "")
    source_content = str(source.get("content") or "")
    if not travel_guide and any(term in f"{source_question} {source_content}" for term in ("旅行", "旅游", "攻略", "行程")):
        details = OpenQuestionResponder._travel_plan_details(source_question)
        if details.get("destination") != "目的地":
            year_match = re.search(r"(20\d{2})\s*年", source_content)
            travel_guide = {
                "destination": details["destination"],
                "days": details["days"],
                "travel_year": int(year_match.group(1)) if year_match else None,
                "hotels": [],
                "restaurants": [],
                "images": [],
                "recommendation_notice": "本次文档沿用上一轮公开问答内容，动态信息须在预订前复核。",
            }
    if web_search:
        response["web_search"] = web_search
    if travel_guide:
        response["travel_guide"] = travel_guide
    return response


def _open_qa_document_path(tenant_id: str, conversation_id: str, document_id: str) -> Path:
    if not re.fullmatch(r"conv_[A-Za-z0-9_-]{6,64}", str(conversation_id or "")):
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    if not re.fullmatch(r"doc_[a-f0-9]{16}", str(document_id or "")):
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    tenant_scope = hashlib.sha256(str(tenant_id or "").encode("utf-8")).hexdigest()[:20]
    target = (OPEN_QA_EXPORT_DIR / tenant_scope / conversation_id / f"{document_id}.pdf").resolve()
    if not str(target).startswith(str(OPEN_QA_EXPORT_DIR.resolve())):
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
    return target


def _plain_document_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"```(?:[A-Za-z0-9_-]+)?\n?", "", text)
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def _travel_pdf_sections(answer: str, days: int) -> tuple[list[dict], list[tuple[str, str]]]:
    """Extract a stable itinerary and supporting sections from model/fallback text."""
    raw = str(answer or "").split("**住宿候选", 1)[0]
    plain = _plain_document_text(raw)
    compact = re.sub(r"\s+", " ", plain)
    stop = r"(?=第\s*\d+\s*天|住宿与交通|预算与预订|出发前清单|当前未获得|$)"
    itinerary = []
    seen_days = set()
    for match in re.finditer(rf"第\s*(\d+)\s*天\s*[：:]?\s*(.*?){stop}", compact):
        day = bounded_int(match.group(1), 1, 1, days)
        detail = re.sub(r"^\d+[.、]\s*", "", match.group(2)).strip(" 。；;")
        detail = re.sub(r"\s+\d+[.、]\s*$", "", detail).strip(" 。；;")
        if day not in seen_days and detail:
            itinerary.append({"day": day, "detail": detail[:420]})
            seen_days.add(day)
    fallback_days = (
        "抵达并入住交通便利区域，安排周边步行与城市初识，给航班延误和体力恢复留出余量。",
        "游览核心城区、代表性地标与一处文化场馆，把相邻地点组合成一条路线。",
        "探索历史街区、市场与本地餐饮，下午保留一段自由活动时间。",
        "从自然景观、主题体验或滨水区域中选择一条整日路线，热门项目提前核实预约。",
        "安排近郊或第二核心片区；若交通耗时过长，则改为市内深度游。",
        "购物、补漏与返程，按航班预留前往机场、退税和安检时间。",
    )
    for day in range(1, days + 1):
        if day in seen_days:
            continue
        detail = fallback_days[-1] if day == days else fallback_days[min(day - 1, len(fallback_days) - 2)]
        itinerary.append({"day": day, "detail": detail})
    itinerary.sort(key=lambda item: item["day"])

    sections = []
    current_heading = "攻略说明"
    current_lines = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        heading_match = re.fullmatch(r"(?:#{1,4}\s*)?\*\*([^*]+)\*\*", line) or re.fullmatch(r"#{1,4}\s+(.+)", line)
        if heading_match:
            if current_lines:
                sections.append((current_heading, " ".join(current_lines)))
            current_heading = heading_match.group(1).strip()
            current_lines = []
            continue
        cleaned = _plain_document_text(line)
        if not cleaned or re.search(r"第\s*\d+\s*天", cleaned) or "地图：https://www.google.com/maps/" in cleaned:
            continue
        current_lines.append(cleaned)
    if current_lines:
        sections.append((current_heading, " ".join(current_lines)))
    excluded = ("行程安排", "住宿候选", "餐饮候选", "PDF")
    return itinerary, [item for item in sections if not any(marker in item[0] for marker in excluded)][:5]


def _download_travel_pdf_images(images: list[dict]) -> dict[str, bytes]:
    from concurrent.futures import ThreadPoolExecutor

    candidates = [item for item in images[:3] if isinstance(item, dict) and item.get("thumbnail_url")]
    if not candidates:
        return {}
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        payloads = list(pool.map(TRAVEL_MEDIA_CLIENT.download, candidates))
    return {
        str(item.get("thumbnail_url")): payload
        for item, payload in zip(candidates, payloads)
        if isinstance(payload, bytes) and payload
    }


def _register_open_qa_pdf_font() -> str:
    """Embed a CJK-capable font when available so exported PDFs are portable."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = "DeepVisionCJK"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    configured_path = str(os.environ.get("AGI_PDF_FONT_PATH") or "").strip()
    candidates = [
        configured_path,
        str(ROOT / "static" / "fonts" / "NotoSansCJKsc-Regular.otf"),
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(path), subfontIndex=0))
            return font_name
        except Exception:
            continue
    fallback_name = "STSong-Light"
    if fallback_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback_name))
    return fallback_name


def generate_open_qa_pdf(user: dict, conversation_id: str, question: str, response: dict) -> dict:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        Image as PdfImage,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from xml.sax.saxutils import escape

    document_id = f"doc_{uuid.uuid4().hex[:16]}"
    target = _open_qa_document_path(user["tenant_id"], conversation_id, document_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf_font_name = _register_open_qa_pdf_font()
    styles = getSampleStyleSheet()
    palette = {
        "ink": colors.HexColor("#182236"),
        "body": colors.HexColor("#344258"),
        "muted": colors.HexColor("#6C778A"),
        "teal": colors.HexColor("#087E8B"),
        "coral": colors.HexColor("#C65D43"),
        "gold": colors.HexColor("#B9862E"),
        "paper": colors.HexColor("#F4F7F8"),
        "line": colors.HexColor("#DCE3E8"),
    }
    title_style = ParagraphStyle(
        "OpenQATitle", parent=styles["Title"], fontName=pdf_font_name, fontSize=25,
        leading=33, textColor=palette["ink"], alignment=TA_LEFT, spaceAfter=4 * mm,
    )
    subtitle_style = ParagraphStyle(
        "OpenQASubtitle", parent=styles["BodyText"], fontName=pdf_font_name, fontSize=11,
        leading=18, textColor=palette["muted"], spaceAfter=5 * mm,
    )
    heading_style = ParagraphStyle(
        "OpenQAHeading", parent=styles["Heading2"], fontName=pdf_font_name, fontSize=15,
        leading=22, textColor=palette["ink"], spaceBefore=5 * mm, spaceAfter=3 * mm,
    )
    subheading_style = ParagraphStyle(
        "OpenQASubheading", parent=styles["Heading3"], fontName=pdf_font_name, fontSize=11,
        leading=16, textColor=palette["teal"], spaceAfter=1.5 * mm,
    )
    body_style = ParagraphStyle(
        "OpenQABody", parent=styles["BodyText"], fontName=pdf_font_name, fontSize=10,
        leading=17, textColor=palette["body"], spaceAfter=2 * mm,
    )
    meta_style = ParagraphStyle(
        "OpenQAMeta", parent=body_style, fontSize=8, leading=12, textColor=palette["muted"],
    )
    small_link_style = ParagraphStyle(
        "OpenQALink", parent=meta_style, fontSize=8.3, leading=13, textColor=palette["teal"],
    )
    created_at = now_iso()
    guide = response.get("travel_guide") if isinstance(response.get("travel_guide"), dict) else None
    is_travel = bool(guide) or any(term in question for term in ("旅行", "旅游", "攻略", "行程"))
    destination = str((guide or {}).get("destination") or "目的地")
    days = bounded_int((guide or {}).get("days"), 5, 1, 14)
    title = f"{destination} {days} 日旅行攻略" if guide else "旅行攻略" if is_travel else "开放问答结果"
    images = (guide or {}).get("images") if isinstance((guide or {}).get("images"), list) else []
    citations = ((response.get("web_search") or {}).get("citations") or [])[:8]
    image_payloads = _download_travel_pdf_images(images) if guide else {}
    embedded_images = [
        item for item in images
        if isinstance(item, dict) and str(item.get("thumbnail_url") or "") in image_payloads
    ]
    story = []

    def paragraph(text: str, style=body_style):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)

    def image_flowable(item: dict, max_width, max_height):
        payload = image_payloads.get(str(item.get("thumbnail_url") or ""))
        if not payload:
            return None
        buffer = BytesIO(payload)
        try:
            width, height = ImageReader(buffer).getSize()
        except Exception:
            return None
        scale = min(max_width / width, max_height / height)
        buffer.seek(0)
        return PdfImage(buffer, width=width * scale, height=height * scale)

    if guide:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph(escape(title), title_style))
        year = guide.get("travel_year")
        story.append(paragraph(
            f"面向 {year or '计划年份'} 的可执行初稿｜行程、住宿、餐饮与公开来源一并整理",
            subtitle_style,
        ))
        cover_item = embedded_images[0] if embedded_images else None
        cover = image_flowable(cover_item, 174 * mm, 67 * mm) if cover_item else None
        if cover:
            story.append(Table([[cover]], colWidths=[174 * mm], style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), palette["paper"]),
                ("BOX", (0, 0), (-1, -1), 0.5, palette["line"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ])))
            story.append(Spacer(1, 4 * mm))
        facts = [
            [paragraph("目的地", meta_style), paragraph(destination, subheading_style)],
            [paragraph("行程长度", meta_style), paragraph(f"{days} 天", subheading_style)],
            [paragraph("信息时点", meta_style), paragraph(str(year or created_at[:10]), subheading_style)],
        ]
        facts_table = Table(facts, colWidths=[30 * mm, 140 * mm])
        facts_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), palette["paper"]),
            ("GRID", (0, 0), (-1, -1), 0.4, palette["line"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ]))
        story.append(facts_table)
        exact_hotels = [
            item for item in (guide.get("hotels") if isinstance(guide.get("hotels"), list) else [])
            if isinstance(item, dict) and item.get("address_verified")
            and is_specific_venue_name(item.get("name") or "", "hotels", item.get("summary") or "", destination)
            and is_precise_venue_address(item.get("address") or "")
        ]
        exact_restaurants = [
            item for item in (guide.get("restaurants") if isinstance(guide.get("restaurants"), list) else [])
            if isinstance(item, dict) and item.get("address_verified")
            and is_specific_venue_name(item.get("name") or "", "restaurants", item.get("summary") or "", destination)
            and is_precise_venue_address(item.get("address") or "")
        ]
        coverage = Table(
            [[
                Paragraph(f"<b>{len(exact_hotels)}</b><br/>可核验住宿", body_style),
                Paragraph(f"<b>{len(exact_restaurants)}</b><br/>可核验餐饮", body_style),
                Paragraph(f"<b>{len(citations)}</b><br/>公开来源", body_style),
            ]],
            colWidths=[56 * mm, 56 * mm, 56 * mm],
        )
        coverage.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, palette["line"]),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, palette["line"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ]))
        story.extend([Spacer(1, 3 * mm), coverage, Spacer(1, 4 * mm)])
        story.append(paragraph("本攻略依据用户问题与公开网页候选生成。地点推荐不代表商业背书，动态价格、营业状态、入境规则与预订条件须在出发前复核。", body_style))
        if cover:
            story.append(PageBreak())
        else:
            story.append(Spacer(1, 3 * mm))

        itinerary, supporting_sections = _travel_pdf_sections(response.get("assistant_content") or "", days)
        story.append(Paragraph("逐日行程", heading_style))
        story.append(paragraph("按相邻区域组织每天活动，保留天气、体力和临时闭馆的调整空间。", subtitle_style))
        for item in itinerary:
            day_badge = Paragraph(f"<b>DAY {item['day']}</b>", ParagraphStyle(
                f"Day{item['day']}", parent=meta_style, fontName="Helvetica-Bold", fontSize=9,
                textColor=colors.white, alignment=TA_CENTER,
            ))
            day_text = paragraph(item["detail"], body_style)
            card = Table([[day_badge, day_text]], colWidths=[23 * mm, 145 * mm])
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), palette["teal"] if item["day"] % 2 else palette["coral"]),
                ("BACKGROUND", (1, 0), (1, 0), palette["paper"]),
                ("BOX", (0, 0), (-1, -1), 0.5, palette["line"]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]))
            story.extend([card, Spacer(1, 2.5 * mm)])
        for section_title, section_text in supporting_sections:
            story.append(Paragraph(escape(section_title), heading_style))
            story.append(paragraph(section_text, body_style))

        def add_recommendations(key: str, section_title: str, accent):
            raw_items = guide.get(key) if isinstance(guide.get(key), list) else []
            items = [
                item for item in raw_items
                if isinstance(item, dict) and item.get("address_verified")
                and is_specific_venue_name(item.get("name") or "", key, item.get("summary") or "", destination)
                and is_precise_venue_address(item.get("address") or "")
            ][:4]
            if not items:
                empty_state = Table(
                    [[Paragraph(
                        f"<b>{escape(section_title)}</b>　本次无通过“具体场所名 + 精确门牌地址”校验的候选；可补充落脚城市或商圈后重新生成。",
                        body_style,
                    )]],
                    colWidths=[168 * mm],
                )
                empty_state.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), palette["paper"]),
                    ("BOX", (0, 0), (-1, -1), 0.6, accent),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]))
                story.extend([
                    Spacer(1, 2 * mm),
                    KeepTogether([empty_state]),
                ])
                return
            cards = []
            for index, item in enumerate(items, start=1):
                address_note = "酒店地址" if key == "hotels" else "餐厅地址"
                source_url = escape(str(item.get("source_url") or ""))
                map_url = escape(str(item.get("map_url") or ""))
                place_data_url = escape(str(item.get("place_data_url") or ""))
                place_link = f'　<link href="{place_data_url}" color="#B9862E">地点数据</link>' if place_data_url else ""
                links = Paragraph(
                    f'<link href="{source_url}" color="#087E8B">查看来源</link>　'
                    f'<link href="{map_url}" color="#C65D43">地图导航</link>{place_link}',
                    small_link_style,
                )
                content = [
                    Paragraph(escape(f"{index}. {item.get('name') or '候选地点'}"), subheading_style),
                    paragraph(f"{address_note}：{item.get('address') or '请在地图中核实'}", body_style),
                    paragraph(str(item.get("summary") or "")[:260], meta_style),
                    links,
                ]
                card = Table([[content]], colWidths=[168 * mm])
                card.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.7, accent),
                    ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 3.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * mm),
                ]))
                cards.append(card)
            story.extend([
                KeepTogether([
                    Paragraph(section_title, heading_style),
                    paragraph("仅展示名称与精确门牌地址均可核验的公开候选；预订前请再确认价格和营业状态。", subtitle_style),
                    cards[0],
                ]),
                Spacer(1, 3 * mm),
            ])
            for card in cards[1:]:
                story.extend([KeepTogether([card]), Spacer(1, 3 * mm)])

        add_recommendations("hotels", "住宿候选", palette["teal"])
        add_recommendations("restaurants", "餐饮候选", palette["coral"])

        gallery_rows = []
        gallery_items = embedded_images[1:] if cover else embedded_images
        for item in gallery_items[:2]:
            flowable = image_flowable(item, 79 * mm, 48 * mm)
            if not flowable:
                continue
            caption = paragraph(
                f"{item.get('title') or destination}\n{item.get('author') or 'Wikimedia Commons'} · {item.get('license') or '许可见来源'}",
                meta_style,
            )
            gallery_rows.append([flowable, caption, item])
        if gallery_rows:
            if len(gallery_rows) == 1:
                _flowable, caption, item = gallery_rows[0]
                compact_image = image_flowable(item, 70 * mm, 41 * mm)
                gallery = Table([[compact_image, caption]], colWidths=[75 * mm, 93 * mm])
            else:
                cells = []
                for flowable, caption, _item in gallery_rows:
                    cells.append([flowable, Spacer(1, 1.5 * mm), caption])
                gallery = Table([cells], colWidths=[84 * mm, 84 * mm])
            gallery.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.4, palette["line"]),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, palette["line"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]))
            story.append(KeepTogether([Paragraph("目的地印象", heading_style), gallery]))
    else:
        story.extend([
            Paragraph(escape(title), title_style),
            Paragraph("问题", heading_style),
            paragraph(_plain_document_text(question), body_style),
            Paragraph("方案", heading_style),
        ])
        for raw_line in _plain_document_text(response.get("assistant_content") or "").splitlines():
            if raw_line.strip():
                story.append(paragraph(raw_line.strip(), body_style))

    if citations or embedded_images:
        story.append(PageBreak())
        story.append(Paragraph("公开来源与素材授权", heading_style))
        story.append(paragraph("网页摘要仅用于形成候选与核验线索；请打开原始页面确认最新信息。", subtitle_style))
        for index, item in enumerate(citations, start=1):
            url = escape(str(item.get("url") or ""))
            label = escape(str(item.get("title") or item.get("domain") or "网页来源"))
            story.append(Paragraph(f'[{index}] <link href="{url}" color="#087E8B">{label}</link>', small_link_style))
            if item.get("snippet"):
                story.append(paragraph(str(item.get("snippet"))[:360], meta_style))
        if embedded_images:
            story.append(Paragraph("图片署名", heading_style))
            for index, item in enumerate(embedded_images[:3], start=1):
                source_url = escape(str(item.get("source_url") or "https://commons.wikimedia.org/"))
                credit = f"图 {index}：{item.get('title') or destination}｜{item.get('author') or 'Wikimedia Commons contributor'}｜{item.get('license') or '许可见来源'}"
                story.append(Paragraph(f'{escape(credit)}｜<link href="{source_url}" color="#087E8B">素材页</link>', meta_style))
        if guide and guide.get("places_attribution_url"):
            attribution_url = escape(str(guide.get("places_attribution_url")))
            story.append(Paragraph(
                f'地点名称、坐标与部分地址使用 <link href="{attribution_url}" color="#B9862E">Wikidata 开放数据</link>；推荐线索来自上列公开网页。',
                meta_style,
            ))
    story.append(Spacer(1, 4 * mm))
    story.append(paragraph(f"生成时间：{created_at}（Asia/Shanghai）", meta_style))

    doc = SimpleDocTemplate(
        str(target), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=19 * mm, bottomMargin=17 * mm, title=title, author="深象万象开放问答助手",
    )

    def draw_page(canvas, document):
        canvas.saveState()
        width, height = A4
        if document.page > 1:
            canvas.setFont(pdf_font_name, 8)
            canvas.setFillColor(palette["muted"])
            canvas.drawString(18 * mm, height - 10 * mm, title[:42])
            canvas.setStrokeColor(palette["line"])
            canvas.line(18 * mm, height - 12 * mm, width - 18 * mm, height - 12 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(palette["muted"])
        canvas.drawRightString(width - 18 * mm, 9 * mm, f"{document.page}")
        canvas.drawString(18 * mm, 9 * mm, "DeepVision Open QA")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return {
        "document_id": document_id,
        "title": title,
        "filename": f"{title}-{created_at[:10]}.pdf",
        "mime_type": "application/pdf",
        "size_bytes": target.stat().st_size,
        "created_at": created_at,
        "download_url": f"/api/conversations/{conversation_id}/documents/{document_id}/download",
    }


def apply_requested_open_qa_document(user: dict, conversation_id: str, question: str, response: dict) -> None:
    if str(response.get("requested_output_format") or "").upper() != "PDF":
        return
    document_only_followup = bool(response.pop("document_only_followup", False))
    agent = response.get("agent") if isinstance(response.get("agent"), dict) else {}
    tool_calls = agent.setdefault("tool_calls", [])
    decision = agent.setdefault("decision", {})
    try:
        generated = generate_open_qa_pdf(user, conversation_id, question, response)
    except Exception as exc:  # The answer remains useful when document rendering fails.
        tool_calls.append("document.generate_pdf:failed")
        agent["warning"] = "文本回答已完成，但 PDF 文档生成失败"
        decision["tool_state"] = "PARTIAL_FAILURE"
        response["assistant_content"] = (
            "PDF 文档本次生成失败，请稍后重试。"
            if document_only_followup
            else f"{str(response.get('assistant_content') or '').rstrip()}\n\nPDF 文档本次生成失败，请稍后重试。"
        )
        response["document_error"] = type(exc).__name__
        return
    response["generated_document"] = generated
    tool_calls.append("document.generate_pdf")
    allowed_tools = decision.setdefault("allowed_tools", [])
    if "document.generate_pdf" not in allowed_tools:
        allowed_tools.append("document.generate_pdf")
    decision["tool_state"] = "SUCCEEDED"
    stages = agent.setdefault("stages", [])
    if "GENERATE_DOCUMENT" not in stages:
        return_index = stages.index("RETURN_GENERAL_ANSWER") if "RETURN_GENERAL_ANSWER" in stages else len(stages)
        stages.insert(return_index, "GENERATE_DOCUMENT")
    if document_only_followup:
        response["assistant_content"] = "已根据上一轮内容生成 PDF 文档，可在下方下载。"
    else:
        response["assistant_content"] = f"{str(response.get('assistant_content') or '').rstrip()}\n\n已生成 PDF 文档，可在下方下载。"


def complete_open_qa_message(conn, user: dict, conversation_id: str, content: str, response: dict) -> dict:
    """Persist the isolated OPEN_QA response with the same audit contract as online mode."""
    apply_web_search_usage_to_response(conn, user, conversation_id, response)
    apply_requested_open_qa_document(user, conversation_id, content, response)
    user_message = add_message(conn, conversation_id, "user", content)
    artifact = conversation_artifact(response) or {}
    linked_object = attach_agent_trace(
        {"agent": response["agent"], "artifact": artifact, "source": "open_qa"},
        content,
    )
    assistant = add_message(
        conn,
        conversation_id,
        "assistant",
        response["assistant_content"],
        None,
        linked_object,
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "agent.open_qa.query",
        "message",
        user_message["message_id"],
        None,
        {
            "intent": "OPEN_QA",
            "engine": response["agent"].get("engine"),
            "tools": response["agent"].get("tool_calls") or [],
            "source": "public_web" if response["agent"].get("data_source") == "public_web" else "open_qa",
        },
        "agent",
        None,
    )
    response["messages"] = [serialize_message(assistant)]
    response["agent"] = linked_object.get("agent")
    return response


def open_question_responder_for_request(conn, tenant_id: str | None = None) -> OpenQuestionResponder:
    """Use the approved model configuration without passing tenant data to OPEN_QA."""
    try:
        config, _cache_token = model_runtime_config(conn)
        web_search_config, _web_search_cache_token = web_search_runtime_config(conn, tenant_id)
    except ApiError:
        config = {}
        web_search_config = {}
    return OpenQuestionResponder(
        IntentAnalyzer(config),
        WebSearchClient(web_search_config),
        web_search_usage_summary(conn, web_search_config),
    )


def new_domain_audit_logger(conn, user: dict):
    """Adapt the legacy audit table without allowing raw new-domain input in it."""
    def _write(*, action: str, object_type: str, object_id: str, after=None, **_ignored):
        log_audit(
            conn,
            user["user_id"],
            user["tenant_id"],
            action,
            object_type,
            object_id,
            None,
            after if isinstance(after, dict) else {"summary_hash": summary_hash(after)},
            "agent_governance",
            None,
        )
    return _write


def new_domain_gate_engine(conn, user: dict, *, domain: str, action: str, conversation_id: str | None, input_value) -> tuple[GateEngine, GateContext]:
    context = GateContext(
        request_id=f"req_{uuid.uuid4().hex[:16]}",
        tenant_id=user["tenant_id"],
        user_id=user["user_id"],
        conversation_id=conversation_id,
        requested_domain=domain,
        action=action,
        input_summary_hash=summary_hash(input_value),
    )
    return GateEngine(conn, now=now_iso(), audit_logger=new_domain_audit_logger(conn, user)), context


def open_research_service_for_request(conn, user: dict) -> OpenResearchService:
    from open_research.detail_fetch import SafeDetailFetcher
    from open_research.evidence_reasoner import EvidenceReasoner
    config, _cache_token = web_search_runtime_config(conn, user["tenant_id"])
    try:
        model_config, _model_cache_token = model_runtime_config(conn)
    except ApiError:
        model_config = {}
    gateway = OPEN_RESEARCH_GATEWAY_FACTORY(conn, user) if OPEN_RESEARCH_GATEWAY_FACTORY else TavilyGateway(WebSearchClient(config))
    aliases = {}
    for item in rows(
        conn,
        """SELECT alias_text, canonical_entity, confidence, reason
           FROM open_research_entity_aliases
           WHERE tenant_id=? AND status='ACTIVE' ORDER BY updated_at DESC""",
        (user["tenant_id"],),
    ):
        aliases[str(item["alias_text"])] = (
            str(item["canonical_entity"]),
            float(item["confidence"]),
            str(item["reason"]),
        )
    return OpenResearchService(
        conn,
        gateway,
        audit_logger=new_domain_audit_logger(conn, user),
        resolver=EntityResolver(aliases),
        # The reader is intentionally isolated from the Tavily adapter.  It
        # receives only a provider-returned public URL and persists no body.
        detail_fetcher=SafeDetailFetcher(),
        # The model receives only this run's public, bounded evidence package;
        # tenant context, conversation history and provider credentials are
        # never included in the synthesis prompt.
        reasoner=EvidenceReasoner(model_config),
    )


def realtime_research_followup_query(conn, user: dict, conversation_id: str, content: str) -> str | None:
    """Resolve an elliptical live follow-up without carrying a past fact.

    Only the prior *user question* is used as an entity/scope template.  The
    previous answer, claims, citations and evidence are never read into the
    new research run, and only NO_MEMORY runs are eligible.
    """
    normalized = re.sub(r"\s+", "", str(content or ""))
    if not re.fullmatch(r"(?:那|那就|这个|它)?(?:现在|最新|今天|此刻)(?:呢|怎么样|情况)?|(?:这个|它)(?:价格|天气|余票|航班|状态)呢", normalized):
        return None
    assistant_rows = rows(
        conn,
        """SELECT rowid, linked_object FROM messages
           WHERE conversation_id=? AND sender='assistant' ORDER BY rowid DESC LIMIT 12""",
        (conversation_id,),
    )
    for assistant in assistant_rows:
        linked = json_loads(assistant.get("linked_object"), {})
        research = (((linked.get("artifact") or {}).get("research")) if isinstance(linked, dict) else {}) or {}
        if research.get("retention_class") != "NO_MEMORY":
            continue
        prior = one(
            conn,
            """SELECT content FROM messages
               WHERE conversation_id=? AND sender='user' AND rowid<?
               ORDER BY rowid DESC LIMIT 1""",
            (conversation_id, assistant["rowid"]),
        )
        question = str((prior or {}).get("content") or "").strip()
        if not question or question.startswith("【已拦截"):
            continue
        return f"{question.rstrip('？?。！! ')} 现在最新情况"
    return None


def open_research_gateway_available(conn, tenant_id: str) -> bool:
    if OPEN_RESEARCH_GATEWAY_FACTORY is not None:
        return True
    config, _cache_token = web_search_runtime_config(conn, tenant_id)
    return WebSearchClient(config).configured and str(config.get("provider") or "").lower() == "tavily"


def office_asset_service_for_request(conn, user: dict) -> OfficeAssetService:
    # The local adapter deliberately has no network capability.  Production
    # supplies a scanner and enables require_scanner through deployment config.
    return OfficeAssetService(
        conn,
        OFFICE_ASSET_DIR,
        require_scanner=str(os.environ.get("AGI_OFFICE_REQUIRE_VIRUS_SCANNER") or "").lower() in {"1", "true", "yes"},
    )


def office_job_service_for_request(conn, user: dict) -> OfficeJobService:
    return OfficeJobService(
        conn,
        office_asset_service_for_request(conn, user),
        OFFICE_ARTIFACT_DIR,
        audit_logger=new_domain_audit_logger(conn, user),
    )


def office_job_service_for_worker(conn, tenant_id: str, user_id: str) -> OfficeJobService:
    """No chat/inspection context is granted to an Office worker."""
    return OfficeJobService(
        conn,
        OfficeAssetService(
            conn,
            OFFICE_ASSET_DIR,
            require_scanner=str(os.environ.get("AGI_OFFICE_REQUIRE_VIRUS_SCANNER") or "").lower() in {"1", "true", "yes"},
        ),
        OFFICE_ARTIFACT_DIR,
        audit_logger=new_domain_audit_logger(conn, {"tenant_id": tenant_id, "user_id": user_id}),
    )


def research_message_content(question: str) -> str:
    """Do not put rejected enterprise/secret/PII query content into chat rows."""
    from open_research.boundary import classify_query
    return question if classify_query(question) == "PUBLIC" else "【已拦截的公开检索请求】"


def research_response_copy(result: dict) -> str:
    answer = result.get("answer") if isinstance(result.get("answer"), dict) else {}
    text = str(answer.get("text") or "")
    if text:
        return text
    reason = str(result.get("reason_code") or "")
    if reason == "FEATURE_DISABLED":
        return "当前租户尚未开启开放信息检索能力。"
    if reason == "QUERY_REWRITE_CLARIFICATION_REQUIRED":
        candidates = ((result.get("rewrite") or {}).get("candidates") or [])
        return f"我识别到可能的实体歧义，请确认检索对象：{'、'.join(candidates)}。" if candidates else "我识别到实体可能有歧义，请补充准确名称。"
    return "该请求未进入公开检索，系统没有发送任何内容到外部搜索服务。"


def feedback_reason_for_storage(value) -> str | None:
    """Feedback is a quality signal, not a second private message store."""
    raw = str(value or "").strip()
    return f"sha256:{summary_hash(raw)}" if raw else None


def research_history_payload(result: dict) -> dict:
    """Return the only research artifact allowed in chat/history projection.

    Runs and audit tables can contain hashes and policy decisions.  A record
    page is intentionally built from this final delivery artifact instead of
    raw provider payloads: no HTML, full page text, unadopted result cards or
    internal gate trace can leak through a second read path.
    """
    answer = result.get("answer") if isinstance(result.get("answer"), dict) else {}
    citations = []
    for item in result.get("citations") or []:
        if not isinstance(item, dict):
            continue
        citation = {
            key: item.get(key)
            for key in (
                "evidence_id", "title", "canonical_url", "publisher", "published_at", "fetched_at",
                "source_tier", "source_policy_id", "evidence_type", "detail_fetch_status", "extraction_locator_type",
                "source_reputation", "relevance_score", "freshness_score", "semantic_score", "evidence_confidence",
            )
        }
        snippet = re.sub(r"\s+", " ", str(item.get("snippet") or "")).strip()[:300]
        if snippet:
            citation["snippet"] = snippet
        citations.append(citation)
    return {
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "as_of": result.get("as_of"),
        "fact_intent": result.get("fact_intent"),
        "territory_assumption": result.get("territory_assumption"),
        "retention_class": result.get("retention_class") or "NO_MEMORY",
        "force_fresh": bool(result.get("force_fresh")),
        "memory_hit": bool(result.get("memory_hit")),
        "answer": {
            key: answer.get(key)
            for key in ("status", "claim_status", "text", "claims", "evidence_synthesis")
        },
        "claims": list(result.get("claims") or []),
        "citations": citations,
        "rewrite": result.get("rewrite") if isinstance(result.get("rewrite"), dict) else None,
    }


def create_open_research_history_record(conn, user: dict, *, result: dict, conversation_id: str,
                                        user_message_id: str, assistant_message_id: str) -> None:
    run_id = str(result.get("run_id") or "")
    if not run_id:
        return
    run = one(
        conn,
        """SELECT run_id, fact_intent, quality_status, retention_class, force_fresh, as_of, created_at
           FROM open_research_runs WHERE run_id=? AND tenant_id=? AND user_id=?""",
        (run_id, user["tenant_id"], user["user_id"]),
    )
    if not run:
        return
    conn.execute(
        """INSERT OR REPLACE INTO open_research_history_records(
               run_id, tenant_id, user_id, conversation_id, user_message_id, assistant_message_id,
               fact_intent, quality_status, retention_class, force_fresh, completed_at, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, user["tenant_id"], user["user_id"], conversation_id, user_message_id, assistant_message_id,
            run["fact_intent"], run["quality_status"], run["retention_class"], int(run["force_fresh"] or 0),
            run["as_of"], run["created_at"],
        ),
    )


def _research_history_filter_sql(query: dict) -> tuple[str, list]:
    clauses: list[str] = []
    values: list = []
    for key, column in (("fact_intent", "h.fact_intent"), ("quality_status", "h.quality_status"), ("retention_class", "h.retention_class")):
        value = str(query.get(key) or "").strip().upper()
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    start = str(query.get("start_at") or "").strip()
    end = str(query.get("end_at") or "").strip()
    if start:
        clauses.append("h.completed_at>=?")
        values.append(start)
    if end:
        clauses.append("h.completed_at<=?")
        values.append(end)
    feedback = str(query.get("feedback_status") or "").strip().upper()
    if feedback:
        clauses.append("COALESCE((SELECT f.feedback_type FROM agent_feedback f WHERE f.domain='OPEN_RESEARCH' AND f.resource_id=h.run_id ORDER BY f.created_at DESC LIMIT 1), 'NONE')=?")
        values.append(feedback)
    keyword = str(query.get("q") or "").strip()[:180]
    if keyword:
        clauses.append("(u.content LIKE ? OR r.rewrite_json LIKE ?)")
        values.extend([f"%{keyword}%", f"%{keyword}%"])
    return (" AND " + " AND ".join(clauses)) if clauses else "", values


def list_open_research_history(conn, user: dict, query: dict) -> dict:
    try:
        page = max(1, int(query.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(50, max(1, int(query.get("page_size") or 20)))
    except (TypeError, ValueError):
        page_size = 20
    filters, filter_values = _research_history_filter_sql(query)
    base = """
        FROM open_research_history_records h
        JOIN open_research_runs r ON r.run_id=h.run_id
        JOIN messages u ON u.message_id=h.user_message_id
        JOIN messages a ON a.message_id=h.assistant_message_id
        WHERE h.tenant_id=? AND h.user_id=?
    """
    total = int((one(conn, "SELECT COUNT(*) AS count " + base + filters, (user["tenant_id"], user["user_id"], *filter_values)) or {}).get("count") or 0)
    records = rows(
        conn,
        """SELECT h.run_id, h.conversation_id, h.fact_intent, h.quality_status, h.retention_class,
                  h.force_fresh, h.completed_at, r.as_of, r.rewrite_json, u.content AS question,
                  a.content AS answer_text,
                  COALESCE((SELECT f.feedback_type FROM agent_feedback f
                            WHERE f.domain='OPEN_RESEARCH' AND f.resource_id=h.run_id
                            ORDER BY f.created_at DESC LIMIT 1), 'NONE') AS feedback_status
        """ + base + filters + " ORDER BY h.completed_at DESC, h.run_id DESC LIMIT ? OFFSET ?",
        (user["tenant_id"], user["user_id"], *filter_values, page_size, (page - 1) * page_size),
    )
    for item in records:
        rewrite = json_loads(item.pop("rewrite_json", "{}"), {})
        item["rewrite"] = rewrite.get("rewritten_query") if isinstance(rewrite, dict) else None
        item["force_fresh"] = bool(item.get("force_fresh"))
        item["real_time_requery_required"] = item.get("retention_class") == "NO_MEMORY"
    return {"records": records, "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, (total + page_size - 1) // page_size)}}


def get_open_research_history_record(conn, user: dict, run_id: str) -> dict | None:
    row = one(
        conn,
        """SELECT h.*, r.as_of, u.content AS question, a.content AS answer_text, a.linked_object
           FROM open_research_history_records h
           JOIN open_research_runs r ON r.run_id=h.run_id
           JOIN messages u ON u.message_id=h.user_message_id
           JOIN messages a ON a.message_id=h.assistant_message_id
           WHERE h.run_id=? AND h.tenant_id=? AND h.user_id=?""",
        (run_id, user["tenant_id"], user["user_id"]),
    )
    if not row:
        return None
    linked = json_loads(row.get("linked_object"), {})
    research = (((linked.get("artifact") or {}).get("research")) if isinstance(linked, dict) else {}) or {}
    # Defensive second allow-list: future message artifacts must not widen the
    # record endpoint by accident.
    detail = {
        "run_id": row["run_id"], "conversation_id": row["conversation_id"], "question": row["question"],
        "answer": research.get("answer") if isinstance(research.get("answer"), dict) else {"text": row["answer_text"]},
        "claims": list(research.get("claims") or []), "citations": list(research.get("citations") or []),
        "rewrite": research.get("rewrite") if isinstance(research.get("rewrite"), dict) else None,
        "status": row["quality_status"], "fact_intent": row["fact_intent"], "retention_class": row["retention_class"],
        "force_fresh": bool(row["force_fresh"]), "as_of": row["as_of"], "completed_at": row["completed_at"],
        "real_time_requery_required": row["retention_class"] == "NO_MEMORY",
    }
    return detail


def invalidate_open_research_memories_for_run(conn, user: dict, run_id: str, *, reason: str) -> int:
    """Invalidate private knowledge when its supporting delivery is rejected."""
    evidence_ids = {
        str(item["evidence_id"])
        for item in rows(
            conn,
            """SELECT e.evidence_id FROM open_research_evidence e
               JOIN open_research_runs r ON r.run_id=e.run_id
               WHERE e.run_id=? AND r.tenant_id=? AND r.user_id=?""",
            (run_id, user["tenant_id"], user["user_id"]),
        )
    }
    if not evidence_ids:
        return 0
    updated = 0
    for memory in rows(
        conn,
        """SELECT memory_id, memory_json FROM open_research_memory_index
           WHERE tenant_id=? AND user_id=? AND status='ACTIVE'""",
        (user["tenant_id"], user["user_id"]),
    ):
        payload = json_loads(memory.get("memory_json"), {})
        if not evidence_ids.intersection({str(item) for item in payload.get("evidence_ids") or []}):
            continue
        result = conn.execute(
            """UPDATE open_research_memory_index SET status='INVALIDATED', updated_at=?, deleted_at=?
               WHERE memory_id=? AND tenant_id=? AND user_id=? AND status='ACTIVE'""",
            (now_iso(), now_iso(), memory["memory_id"], user["tenant_id"], user["user_id"]),
        )
        updated += max(0, result.rowcount)
    if updated:
        log_audit(
            conn, user["user_id"], user["tenant_id"], "open_research.memory.invalidate", "open_research_run", run_id,
            None, {"reason": reason, "invalidated_count": updated}, "open_research", None,
        )
    return updated


def add_open_research_message(conn, user: dict, conversation_id: str, question: str, result: dict) -> dict:
    user_message = add_message(conn, conversation_id, "user", research_message_content(question))
    rewrite = result.get("rewrite") if isinstance(result.get("rewrite"), dict) else None
    history_artifact = research_history_payload(result)
    linked = {
        "source": "open_research",
        "agent": {
            "intent": "OPEN_RESEARCH",
            "mode": "OPEN_RESEARCH",
            "engine": "evidence_first_tavily",
            "status": "BLOCKED" if result.get("status") == "BLOCKED" else "SUCCEEDED",
            "tool_calls": ["tavily.search"] if result.get("run_id") else [],
            "rewrite": rewrite,
            "reason_code": result.get("reason_code"),
        },
        "artifact": {"research": {
            "run_id": result.get("run_id"), "status": result.get("status"), "as_of": result.get("as_of"),
            "fact_intent": result.get("fact_intent"), "territory_assumption": result.get("territory_assumption"),
            "memory_hit": bool(result.get("memory_hit")), "answer": history_artifact["answer"],
            "claims": history_artifact["claims"], "citations": history_artifact["citations"], "rewrite": rewrite,
            "retention_class": history_artifact["retention_class"], "force_fresh": history_artifact["force_fresh"],
        }},
    }
    assistant = add_message(conn, conversation_id, "assistant", research_response_copy(result), None, linked)
    create_open_research_history_record(
        conn, user, result=result, conversation_id=conversation_id,
        user_message_id=user_message["message_id"], assistant_message_id=assistant["message_id"],
    )
    log_audit(
        conn, user["user_id"], user["tenant_id"], "open_research.deliver", "open_research_run",
        str(result.get("run_id") or user_message["message_id"]), None,
        audit_payload(status=result.get("status"), reason_code=result.get("reason_code"), query=question, workflow_id=result.get("workflow_id")),
        "open_research", None,
    )
    return {"intent": "OPEN_RESEARCH", "confidence": 0.95, "research": result,
            "agent": linked["agent"], "messages": [serialize_message(assistant)]}


def create_plan(conn, user, conversation_id: str, intent: str, risk_level: str, status: str, slots: dict, actions: list, validators: list, confirm_required: bool, summary: str, validation: dict, idempotency_key: str) -> dict:
    plan_id = f"plan_{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    conn.execute(
        "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            plan_id,
            user["tenant_id"],
            user["user_id"],
            conversation_id,
            intent,
            risk_level,
            status,
            json_dumps(slots),
            json_dumps(actions),
            json_dumps(validators),
            1 if confirm_required else 0,
            summary,
            json_dumps(validation),
            idempotency_key,
            None,
            None,
            ts,
            ts,
        ),
    )
    return serialize_plan(one(conn, "SELECT * FROM plans WHERE plan_id=?", (plan_id,)))


def serialize_plan(plan: dict) -> dict:
    item = dict(plan)
    item["slots"] = json_loads(item["slots"], {})
    item["actions"] = json_loads(item["actions"], [])
    item["validators"] = json_loads(item["validators"], [])
    item["validation_result"] = json_loads(item["validation_result"], {})
    item["confirm_required"] = bool(item["confirm_required"])
    item["result"] = json_loads(item.get("result"), None)
    return item


def is_scheduled_inspection_request(text: str) -> bool:
    interval = re.search(r"每\s*(?:隔\s*)?(?:\d+|[一二三四五六七八九十两半]{1,3})\s*(?:h|H|小时|分钟|min)", text)
    fixed_daily = parse_fixed_daily_time(text)
    action_like = any(word in text for word in ("巡检", "看下", "看看", "看一下", "检查", "分析", "截取", "抓取", "轮询"))
    return bool((interval or fixed_daily) and action_like)


def parse_fixed_daily_time(text: str) -> dict | None:
    match = re.search(
        r"(?:每天|每日)\s*(凌晨|上午|下午|早上|中午|晚上)?\s*(\d{1,2})\s*"
        r"(?:(?:[:：]\s*(?P<minute_colon>\d{1,2})(?!\d))|(?:点|时)\s*(?:(?P<minute_cn>\d{1,2})(?!\d)\s*分?|(?P<half>半)|(?![半\d])))"
        r"\s*(?!到|~|～|-|—|至)",
        text,
    )
    if not match:
        return None
    hour = normalize_cn_hour(match.group(2), match.group(1))
    minute = 30 if match.group("half") else int(match.group("minute_colon") or match.group("minute_cn") or 0)
    if hour > 23 or minute > 59:
        return None
    time_text = f"{hour:02d}:{minute:02d}"
    return {
        "mode": "fixed_daily",
        "start_time": time_text,
        "end_time": time_text,
        "fixed_time": time_text,
        "label": f"每天 {time_text} 执行",
    }


def fixed_daily_first_run_requested(text: str) -> bool:
    """Require an explicit immediate instruction before bypassing a fixed daily time.

    "每天 11 点巡检" describes the first eligible run as 11:00.  It does not
    implicitly authorize a capture at task-creation time.  Users can still ask
    for an initial check with wording such as "现在先巡检一遍，并每天 11 点执行".
    """
    return bool(
        re.search(
            r"(?:立即|马上|现在|先)\s*(?:执行|巡检|检查|抓图|抓取|跑(?:一遍)?)",
            text,
        )
    )


def parse_interval_minutes(text: str) -> int | None:
    match = re.search(r"每\s*(?:隔\s*)?(\d+|[一二三四五六七八九十两半]{1,3})\s*(h|H|小时|分钟|min)", text)
    if not match:
        return 24 * 60 if parse_fixed_daily_time(text) else None
    raw_value = match.group(1)
    unit = match.group(2).lower()
    if raw_value == "半":
        minutes = 30 if unit in {"h", "小时"} else 0
    else:
        value = int(raw_value) if raw_value.isdigit() else parse_cn_number(raw_value)
        if not value:
            return None
        minutes = value * 60 if unit in {"h", "小时"} else value
    return minutes if 5 <= minutes <= 24 * 60 else None


CN_NUMBERS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def parse_cn_number(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value in CN_NUMBERS:
        return CN_NUMBERS[value]
    value = value.replace("两", "二")
    if "十" in value:
        left, _, right = value.partition("十")
        tens = CN_NUMBERS.get(left, 1) if left else 1
        ones = CN_NUMBERS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def last_day_of_month(year: int, month: int) -> date | None:
    if month < 1 or month > 12:
        return None
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def parse_effective_end_date(text: str, reference_date: date) -> date | None:
    compact = re.sub(r"\s+", "", text)
    explicit_date = re.search(
        r"(?:到|至|截止到?|截至到?|持续到|直到|结束到)?(?:(20\d{2})年)?"
        r"(\d{1,2}|[一二三四五六七八九十两]{1,3})月"
        r"(\d{1,2}|[一二三四五六七八九十两]{1,3})(?:日|号)?",
        compact,
    )
    if explicit_date:
        year = int(explicit_date.group(1)) if explicit_date.group(1) else reference_date.year
        month = parse_cn_number(explicit_date.group(2))
        day = parse_cn_number(explicit_date.group(3))
        if month and day:
            try:
                parsed = date(year, month, day)
            except ValueError:
                return None
            if not explicit_date.group(1) and parsed < reference_date:
                parsed = date(year + 1, month, day)
            return parsed

    explicit_month_end = re.search(
        r"(?:到|至|截止到?|截至到?|持续到|直到|结束到)?(?:(20\d{2})年)?"
        r"(\d{1,2}|[一二三四五六七八九十两]{1,3})月(?:底|末|底前|末前)",
        compact,
    )
    if explicit_month_end:
        year = int(explicit_month_end.group(1)) if explicit_month_end.group(1) else reference_date.year
        month = parse_cn_number(explicit_month_end.group(2))
        if not month:
            return None
        if not explicit_month_end.group(1) and month < reference_date.month:
            year += 1
        return last_day_of_month(year, month)

    if re.search(r"(?:到|至|截止到?|截至到?|持续到|直到|结束到)?(?:下月|下个月)(?:底|末|底前|末前)", compact):
        month = reference_date.month + 1
        year = reference_date.year
        if month > 12:
            month = 1
            year += 1
        return last_day_of_month(year, month)
    if re.search(r"(?:到|至|截止到?|截至到?|持续到|直到|结束到)?(?:本月)?(?:月底|月末|月底前|月末前)", compact):
        return last_day_of_month(reference_date.year, reference_date.month)
    return None


def parse_duration_days(text: str, reference_time: datetime | date | None = None) -> int | None:
    reference_date = (
        reference_time.date()
        if isinstance(reference_time, datetime)
        else reference_time
        if isinstance(reference_time, date)
        else CURRENT_DATE
    )
    end_date = parse_effective_end_date(text, reference_date)
    if end_date:
        return max(1, (end_date - reference_date).days + 1)

    compact = re.sub(r"\s+", "", text)
    if "半个月" in compact or "半月" in compact:
        return 15
    match = re.search(
        r"(?:为期|持续|周期为期|巡检周期为期)?"
        r"(\d+|[一二三四五六七八九十两]{1,3})(天|日|周|星期|礼拜)",
        compact,
    )
    if match:
        value = parse_cn_number(match.group(1))
        if not value:
            return None
        return value * 7 if match.group(2) in {"周", "星期", "礼拜"} else value

    month_match = re.search(
        r"(?:为期|持续|周期为期|巡检周期为期)"
        r"(\d+|[一二三四五六七八九十两]{1,3})个?月",
        compact,
    )
    if not month_match:
        month_match = re.fullmatch(r"(\d+|[一二三四五六七八九十两]{1,3})个?月", compact)
    if month_match:
        value = parse_cn_number(month_match.group(1))
        return value * 30 if value else None
    return None


def parse_daily_window(text: str) -> dict | None:
    parsed = parse_schedule(text)
    if parsed and parsed.get("mode") == "daily_window":
        return parsed
    fixed_daily = parse_fixed_daily_time(text)
    if fixed_daily:
        return fixed_daily
    if "全天" in text or "24小时" in text or "24 小时" in text:
        return {"mode": "all_day", "start_time": "00:00", "end_time": "24:00", "label": "全天执行"}
    if parsed and parsed.get("mode") == "business_hours":
        return {
            "mode": "business_hours",
            "start_time": "09:00",
            "end_time": "22:00",
            "label": "按门店营业时间（当前 09:00-22:00）",
        }
    if "营业时间" in text or "店时" in text:
        return {
            "mode": "business_hours",
            "start_time": "09:00",
            "end_time": "22:00",
            "label": "按门店营业时间（当前 09:00-22:00）",
        }
    return None


INSPECTION_ACTION_PATTERN = r"(?:看一下|看下|看看|检查一下|检查下|检查|分析|判断|识别)"

ROI_ZONE_LABELS = (
    "广告背景板区域",
    "售后服务区域",
    "收银台区域",
    "接待区域",
    "前台区域",
    "入口区域",
    "门口区域",
    "展厅区域",
    "电视区域",
    "屏幕区域",
    "海报区域",
    "中岛区域",
    "体验区域",
    "维修区域",
    "休息区域",
    "办公区域",
    "仓库区域",
    "售后服务区",
    "广告背景板",
    "售后区域",
    "收银区域",
    "接待区",
    "前台区",
    "入口区",
    "门口区",
    "展厅区",
    "电视区",
    "屏幕区",
    "海报区",
    "中岛区",
    "体验区",
    "维修区",
    "休息区",
    "办公区",
    "仓库区",
    "收银台",
    "售后区",
    "售后",
    "入口",
    "门口",
    "展厅",
    "前台",
)

ROI_ZONE_CANONICAL = {
    "售后": "售后区域",
    "售后区": "售后区域",
    "售后服务区": "售后服务区域",
    "收银台": "收银台区域",
    "收银区域": "收银台区域",
    "接待区": "接待区域",
    "前台": "前台区域",
    "前台区": "前台区域",
    "入口": "入口区域",
    "入口区": "入口区域",
    "门口": "门口区域",
    "门口区": "门口区域",
    "展厅": "展厅区域",
    "展厅区": "展厅区域",
    "电视区": "电视区域",
    "屏幕区": "屏幕区域",
    "海报区": "海报区域",
    "中岛区": "中岛区域",
    "体验区": "体验区域",
    "维修区": "维修区域",
    "休息区": "休息区域",
    "办公区": "办公区域",
    "仓库区": "仓库区域",
}


def inspection_roi_from_text(text: str) -> dict | None:
    if any(word in text for word in ("全画面", "全屏", "整个画面", "不限定区域")):
        return {"mode": "full_frame", "label": "全画面", "polygon": None, "calibration_required": False}
    compact = re.sub(r"\s+", "", text)
    for label in sorted(ROI_ZONE_LABELS, key=len, reverse=True):
        if label not in compact:
            continue
        canonical = ROI_ZONE_CANONICAL.get(label, label)
        return {
            "mode": "named_region",
            "label": canonical,
            "raw": label,
            "polygon": None,
            "calibration_required": False,
        }
    return None


def normalize_inspection_goal(goal: str) -> str:
    normalized = re.sub(r"\s+", " ", goal).strip(" ，,；;。")
    normalized = re.sub(r"^(?:一下|下)", "", normalized).strip(" ，,；;。")
    normalized = re.sub(
        r"^(?:所有|全部|每家|每个|各个|各|当前租户)?(?:门店|店铺|店)(?:的|内|里)?",
        "",
        normalized,
    ).strip(" ，,；;。")
    normalized = re.sub(
        r"^(?:所有|全部|每家|每个|各个|各)?(?:监控|摄像头|镜头)(?:的)?(?:画面|快照|视频)?",
        "",
        normalized,
    ).strip(" ，,；;。")
    return normalized


def inspection_goal_from_text(text: str) -> str | None:
    if is_visual_compliance_request(text):
        return visual_compliance_goal(text)
    explicit_goal_match = re.search(
        r"(?:判断|识别|核验|确认)\s*(.+?)(?:巡检周期|周期|为期|从今天|从明天|持续|到\d{1,2}月|$)",
        text,
        re.S,
    )
    explicit_goal = ""
    if explicit_goal_match and explicit_goal_match.group(1).strip():
        explicit_goal = normalize_inspection_goal(explicit_goal_match.group(1))
    is_floor_cleanliness_goal = "地面" in text and any(word in text for word in ("垃圾", "干净", "纸屑", "杂物"))
    has_multiple_goal_terms = bool(
        explicit_goal
        and is_floor_cleanliness_goal
        and (
            any(word in explicit_goal for word in ("以及", "并且", "同时", "和", "、", "；", ";"))
            or any(word in explicit_goal for word in ("员工", "吃饭", "吃东西", "屏幕", "广告", "灯", "logo", "Logo", "海报", "倒水", "接待"))
        )
    )
    if explicit_goal and has_multiple_goal_terms:
        return explicit_goal[:300]
    if is_floor_cleanliness_goal:
        return "检查门店地面是否存在垃圾或杂物，排除地贴、固定标识、家具和正常堆放物。"
    if explicit_goal:
        return explicit_goal[:300]
    goal_match = re.search(
        rf"{INSPECTION_ACTION_PATTERN}\s*(.+?)(?:巡检周期|周期|为期|从今天|从明天|持续|到\d{{1,2}}月|$)",
        text,
        re.S,
    )
    if not goal_match or not goal_match.group(1).strip():
        return None
    goal = normalize_inspection_goal(goal_match.group(1))
    return goal[:300] if goal else None


def scheduled_inventory(conn, user: dict, text: str, context: dict) -> tuple[dict | None, list[dict]]:
    online = online_agent_for_tenant(conn, user.get("tenant_id"))
    if online:
        bootstrap = online.bootstrap(user)
        orgs = bootstrap.get("orgs") or []
        cameras = bootstrap.get("cameras") or []
    else:
        allowed = sorted(allowed_org_ids(conn, user))
        orgs = rows(conn, f"SELECT * FROM orgs WHERE org_id IN ({','.join('?' for _ in allowed)})", allowed)
        cameras = [serialize_camera(item) for item in rows(conn, f"SELECT * FROM cameras WHERE org_id IN ({','.join('?' for _ in allowed)})", allowed)]
    store_orgs = [item for item in orgs if item.get("org_type") == "store"]
    context_org_id = str(context.get("org_id") or "")
    selected_org = next((item for item in store_orgs if item["org_id"] == context_org_id), None)
    if not selected_org:
        selected_org = next((item for item in store_orgs if item.get("name") and item["name"] in text), None)
    if not selected_org and len(store_orgs) == 1:
        selected_org = store_orgs[0]
    if not selected_org:
        return None, []
    selected_cameras = [
        item for item in cameras
        if item.get("org_id") == selected_org["org_id"] and item.get("stream_status") == "ONLINE"
    ]
    named = [item for item in selected_cameras if item.get("name") and item["name"] in text]
    return selected_org, named or selected_cameras


def is_all_store_scope_request(text: str) -> bool:
    return bool(
        re.search(r"(?:全部|所有|每家|每个|各个|各|全量).{0,12}(?:门店|店铺|店)", text)
        or re.search(r"(?:当前租户|租户下|该租户|这个租户|全租户).{0,12}(?:门店|店铺|店)", text)
    )


def explicit_store_scope_count(text: str) -> int | None:
    """Return an explicitly requested store count, without treating ordinal stores as a scope.

    A request such as ``2家门店`` means that the user expects a multi-store
    result, even though it does not include the broad-scope words such as
    ``所有`` or ``多家``.  ``第2家门店`` is an ordinal and must stay on the
    single-store path.
    """
    match = re.search(
        r"(?<!第)(?P<count>\d+|[零〇一二两三四五六七八九十]+)\s*(?:家|个)\s*(?:门店|店铺|店)",
        str(text or ""),
    )
    if not match:
        return None
    raw = match.group("count")
    if raw.isdigit():
        return int(raw)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if raw == "十":
        return 10
    if "十" in raw:
        tens_raw, ones_raw = raw.split("十", 1)
        tens = digits.get(tens_raw, 1) if tens_raw else 1
        ones = digits.get(ones_raw, 0) if ones_raw else 0
        return tens * 10 + ones
    return digits.get(raw)


def is_multi_store_scope_request(text: str) -> bool:
    explicit_count = explicit_store_scope_count(text)
    return bool(
        is_all_store_scope_request(text)
        or re.search(r"(?:多家|多个).{0,8}(?:门店|店铺|店)", text)
        or (explicit_count is not None and explicit_count > 1)
        or re.search(r"(?:租户下|当前租户|全租户|跨门店|多门店)", text)
        or re.search(r"(?:其他|其它|其余).{0,8}(?:门店|店铺|店)", text)
        or any(alias in text for alias in ("华东区", "华南区", "北京区域", "北京区"))
    )


def is_batch_scheduled_inspection_request(text: str) -> bool:
    return is_scheduled_inspection_request(text) and is_multi_store_scope_request(text)


def is_batch_visual_inspection_request(text: str) -> bool:
    if is_scheduled_inspection_request(text) or not is_multi_store_scope_request(text):
        return False
    write_verbs = ("订阅", "创建", "上线", "开通", "启用", "部署", "配置", "接入")
    capability_terms = ("能力", "检测", "布防", "应用")
    if any(word in text for word in write_verbs) and any(word in text for word in capability_terms):
        return False
    visual_words = (
        "立即",
        "马上",
        "现在",
        "跑一遍",
        "执行一次",
        "检查",
        "巡检",
        "看下",
        "看看",
        "看一下",
        "判断",
        "识别",
        "分析",
        "有没有",
        "是否",
        "有无",
        "画面",
        "快照",
        "摄像头",
    )
    return any(word in text for word in visual_words)


def _store_rows_for_org_ids(conn: sqlite3.Connection, org_ids: list[str], tenant_id: str) -> list[dict]:
    if not org_ids:
        return []
    placeholders = ",".join("?" for _ in org_ids)
    expanded = []
    for org_id in org_ids:
        expanded.extend(descendant_org_ids(conn, org_id))
    store_ids = sorted(set(expanded))
    placeholders = ",".join("?" for _ in store_ids)
    if not store_ids:
        return []
    return rows(
        conn,
        f"""SELECT * FROM orgs
            WHERE tenant_id=? AND org_type='store' AND org_id IN ({placeholders})
            ORDER BY name""",
        [tenant_id, *store_ids],
    )


def _tenant_integration_store_rows(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    integration = one(
        conn,
        "SELECT * FROM tenant_integrations WHERE tenant_code=? AND status='CONNECTED'",
        (tenant_id,),
    )
    if not integration:
        return []
    return [
        {
            "org_id": row["org_id"],
            "tenant_id": tenant_id,
            "parent_id": row["parent_id"],
            "name": row["name"],
            "org_type": row["org_type"],
            "status": row["status"],
            "camera_count": row["camera_count"],
            "synced_at": row["synced_at"],
            "source": "tenant_integration",
        }
        for row in rows(
            conn,
            """
            SELECT * FROM tenant_integration_stores
            WHERE integration_id=? AND org_type='store'
            ORDER BY name
            """,
            (integration["integration_id"],),
        )
    ]


def _online_inventory_store_rows(agent: OnlineInspectionAgent) -> list[dict]:
    orgs, fields = agent._organization_inventory()
    return [
        {
            "org_id": field["org_id"],
            "tenant_id": agent.tenant_code,
            "parent_id": field.get("parent_id"),
            "name": field.get("name") or field["org_id"],
            "org_type": "store",
            "status": field.get("status") or "UNKNOWN",
            "camera_count": field.get("camera_count"),
            "synced_at": now_iso(),
            "source": "deepvision_online",
            "_online_field": field,
        }
        for field in fields
    ]


def _allowed_online_store_ids(user: dict) -> set[str] | None:
    configured = json_loads(user["allowed_org_ids"], [])
    if "*" in configured:
        return None
    return {str(item) for item in configured}


def _select_online_scope_stores(stores: list[dict], text: str, context: dict) -> list[dict]:
    if not stores:
        return []
    all_scope = is_all_store_scope_request(text)
    multi_scope = is_multi_store_scope_request(text)
    context_org_id = context.get("org_id")
    continuation = context.get("_conversation_continuation") if isinstance(context.get("_conversation_continuation"), dict) else {}
    active_scope = continuation.get("active_task_scope") if isinstance(continuation.get("active_task_scope"), dict) else {}
    active_ids = {str(item) for item in active_scope.get("org_ids") or [] if item}
    scope_history = continuation.get("scope_history") if isinstance(continuation.get("scope_history"), list) else []
    previous_scope = next(
        (item for item in reversed(scope_history) if isinstance(item, dict) and item.get("org_ids")),
        {},
    )
    previous_ids = {str(item) for item in previous_scope.get("org_ids") or [] if item}
    operation = str(continuation.get("scope_operation") or "KEEP_SCOPE")
    matched = []
    for store in stores:
        name = str(store.get("name") or "")
        if name and (name in text or text in name):
            matched.append(store)
    if all_scope:
        return stores
    if re.search(r"(?:其他|其它|其余).{0,8}(?:门店|店铺|店)", text):
        excluded = active_ids or ({str(context_org_id)} if context_org_id else set())
        return [store for store in stores if str(store.get("org_id") or "") not in excluded]
    if operation == "COMPARE_SCOPE":
        target_ids = active_ids | previous_ids
        selected = [store for store in stores if str(store.get("org_id") or "") in target_ids]
        if selected:
            return selected
    if operation == "PREVIOUS_SCOPE" and previous_ids:
        return [store for store in stores if str(store.get("org_id") or "") in previous_ids]
    if matched:
        return matched
    # A numeric scope such as “2家门店” is an explicit cross-store request.
    # Do not let the page's current-store context silently collapse it back to
    # one store before the batch planner can produce its per-store child runs.
    if multi_scope:
        return stores
    if context_org_id:
        contextual = [store for store in stores if store.get("org_id") == context_org_id]
        if contextual:
            return contextual
    return []


def _resolve_online_batch_store_scope(conn, user: dict, text: str, context: dict) -> dict | None:
    if not is_multi_store_scope_request(text):
        return None
    try:
        agent = online_agent_for_tenant(conn, user.get("tenant_id"))
    except ApiError:
        raise
    if not agent:
        return None
    stores = _tenant_integration_store_rows(conn, user["tenant_id"])
    if not stores:
        if not hasattr(agent, "_organization_inventory"):
            return None
        stores = _online_inventory_store_rows(agent)
    selected = _select_online_scope_stores(stores, text, context)
    allowed = allowed_org_ids(conn, user)
    before_count = len(selected)
    selected = [store for store in selected if store.get("org_id") in allowed]
    permission_filtered_count = max(0, before_count - len(selected))

    exclude_tail = ""
    for marker in ("排除", "剔除", "除了", "除", "不包含", "不要"):
        if marker in text:
            exclude_tail = f"{exclude_tail} {text.split(marker, 1)[1]}"
    excluded = [store for store in selected if store.get("name") and store["name"] in exclude_tail]
    excluded_ids = {store["org_id"] for store in excluded}
    selected = [store for store in selected if store["org_id"] not in excluded_ids]

    store_tasks = []
    online_camera_count = 0
    offline_camera_count = 0
    total_camera_count = 0
    for store in selected:
        field = dict(store.get("_online_field") or store)
        field.setdefault("org_id", store["org_id"])
        field.setdefault("name", store["name"])
        try:
            camera_rows = agent._camera_rows(field)
            camera_error = None
        except OnlineAgentError as exc:
            camera_rows = []
            camera_error = exc.message
        serialized_cameras = []
        for camera in camera_rows:
            item = dict(camera)
            item.setdefault("tenant_id", user["tenant_id"])
            item.setdefault("org_id", store["org_id"])
            serialized_cameras.append(item)
        online_cameras = [item for item in serialized_cameras if item.get("stream_status") == "ONLINE"]
        offline_cameras = [item for item in serialized_cameras if item.get("stream_status") != "ONLINE"]
        total_camera_count += len(serialized_cameras)
        online_camera_count += len(online_cameras)
        offline_camera_count += len(offline_cameras)
        task = {
            "org_id": store["org_id"],
            "org_name": store["name"],
            "camera_ids": [item["camera_id"] for item in online_cameras if item.get("camera_id")],
            "camera_names": [item["name"] for item in online_cameras if item.get("name")],
            "online_camera_count": len(online_cameras),
            "offline_camera_count": len(offline_cameras),
            "total_camera_count": len(serialized_cameras),
            "status": "READY" if online_cameras else "NO_ONLINE_CAMERA",
        }
        if camera_error:
            task["status"] = "CAMERA_LOAD_FAILED"
            task["failure_code"] = camera_error
        store_tasks.append(task)
    return {
        "stores": [{key: value for key, value in store.items() if not key.startswith("_")} for store in selected],
        "store_tasks": store_tasks,
        "excluded": [{key: value for key, value in store.items() if not key.startswith("_")} for store in excluded],
        "permission_filtered_count": permission_filtered_count,
        "online_camera_count": online_camera_count,
        "offline_camera_count": offline_camera_count,
        "total_camera_count": total_camera_count,
    }


def resolve_batch_store_scope(conn, user: dict, text: str, context: dict) -> tuple[dict, dict | None]:
    online_scope = _resolve_online_batch_store_scope(conn, user, text, context)
    if online_scope is not None:
        return online_scope, None
    context_org_id = context.get("org_id")
    continuation = context.get("_conversation_continuation") if isinstance(context.get("_conversation_continuation"), dict) else {}
    active_scope = continuation.get("active_task_scope") if isinstance(continuation.get("active_task_scope"), dict) else {}
    active_ids = {str(item) for item in active_scope.get("org_ids") or [] if item}
    scope_history = continuation.get("scope_history") if isinstance(continuation.get("scope_history"), list) else []
    previous_scope = next(
        (item for item in reversed(scope_history) if isinstance(item, dict) and item.get("org_ids")),
        {},
    )
    previous_ids = {str(item) for item in previous_scope.get("org_ids") or [] if item}
    operation = str(continuation.get("scope_operation") or "KEEP_SCOPE")
    if operation == "COMPARE_SCOPE" and (active_ids or previous_ids):
        org_ids, ambiguous = sorted(active_ids | previous_ids), None
    elif operation == "PREVIOUS_SCOPE" and previous_ids:
        org_ids, ambiguous = sorted(previous_ids), None
    else:
        org_ids, ambiguous = find_org_candidates(conn, text, None if is_multi_store_scope_request(text) else context_org_id, user)
    if ambiguous:
        return {"stores": [], "excluded": [], "permission_filtered_count": 0}, ambiguous
    if not org_ids and is_multi_store_scope_request(text):
        configured = json_loads(user["allowed_org_ids"], [])
        if "*" in configured:
            org_ids = [row["org_id"] for row in rows(conn, "SELECT org_id FROM orgs WHERE tenant_id=?", (user["tenant_id"],))]
        else:
            org_ids = sorted(allowed_org_ids(conn, user))
    if not org_ids and context_org_id:
        org_ids = [context_org_id]
    stores = _store_rows_for_org_ids(conn, org_ids, user["tenant_id"])
    allowed = allowed_org_ids(conn, user)
    before_count = len(stores)
    stores = [store for store in stores if store["org_id"] in allowed]
    permission_filtered_count = max(0, before_count - len(stores))
    if re.search(r"(?:其他|其它|其余).{0,8}(?:门店|店铺|店)", text):
        excluded_prior_ids = active_ids or ({str(context_org_id)} if context_org_id else set())
        stores = [store for store in stores if store["org_id"] not in excluded_prior_ids]
    exclude_tail = ""
    for marker in ("排除", "剔除", "除了", "除", "不包含", "不要"):
        if marker in text:
            exclude_tail = f"{exclude_tail} {text.split(marker, 1)[1]}"
    excluded = [store for store in stores if store["name"] and store["name"] in exclude_tail]
    excluded_ids = {store["org_id"] for store in excluded}
    stores = [store for store in stores if store["org_id"] not in excluded_ids]
    store_ids = [store["org_id"] for store in stores]
    if store_ids:
        placeholders = ",".join("?" for _ in store_ids)
        camera_rows = rows(
            conn,
            f"""SELECT * FROM cameras
                WHERE tenant_id=? AND org_id IN ({placeholders})
                ORDER BY org_id, name""",
            [user["tenant_id"], *store_ids],
        )
    else:
        camera_rows = []
    cameras_by_store: dict[str, list[dict]] = {store["org_id"]: [] for store in stores}
    for camera in camera_rows:
        cameras_by_store.setdefault(camera["org_id"], []).append(serialize_camera(camera))
    store_tasks = []
    online_camera_count = 0
    offline_camera_count = 0
    total_camera_count = 0
    for store in stores:
        store_cameras = cameras_by_store.get(store["org_id"], [])
        online_cameras = [item for item in store_cameras if item.get("stream_status") == "ONLINE"]
        offline_cameras = [item for item in store_cameras if item.get("stream_status") != "ONLINE"]
        total_camera_count += len(store_cameras)
        online_camera_count += len(online_cameras)
        offline_camera_count += len(offline_cameras)
        store_tasks.append(
            {
                "org_id": store["org_id"],
                "org_name": store["name"],
                "camera_ids": [item["camera_id"] for item in online_cameras],
                "camera_names": [item["name"] for item in online_cameras],
                "online_camera_count": len(online_cameras),
                "offline_camera_count": len(offline_cameras),
                "total_camera_count": len(store_cameras),
                "status": "READY" if online_cameras else "NO_ONLINE_CAMERA",
            }
        )
    return (
        {
            "stores": [dict(store) for store in stores],
            "store_tasks": store_tasks,
            "excluded": [dict(store) for store in excluded],
            "permission_filtered_count": permission_filtered_count,
            "online_camera_count": online_camera_count,
            "offline_camera_count": offline_camera_count,
            "total_camera_count": total_camera_count,
        },
        None,
    )


def estimate_scheduled_runs(start_at: datetime, end_at: datetime, interval_minutes: int, daily_window: dict) -> int:
    if daily_window.get("mode") == "fixed_daily":
        fixed_hour, fixed_minute = (int(item) for item in daily_window.get("fixed_time", "09:00").split(":"))
        candidate = start_at.replace(hour=fixed_hour, minute=fixed_minute, second=0, microsecond=0)
        if candidate < start_at:
            candidate += timedelta(days=1)
        count = 0
        while candidate < end_at and count < 10000:
            count += 1
            candidate += timedelta(days=1)
        return count
    count = 1 if start_at < end_at else 0
    current = start_at
    while current < end_at and count < 10000:
        candidate = current + timedelta(minutes=interval_minutes)
        if daily_window["mode"] != "all_day":
            start_hour, start_minute = (int(item) for item in daily_window["start_time"].split(":"))
            end_hour, end_minute = (int(item) for item in daily_window["end_time"].split(":"))
            day_start = candidate.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
            day_end = candidate.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
            if candidate < day_start:
                candidate = day_start
            elif candidate > day_end:
                candidate = day_start + timedelta(days=1)
        if candidate >= end_at:
            break
        count += 1
        current = candidate
    return count


def build_scheduled_inspection_plan(
    conn,
    user: dict,
    conversation_id: str,
    text: str,
    context: dict,
    previous_plan: dict | None = None,
) -> tuple[dict, str]:
    if not role_can_create_subscription(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN, {"intent": "CREATE_SCHEDULED_INSPECTION"})
    prior_text = str((previous_plan or {}).get("slots", {}).get("request_text") or "")
    full_text = f"{prior_text}\n{text}".strip() if prior_text else text
    now = datetime.now(CN_TZ).replace(microsecond=0)
    interval_minutes = parse_interval_minutes(full_text)
    duration_days = parse_duration_days(full_text, now)
    daily_window = parse_daily_window(full_text)
    force_first_run = bool(daily_window and daily_window.get("mode") == "fixed_daily" and fixed_daily_first_run_requested(full_text))
    inspection_goal = str(context.get("effective_visual_query") or "").strip() or inspection_goal_from_text(full_text)
    roi = inspection_roi_from_text(full_text)
    knowledge_hits = retrieve_agent_knowledge(conn, user, full_text)
    org, cameras = scheduled_inventory(conn, user, full_text, context)
    start_at = now
    end_at = now + timedelta(days=duration_days or 0)
    missing = []
    if not org:
        missing.append("org_scope")
    if not cameras:
        missing.append("camera_ids")
    if not interval_minutes:
        missing.append("interval")
    if not duration_days:
        missing.append("effective_time_range")
    if not daily_window:
        missing.append("daily_window")
    if not inspection_goal:
        missing.append("inspection_goal")
    estimated_runs = estimate_scheduled_runs(start_at, end_at, interval_minutes, daily_window) if not missing else 0
    schedule = {
        "mode": "interval",
        "interval_minutes": interval_minutes,
        "daily_window": daily_window,
        "timezone": "Asia/Shanghai",
        "estimated_runs": estimated_runs,
        "label": (
            f"{daily_window['label']} · 持续 {duration_days} 天 · 预计 {estimated_runs} 次"
            if daily_window and daily_window.get("mode") == "fixed_daily" and duration_days
            else
            f"每 {interval_minutes // 60} 小时 · {daily_window['label']} · 持续 {duration_days} 天 · 预计 {estimated_runs} 次"
            if interval_minutes and interval_minutes % 60 == 0 and daily_window and duration_days
            else f"每 {interval_minutes} 分钟 · {daily_window['label']} · 持续 {duration_days} 天 · 预计 {estimated_runs} 次"
            if interval_minutes and daily_window and duration_days
            else "待补充执行周期"
        ),
    }
    slots = {
        "request_text": full_text,
        "org_scope": {
            "resolved_ids": [org["org_id"]] if org else [],
            "resolved_names": [org["name"]] if org else [],
            "store_count": 1 if org else 0,
        },
        "capability": {"name": "周期快照 AI 巡检", "raw": inspection_goal},
        "camera_scope": {
            "resolved_ids": [item["camera_id"] for item in cameras],
            "resolved_names": [item["name"] for item in cameras],
        },
        "inspection_goal": inspection_goal,
        "schedule": schedule,
        "time_range": {
            "raw": f"从今天开始，为期 {duration_days} 天" if duration_days else None,
            "start": start_at.isoformat(timespec="seconds") if duration_days else None,
            "end": end_at.isoformat(timespec="seconds") if duration_days else None,
        },
        "thresholds": inspection_thresholds(full_text, knowledge_hits),
        "roi": roi or {"mode": "full_frame", "label": "全画面，重点判断地面区域"},
        "missing_slots": missing,
    }
    if knowledge_hits:
        slots["knowledge_hits"] = knowledge_hits
    if is_visual_compliance_request(full_text):
        slots["capability"] = {"name": VISUAL_COMPLIANCE_NAME, "raw": inspection_goal}
        slots["roi"] = roi or {"mode": "full_frame", "label": "全画面，按对象包规则聚焦推荐点位"}
        slots["visual_compliance"] = slots["thresholds"]["visual_compliance"]
    status = "NEED_CLARIFICATION" if missing else "READY_FOR_CONFIRM"
    action = {
        "action_id": "scheduled-create",
        "tool": "scheduler.inspection.create",
        "params": {
            "org_id": org["org_id"] if org else None,
            "org_name": org["name"] if org else None,
            "camera_ids": [item["camera_id"] for item in cameras],
            "camera_names": [item["name"] for item in cameras],
            "inspection_goal": inspection_goal,
            "schedule": schedule,
            "start_at": slots["time_range"]["start"],
            "end_at": slots["time_range"]["end"],
            "thresholds": slots["thresholds"],
            "roi": slots["roi"],
            "force_first_run": force_first_run,
        },
    }
    plan = create_plan(
        conn,
        user,
        conversation_id,
        "CREATE_SCHEDULED_INSPECTION",
        "HIGH_WRITE",
        status,
        slots,
        [action] if not missing else [],
        ["permission.check", "camera.online_check", "schedule.validate", "quota.cost_check"],
        True,
        f"为{org['name'] if org else '指定门店'}创建周期快照 AI 巡检",
        {
            "passed": not missing,
            "missing_slots": missing,
            "warnings": (
                [
                    "固定时刻任务将等待首个指定时刻执行；结果会同时展示计划时间、实际开始时间和首帧时间。"
                    if not force_first_run
                    else "已识别到“立即/先执行”指令，将先执行一轮，再按固定时刻运行。"
                ]
                if not missing and daily_window and daily_window.get("mode") == "fixed_daily"
                else ["任务确认后将立即执行首轮，后续按固定间隔自动运行。"] if not missing else []
            ),
        },
        hashlib.sha256(f"scheduled|{user['user_id']}|{full_text}".encode("utf-8")).hexdigest(),
    )
    if previous_plan:
        conn.execute("UPDATE plans SET status='CANCELLED', updated_at=? WHERE plan_id=?", (now_iso(), previous_plan["plan_id"]))
    if missing:
        questions = {
            "org_scope": "请指定需要巡检的门店。",
            "camera_ids": "当前门店没有可用在线摄像头，请先检查设备状态。",
            "interval": "请说明巡检间隔，例如“每隔 3 小时”。",
            "effective_time_range": "请说明任务持续时间，例如“为期一周”“到7月底”或“到7月31日”。",
            "daily_window": "这项任务按门店营业时间执行，还是全天执行？",
            "inspection_goal": "请说明每次快照需要判断什么问题。",
        }
        return plan, f"我已保留周期巡检需求。{questions[missing[0]]}"
    return plan, (
        f"已生成周期巡检计划：{schedule['label']}，覆盖 {len(cameras)} 路在线摄像头。"
        f"{inspection_knowledge_summary(knowledge_hits)}"
        + (
            "确认后将等待首个指定时刻执行；结果会标明计划时间、实际开始时间和首帧时间。"
            if daily_window and daily_window.get("mode") == "fixed_daily" and not force_first_run
            else "确认后立即执行首轮，之后每次分析都会把模型实际使用的快照回写到本对话。"
        )
    )


def build_batch_scheduled_inspection_plan(
    conn,
    user: dict,
    conversation_id: str,
    text: str,
    context: dict,
    previous_plan: dict | None = None,
) -> tuple[dict, str]:
    if not role_can_create_batch_inspection(user["role"]):
        raise ApiError(
            "PERMISSION_DENIED",
            HTTPStatus.FORBIDDEN,
            {"intent": "BATCH_SCHEDULED_INSPECTION_CREATE", "message": "当前角色不能创建跨门店巡检任务"},
        )
    prior_text = str((previous_plan or {}).get("slots", {}).get("request_text") or "")
    full_text = f"{prior_text}\n{text}".strip() if prior_text else text
    now = datetime.now(CN_TZ).replace(microsecond=0)
    interval_minutes = parse_interval_minutes(full_text)
    duration_days = parse_duration_days(full_text, now)
    daily_window = parse_daily_window(full_text)
    force_first_run = bool(daily_window and daily_window.get("mode") == "fixed_daily" and fixed_daily_first_run_requested(full_text))
    execution_mode = (
        "scheduled"
        if daily_window and daily_window.get("mode") == "fixed_daily" and not force_first_run
        else "scheduled_with_first_run"
    )
    inspection_goal = str(context.get("effective_visual_query") or "").strip() or inspection_goal_from_text(full_text)
    roi = inspection_roi_from_text(full_text)
    knowledge_hits = retrieve_agent_knowledge(conn, user, full_text)
    scope, ambiguous = resolve_batch_store_scope(conn, user, full_text, context)
    if ambiguous:
        raise ApiError("ENTITY_AMBIGUOUS", HTTPStatus.CONFLICT, ambiguous)
    store_tasks = scope.get("store_tasks") or []
    executable_store_tasks = [item for item in store_tasks if item.get("camera_ids")]
    start_at = now
    end_at = now + timedelta(days=duration_days or 0)
    missing = []
    if not store_tasks:
        missing.append("org_scope")
    if not executable_store_tasks:
        missing.append("camera_ids")
    if not interval_minutes:
        missing.append("interval")
    if not duration_days:
        missing.append("effective_time_range")
    if not daily_window:
        missing.append("daily_window")
    if not inspection_goal:
        missing.append("inspection_goal")
    estimated_runs_per_store = estimate_scheduled_runs(start_at, end_at, interval_minutes, daily_window) if not missing else 0
    store_count = len(store_tasks)
    online_camera_count = int(scope.get("online_camera_count") or 0)
    offline_camera_count = int(scope.get("offline_camera_count") or 0)
    total_camera_count = int(scope.get("total_camera_count") or 0)
    estimated_model_calls = estimated_runs_per_store * online_camera_count
    schedule = {
        "mode": "interval",
        "interval_minutes": interval_minutes,
        "daily_window": daily_window,
        "timezone": "Asia/Shanghai",
        "estimated_runs": estimated_runs_per_store,
        "estimated_total_store_runs": estimated_runs_per_store * max(1, store_count) if estimated_runs_per_store else 0,
        "estimated_model_calls": estimated_model_calls,
        "label": (
            f"{daily_window['label']} · 持续 {duration_days} 天 · 每店预计 {estimated_runs_per_store} 次"
            if daily_window and daily_window.get("mode") == "fixed_daily" and duration_days
            else
            f"每 {interval_minutes // 60} 小时 · {daily_window['label']} · 持续 {duration_days} 天 · 每店预计 {estimated_runs_per_store} 次"
            if interval_minutes and interval_minutes % 60 == 0 and daily_window and duration_days
            else f"每 {interval_minutes} 分钟 · {daily_window['label']} · 持续 {duration_days} 天 · 每店预计 {estimated_runs_per_store} 次"
            if interval_minutes and daily_window and duration_days
            else "待补充执行周期"
        ),
    }
    thresholds = inspection_thresholds(full_text, knowledge_hits)
    slots = {
        "request_text": full_text,
        "batch": {
            "enabled": True,
            "execution_mode": execution_mode,
            "store_count": store_count,
            "executable_store_count": len(executable_store_tasks),
            "skipped_store_count": max(0, store_count - len(executable_store_tasks)),
            "estimated_model_calls": estimated_model_calls,
            "strong_confirm_required": store_count > 10 or online_camera_count > 50 or estimated_model_calls > 500,
            "failure_policy": "PARTIAL_SUCCESS_WITH_STORE_LEVEL_RETRY",
        },
        "org_scope": {
            "scope_type": "multi_store",
            "raw": full_text,
            "resolved_ids": [item["org_id"] for item in scope.get("stores", [])],
            "resolved_names": [item["name"] for item in scope.get("stores", [])],
            "store_count": store_count,
            "excluded_ids": [item["org_id"] for item in scope.get("excluded", [])],
            "excluded_names": [item["name"] for item in scope.get("excluded", [])],
            "permission_filtered_count": scope.get("permission_filtered_count", 0),
        },
        "capability": {"name": "多门店周期快照 AI 巡检", "raw": inspection_goal},
        "camera_scope": {
            "resolved_ids": [camera_id for item in executable_store_tasks for camera_id in item.get("camera_ids", [])],
            "resolved_names": [name for item in executable_store_tasks for name in item.get("camera_names", [])],
            "store_tasks": store_tasks,
            "online_camera_count": online_camera_count,
            "offline_camera_count": offline_camera_count,
            "total_camera_count": total_camera_count,
        },
        "inspection_goal": inspection_goal,
        "schedule": schedule,
        "time_range": {
            "raw": f"从今天开始，为期 {duration_days} 天" if duration_days else None,
            "start": start_at.isoformat(timespec="seconds") if duration_days else None,
            "end": end_at.isoformat(timespec="seconds") if duration_days else None,
        },
        "thresholds": thresholds,
        "roi": roi or {"mode": "full_frame", "label": "全画面，按每家门店可用镜头分别判断"},
        "missing_slots": missing,
    }
    if knowledge_hits:
        slots["knowledge_hits"] = knowledge_hits
    if is_visual_compliance_request(full_text):
        slots["capability"] = {"name": f"多门店{VISUAL_COMPLIANCE_NAME}", "raw": inspection_goal}
        slots["roi"] = roi or {"mode": "full_frame", "label": "全画面，按对象包规则逐店推荐点位"}
        slots["visual_compliance"] = thresholds["visual_compliance"]
    status = "NEED_CLARIFICATION" if missing else "READY_FOR_CONFIRM"
    action = {
        "action_id": "batch-scheduled-create",
        "tool": "batch_inspection.create",
        "params": {
            "store_tasks": store_tasks,
            "inspection_goal": inspection_goal,
            "schedule": schedule,
            "start_at": slots["time_range"]["start"],
            "end_at": slots["time_range"]["end"],
            "thresholds": thresholds,
            "roi": slots["roi"],
            "failure_policy": "PARTIAL_SUCCESS_WITH_STORE_LEVEL_RETRY",
            "execution_mode": execution_mode,
            "force_first_run": force_first_run,
        },
    }
    warnings = []
    if store_count and len(executable_store_tasks) < store_count:
        warnings.append(f"{store_count - len(executable_store_tasks)} 家门店没有在线镜头，将作为跳过项保留在批次详情中。")
    if daily_window and daily_window.get("mode") == "fixed_daily" and not force_first_run:
        warnings.append("固定时刻子任务将等待首个指定时刻执行，并记录计划、实际开始和首帧时间。")
    if slots["batch"]["strong_confirm_required"]:
        warnings.append("本次覆盖门店或镜头较多，确认后会按门店拆分创建子任务，请核对范围。")
    plan = create_plan(
        conn,
        user,
        conversation_id,
        "BATCH_SCHEDULED_INSPECTION_CREATE",
        "HIGH_WRITE",
        status,
        slots,
        [action] if not missing else [],
        [
            "permission.scope_check",
            "entity.store_resolve",
            "camera.online_check",
            "schedule.validate",
            "quota.cost_check",
            "idempotency.check",
            "batch.partial_success",
        ],
        True,
        f"为 {store_count or '多家'} 家门店创建周期快照 AI 巡检",
        {
            "passed": not missing,
            "missing_slots": missing,
            "warnings": warnings if not missing else [],
            "estimated_model_calls": estimated_model_calls,
        },
        hashlib.sha256(f"batch-scheduled|{user['user_id']}|{full_text}".encode("utf-8")).hexdigest(),
    )
    if previous_plan:
        conn.execute("UPDATE plans SET status='CANCELLED', updated_at=? WHERE plan_id=?", (now_iso(), previous_plan["plan_id"]))
    if missing:
        questions = {
            "org_scope": "请说明要覆盖哪些门店，例如“当前租户所有门店”“华东区所有门店”或直接列出门店名称。",
            "camera_ids": "当前范围没有可用在线摄像头，请先切换范围或检查设备状态。",
            "interval": "请说明巡检间隔，例如“每隔 3 小时”。",
            "effective_time_range": "请说明任务持续时间，例如“为期一周”“为期 14 天”或“到7月底”。",
            "daily_window": "这项任务按门店营业时间执行、全天执行，还是指定每天的时间段？",
            "inspection_goal": "请说明每次快照需要判断什么问题。",
        }
        return plan, f"我已保留多门店巡检需求。{questions[missing[0]]}"
    return plan, (
        f"已生成多门店周期巡检计划：覆盖 {store_count} 家门店、{online_camera_count} 路在线镜头，"
        f"{schedule['label']}，预计模型分析 {estimated_model_calls} 张快照。{inspection_knowledge_summary(knowledge_hits)}"
        "确认后会创建批量父任务和门店子任务，支持部分成功与逐店追溯。"
    )


def build_batch_visual_inspection_plan(
    conn,
    user: dict,
    conversation_id: str,
    text: str,
    context: dict,
    previous_plan: dict | None = None,
) -> tuple[dict, str]:
    if not role_can_create_batch_inspection(user["role"]):
        raise ApiError(
            "PERMISSION_DENIED",
            HTTPStatus.FORBIDDEN,
            {"intent": "BATCH_INSPECTION_EXECUTE", "message": "当前角色不能创建跨门店即时巡检任务"},
        )
    prior_text = str((previous_plan or {}).get("slots", {}).get("request_text") or "")
    full_text = f"{prior_text}\n{text}".strip() if prior_text else text
    now = datetime.now(CN_TZ).replace(microsecond=0)
    inspection_goal = str(context.get("effective_visual_query") or "").strip() or inspection_goal_from_text(full_text)
    roi = inspection_roi_from_text(full_text)
    knowledge_hits = retrieve_agent_knowledge(conn, user, full_text)
    scope, ambiguous = resolve_batch_store_scope(conn, user, full_text, context)
    if ambiguous:
        raise ApiError("ENTITY_AMBIGUOUS", HTTPStatus.CONFLICT, ambiguous)
    store_tasks = scope.get("store_tasks") or []
    executable_store_tasks = [item for item in store_tasks if item.get("camera_ids")]
    store_count = len(store_tasks)
    online_camera_count = int(scope.get("online_camera_count") or 0)
    offline_camera_count = int(scope.get("offline_camera_count") or 0)
    total_camera_count = int(scope.get("total_camera_count") or 0)
    missing = []
    if not store_tasks:
        missing.append("org_scope")
    if not executable_store_tasks:
        missing.append("camera_ids")
    if not inspection_goal:
        missing.append("inspection_goal")
    schedule = {
        "mode": "one_off",
        "label": "立即执行一次",
        "interval_minutes": 0,
        "timezone": "Asia/Shanghai",
        "estimated_runs": 1 if not missing else 0,
        "estimated_total_store_runs": len(executable_store_tasks) if not missing else 0,
        "estimated_model_calls": online_camera_count if not missing else 0,
    }
    thresholds = inspection_thresholds(full_text, knowledge_hits)
    slots = {
        "request_text": full_text,
        "batch": {
            "enabled": True,
            "execution_mode": "immediate",
            "store_count": store_count,
            "executable_store_count": len(executable_store_tasks),
            "skipped_store_count": max(0, store_count - len(executable_store_tasks)),
            "estimated_model_calls": schedule["estimated_model_calls"],
            "strong_confirm_required": store_count > 10 or online_camera_count > 50,
            "failure_policy": "PARTIAL_SUCCESS_WITH_STORE_LEVEL_RETRY",
        },
        "org_scope": {
            "scope_type": "multi_store",
            "raw": full_text,
            "resolved_ids": [item["org_id"] for item in scope.get("stores", [])],
            "resolved_names": [item["name"] for item in scope.get("stores", [])],
            "store_count": store_count,
            "excluded_ids": [item["org_id"] for item in scope.get("excluded", [])],
            "excluded_names": [item["name"] for item in scope.get("excluded", [])],
            "permission_filtered_count": scope.get("permission_filtered_count", 0),
        },
        "capability": {"name": "多门店即时 AI 巡检", "raw": inspection_goal},
        "camera_scope": {
            "resolved_ids": [camera_id for item in executable_store_tasks for camera_id in item.get("camera_ids", [])],
            "resolved_names": [name for item in executable_store_tasks for name in item.get("camera_names", [])],
            "store_tasks": store_tasks,
            "online_camera_count": online_camera_count,
            "offline_camera_count": offline_camera_count,
            "total_camera_count": total_camera_count,
        },
        "inspection_goal": inspection_goal,
        "schedule": schedule,
        "time_range": {
            "raw": "立即执行一次",
            "start": now.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds"),
        },
        "thresholds": thresholds,
        "roi": roi or {"mode": "full_frame", "label": "全画面，按每家门店可用镜头分别判断"},
        "missing_slots": missing,
    }
    if knowledge_hits:
        slots["knowledge_hits"] = knowledge_hits
    if is_visual_compliance_request(full_text):
        slots["capability"] = {"name": f"多门店{VISUAL_COMPLIANCE_NAME}", "raw": inspection_goal}
        slots["roi"] = roi or {"mode": "full_frame", "label": "全画面，按对象包规则逐店推荐点位"}
        slots["visual_compliance"] = thresholds["visual_compliance"]
    status = "NEED_CLARIFICATION" if missing else "READY_FOR_CONFIRM"
    action = {
        "action_id": "batch-inspection-execute",
        "tool": "batch_inspection.execute",
        "params": {
            "store_tasks": store_tasks,
            "inspection_goal": inspection_goal,
            "schedule": schedule,
            "start_at": slots["time_range"]["start"],
            "end_at": slots["time_range"]["end"],
            "thresholds": thresholds,
            "roi": slots["roi"],
            "failure_policy": "PARTIAL_SUCCESS_WITH_STORE_LEVEL_RETRY",
            "execution_mode": "immediate",
        },
    }
    warnings = []
    if store_count and len(executable_store_tasks) < store_count:
        warnings.append(f"{store_count - len(executable_store_tasks)} 家门店没有在线镜头，将作为跳过项保留在批次详情中。")
    if slots["batch"]["strong_confirm_required"]:
        warnings.append("本次覆盖门店或镜头较多，确认后会立即抓图和分析，请核对范围。")
    plan = create_plan(
        conn,
        user,
        conversation_id,
        "BATCH_INSPECTION_EXECUTE",
        "HIGH_WRITE",
        status,
        slots,
        [action] if not missing else [],
        [
            "permission.scope_check",
            "entity.store_resolve",
            "camera.online_check",
            "paas.media.snapshot",
            "evidence.archive",
            "vlm.image.inspect",
            "idempotency.check",
            "batch.partial_success",
        ],
        True,
        f"为 {store_count or '多家'} 家门店执行即时 AI 巡检",
        {
            "passed": not missing,
            "missing_slots": missing,
            "warnings": warnings if not missing else [],
            "estimated_model_calls": schedule["estimated_model_calls"],
        },
        hashlib.sha256(f"batch-visual|{user['user_id']}|{full_text}".encode("utf-8")).hexdigest(),
    )
    if previous_plan:
        conn.execute("UPDATE plans SET status='CANCELLED', updated_at=? WHERE plan_id=?", (now_iso(), previous_plan["plan_id"]))
    if missing:
        questions = {
            "org_scope": "请说明要覆盖哪些门店，例如“当前租户所有门店”“华东区所有门店”或直接列出门店名称。",
            "camera_ids": "当前范围没有可用在线摄像头，请先切换范围或检查设备状态。",
            "inspection_goal": "请说明这次即时巡检需要判断什么问题。",
        }
        return plan, f"我已保留多门店即时巡检需求。{questions[missing[0]]}"
    return plan, (
        f"已生成多门店即时巡检计划：覆盖 {store_count} 家门店、{online_camera_count} 路在线镜头。"
        f"{inspection_knowledge_summary(knowledge_hits)}"
        "确认后将立即抓取快照、调用视觉模型分析并归档证据，支持部分成功与逐店追溯。"
    )


def build_subscription_plan(conn, user, conversation_id: str, text: str, context: dict) -> tuple[dict | None, str, dict]:
    if not role_can_create_subscription(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN, {"intent": "SUBSCRIPTION_CREATE"})
    context_org_id = context.get("org_id")
    org_ids, ambiguous = find_org_candidates(conn, text, context_org_id, user)
    if ambiguous:
        return None, f"“{ambiguous['raw']}”匹配到多个门店，请先选择一个：{', '.join(c['name'] for c in ambiguous['candidates'])}。", {"error_code": "ENTITY_AMBIGUOUS", "ambiguous": ambiguous}
    capability = find_capability(conn, text)
    time_range = parse_time_range(text)
    schedule = parse_schedule(text)
    missing = []
    if not org_ids:
        missing.append("org_scope")
    if not capability:
        missing.append("capability")
    if not time_range:
        missing.append("time_range")
    if not schedule:
        missing.append("schedule")
    slots = {
        "org_scope": {"raw": "已解析组织" if org_ids else None, "resolved_ids": org_ids},
        "capability": {"raw": capability["name"] if capability else None, "resolved_capability_id": capability["capability_id"] if capability else None},
        "time_range": time_range,
        "schedule": schedule,
        "threshold": parse_threshold(text, capability),
        "missing_slots": missing,
    }
    if missing:
        missing_labels = {
            "org_scope": "门店或区域",
            "capability": "巡检能力",
            "time_range": "生效日期",
            "schedule": "巡检时段",
        }
        missing_text = "、".join(missing_labels.get(item, item) for item in missing)
        examples = {
            "org_scope": "广州悦汇城",
            "capability": "离岗检测",
            "time_range": "下周开始",
            "schedule": "每天 9 点到 22 点",
        }
        example_text = "，".join(examples[item] for item in missing if item in examples)
        plan = create_plan(
            conn,
            user,
            conversation_id,
            "SUBSCRIPTION_CREATE",
            "HIGH_WRITE",
            "NEED_CLARIFICATION",
            slots,
            [],
            ["slot.required"],
            True,
            "订阅创建信息不完整",
            {"passed": False, "missing_slots": missing},
            f"pending_{uuid.uuid4().hex[:12]}",
        )
        return plan, f"创建任务还需要补充：{missing_text}。你可以直接回复“{example_text}”。", {}

    expanded_org_ids = expand_scope(conn, org_ids)
    assert_org_access(conn, user, expanded_org_ids)
    store_ids = [row["org_id"] for row in rows(conn, f"SELECT org_id FROM orgs WHERE org_id IN ({','.join('?' for _ in expanded_org_ids)}) AND org_type='store'", expanded_org_ids)]
    if not store_ids:
        store_ids = [org_id for org_id in expanded_org_ids if org_id in allowed_org_ids(conn, user)]
    cameras = rows(
        conn,
        f"SELECT * FROM cameras WHERE tenant_id=? AND org_id IN ({','.join('?' for _ in store_ids)})",
        [user["tenant_id"], *store_ids],
    )
    online = [c for c in cameras if c["stream_status"] == "ONLINE"]
    offline = [c for c in cameras if c["stream_status"] != "ONLINE"]
    if not online:
        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "授权范围内没有可用在线摄像头"})
    uncalibrated = [c for c in online if c["calibration_status"] != "CALIBRATED"]
    threshold = parse_threshold(text, capability)
    visual_compliance = (
        threshold.get("visual_compliance")
        if capability and capability["capability_id"] == VISUAL_COMPLIANCE_CAPABILITY_ID and isinstance(threshold.get("visual_compliance"), dict)
        else None
    )
    scope = {
        "org_ids": org_ids,
        "expanded_org_ids": expanded_org_ids,
        "store_count": len(store_ids),
        "camera_count": len(cameras),
        "online_camera_count": len(online),
        "offline_camera_count": len(offline),
        "uncalibrated_camera_count": len(uncalibrated),
    }
    slots["org_scope"].update(scope)
    slots["capability"].update({"name": capability["name"], "app_id": capability["app_id"], "version": capability["version"]})
    slots["camera_scope"] = {"resolved_ids": [c["camera_id"] for c in online], "offline_ids": [c["camera_id"] for c in offline]}
    if visual_compliance:
        slots["visual_compliance"] = visual_compliance
    validation = {
        "passed": True,
        "permission": "PASSED",
        "camera_online": {"passed": bool(online), "online": len(online), "offline": len(offline)},
        "capability": "PASSED",
        "quota": "PASSED",
        "warnings": [],
    }
    if offline:
        validation["warnings"].append(f"{len(offline)} 路摄像头离线，本次计划仅纳入在线摄像头")
    if visual_compliance and visual_compliance.get("reference_assets_required"):
        validation["warnings"].append(
            "该对象包需要参考素材："
            + "、".join(visual_compliance["reference_assets_required"])
            + "；未上传前相关规则会以低置信待确认为主。"
        )
    if capability["calibration_required"] and uncalibrated:
        if capability["allow_full_frame"]:
            validation["warnings"].append(f"{len(uncalibrated)} 路摄像头未标定，将按全画面检测并提示准确率风险")
        else:
            raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "能力要求标定，存在未标定摄像头"})
    action = {
        "action_id": "a1",
        "tool": "subscription.create",
        "params": {
            "app_id": capability["app_id"],
            "app_version_id": capability["app_version_id"],
            "capability_id": capability["capability_id"],
            "org_id": org_ids[0],
            "camera_ids": [c["camera_id"] for c in online],
            "schedule": schedule,
            "valid_from": time_range["start"],
            "valid_to": time_range["end"],
            "thresholds": threshold,
            "dedupe_policy": {"enabled": True, "dedupe_minutes": 10},
        },
        "idempotency_key": f"sub_create_{conversation_id}_{uuid.uuid4().hex[:8]}",
    }
    summary_scope = f"{scope['store_count']}家门店" if scope["store_count"] > 1 else org_label(conn, org_ids[0])
    summary_capability = capability["name"]
    if visual_compliance:
        summary_capability = f"{VISUAL_COMPLIANCE_NAME}（{visual_compliance['name']}）"
    plan = create_plan(
        conn,
        user,
        conversation_id,
        "SUBSCRIPTION_CREATE",
        "HIGH_WRITE",
        "READY_FOR_CONFIRM",
        slots,
        [action],
        ["permission.check", "camera.online_check", "capability.compatibility_check", "quota.cost_check"],
        True,
        f"为{summary_scope}创建{summary_capability}订阅",
        validation,
        action["idempotency_key"],
    )
    content = f"已生成计划卡：{plan['summary']}。将影响 {scope['store_count']} 家门店、{scope['online_camera_count']} 路在线摄像头，确认后才会创建订阅。"
    return plan, content, {"scope": scope}


def query_events(conn, user, text: str, context: dict, page: int = 1, page_size: int = 50) -> dict:
    org_ids, ambiguous = find_org_candidates(conn, text, context.get("org_id"), user)
    if ambiguous:
        raise ApiError("ENTITY_AMBIGUOUS", HTTPStatus.CONFLICT, ambiguous)
    if not org_ids:
        org_ids = sorted(allowed_org_ids(conn, user))
    expanded = expand_scope(conn, org_ids)
    assert_org_access(conn, user, expanded)
    capability = find_capability(conn, text)
    event_type = capability["event_type"] if capability else None
    time_range = parse_time_range(text) or {"raw": "默认近7天", "start": f"{(CURRENT_DATE - timedelta(days=6)).isoformat()}T00:00:00+08:00", "end": f"{CURRENT_DATE.isoformat()}T23:59:59+08:00"}
    threshold = parse_threshold(text, capability)
    params = [user["tenant_id"], time_range["start"], time_range["end"], *expanded]
    where = [f"tenant_id=?", "started_at>=?", "started_at<=?", f"org_id IN ({','.join('?' for _ in expanded)})"]
    if event_type:
        where.append("event_type=?")
        params.append(event_type)
    if "duration_seconds" in threshold and any(word in text for word in ["超过", "大于"]):
        where.append("duration_seconds>?")
        params.append(int(threshold["duration_seconds"]))
    count_sql = f"SELECT COUNT(*) AS total FROM events WHERE {' AND '.join(where)}"
    total = int(one(conn, count_sql, params)["total"])
    offset = (page - 1) * page_size
    sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY started_at DESC LIMIT ? OFFSET ?"
    events = rows(conn, sql, [*params, page_size, offset])
    serialized = [serialize_event(conn, event) for event in events]
    summary = {
        "total": total,
        "true_positive": len([e for e in serialized if e["status"] == "TRUE_POSITIVE"]),
        "false_positive": len([e for e in serialized if e["status"] == "FALSE_POSITIVE"]),
        "pending_confirm": len([e for e in serialized if e["status"] == "PENDING_CONFIRM"]),
        "scope": {"org_ids": expanded, "time_range": time_range, "event_type": event_type, "threshold": threshold},
    }
    total_pages = (total + page_size - 1) // page_size if total else 0
    pagination = {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "range_start": offset + 1 if serialized else 0,
        "range_end": offset + len(serialized) if serialized else 0,
        "page_size_options": [10, 20, 50, 100],
    }
    return {"summary": summary, "events": serialized, "pagination": pagination, "scope": summary["scope"]}


def serialize_event(conn, event: dict) -> dict:
    camera = one(conn, "SELECT * FROM cameras WHERE camera_id=?", (event["camera_id"],))
    org = one(conn, "SELECT * FROM orgs WHERE org_id=?", (event["org_id"],))
    evidence_ids = json_loads(event["evidence_ids"], [])
    evidences = rows(conn, f"SELECT * FROM evidence WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)})", evidence_ids) if evidence_ids else []
    return {
        "event_id": event["event_id"],
        "tenant_id": event["tenant_id"],
        "org_id": event["org_id"],
        "org_name": org["name"] if org else event["org_id"],
        "camera_id": event["camera_id"],
        "camera_name": camera["name"] if camera else event["camera_id"],
        "point_label": camera["point_label"] if camera else "",
        "event_type": event["event_type"],
        "event_name": event_type_label(event["event_type"]),
        "severity": event["severity"],
        "started_at": event["started_at"],
        "ended_at": event["ended_at"],
        "duration_seconds": event["duration_seconds"],
        "confidence": event["confidence"],
        "status": event["status"],
        "evidence_count": len(evidences),
        "evidence": [serialize_evidence(e) for e in evidences],
        "model_version": event["model_version"],
        "rule_snapshot": json_loads(event["rule_snapshot"], {}),
    }


def serialize_evidence(evidence: dict) -> dict:
    return {
        "evidence_id": evidence["evidence_id"],
        "event_id": evidence["event_id"],
        "type": evidence["type"],
        "storage_url": evidence["storage_url"],
        "thumbnail_url": evidence["thumbnail_url"],
        "captured_at": evidence["captured_at"],
        "bbox": json_loads(evidence["bbox"], {}),
        "metadata": json_loads(evidence["metadata"], {}),
    }


def event_type_label(event_type: str) -> str:
    return {
        "LEAVE_POST": "离岗",
        "SMOKING": "抽烟",
        "FIRE_LANE_BLOCKED": "消防通道占用",
        "RECEPTION_ABSENT": "进店无人接待",
        VISUAL_COMPLIANCE_EVENT_TYPE: "门店视觉合规",
    }.get(event_type, event_type)


def analytics_query(conn, user, question: str, context: dict) -> dict:
    org_ids, ambiguous = find_org_candidates(conn, question, context.get("org_id"), user)
    if ambiguous:
        raise ApiError("ENTITY_AMBIGUOUS", HTTPStatus.CONFLICT, ambiguous)
    if not org_ids:
        org_ids = sorted(allowed_org_ids(conn, user))
    expanded = expand_scope(conn, org_ids)
    assert_org_access(conn, user, expanded)
    time_range = parse_time_range(question) or {"raw": "默认近7天", "start": f"{(CURRENT_DATE - timedelta(days=6)).isoformat()}T00:00:00+08:00", "end": f"{CURRENT_DATE.isoformat()}T23:59:59+08:00"}
    capability = find_capability(conn, question)
    event_type = capability["event_type"] if capability else None
    params = [user["tenant_id"], time_range["start"], time_range["end"], *expanded]
    where = [f"e.tenant_id=?", "e.started_at>=?", "e.started_at<=?", f"e.org_id IN ({','.join('?' for _ in expanded)})"]
    if event_type:
        where.append("e.event_type=?")
        params.append(event_type)
    sql = f"""
      SELECT o.org_id, o.name AS org_name, COUNT(*) AS event_count,
             SUM(CASE WHEN e.status='TRUE_POSITIVE' THEN 1 ELSE 0 END) AS true_positive,
             SUM(CASE WHEN e.status='FALSE_POSITIVE' THEN 1 ELSE 0 END) AS false_positive
      FROM events e
      JOIN orgs o ON o.org_id=e.org_id
      WHERE {' AND '.join(where)}
      GROUP BY o.org_id, o.name
      ORDER BY event_count DESC, o.name ASC
      LIMIT 10
    """
    ranking = rows(conn, sql, params)
    total = sum(row["event_count"] for row in ranking)
    handled = sum((row["true_positive"] or 0) + (row["false_positive"] or 0) for row in ranking)
    false_positive = sum(row["false_positive"] or 0 for row in ranking)
    result = {
        "query_id": f"qry_{uuid.uuid4().hex[:10]}",
        "question": question,
        "scope": {
            "time_range": time_range,
            "org_ids": expanded,
            "metric": "事件数",
            "event_type": event_type,
            "caliber": "按事件发生时间、授权组织范围、事件类型聚合；误报率=误报数/已处理事件数。",
        },
        "metrics": {
            "event_total": total,
            "false_positive_rate": round(false_positive / handled, 4) if handled else 0,
            "handled_total": handled,
        },
        "ranking": ranking,
    }
    conn.execute(
        "INSERT INTO analytics_queries VALUES (?,?,?,?,?,?,?)",
        (result["query_id"], user["tenant_id"], user["user_id"], question, json_dumps(result["scope"]), json_dumps(result), now_iso()),
    )
    log_audit(conn, user["user_id"], user["tenant_id"], "analytics.query", "analytics_query", result["query_id"], None, {"scope": result["scope"]}, "chat", None)
    return result


def camera_search(conn, user, text: str, context: dict) -> dict:
    org_ids, ambiguous = find_org_candidates(conn, text, context.get("org_id"), user)
    if ambiguous:
        raise ApiError("ENTITY_AMBIGUOUS", HTTPStatus.CONFLICT, ambiguous)
    if not org_ids:
        org_ids = sorted(allowed_org_ids(conn, user))
    expanded = expand_scope(conn, org_ids)
    assert_org_access(conn, user, expanded)
    params = [user["tenant_id"], *expanded]
    where = [f"tenant_id=?", f"org_id IN ({','.join('?' for _ in expanded)})"]
    if "离线" in text:
        where.append("stream_status!='ONLINE'")
    cameras = rows(conn, f"SELECT * FROM cameras WHERE {' AND '.join(where)} ORDER BY org_id, name", params)
    return {"cameras": [serialize_camera(c) for c in cameras], "redaction": "原始流地址、设备密码和内部凭证引用不会返回前端"}


def build_feedback_plan(conn, user, conversation_id: str, text: str, context: dict) -> tuple[dict | None, str, dict]:
    if not role_can_feedback(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN, {"intent": "FEEDBACK_CREATE"})
    event_id = context.get("event_id")
    event_match = re.search(r"EV[-A-Z0-9]+", text)
    if event_match:
        event_id = event_match.group(0)
    if not event_id:
        plan = create_plan(conn, user, conversation_id, "FEEDBACK_CREATE", "MEDIUM_WRITE", "NEED_CLARIFICATION", {"missing_slots": ["event_id"]}, [], ["slot.required"], True, "反馈信息缺少事件 ID", {"passed": False, "missing_slots": ["event_id"]}, f"pending_{uuid.uuid4().hex[:12]}")
        return plan, "请先指定要反馈的事件，例如 EV-10231，或者从事件详情里发起反馈。", {}
    event = one(conn, "SELECT * FROM events WHERE event_id=?", (event_id,))
    if not event:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"event_id": event_id})
    assert_org_access(conn, user, [event["org_id"]])
    feedback_type = "FALSE_POSITIVE" if "误报" in text else "TRUE_POSITIVE" if "真警" in text else "IGNORED"
    reason = "用户对话反馈"
    if "遮挡" in text:
        reason = "摄像头遮挡"
    elif "标定" in text:
        reason = "标定问题"
    elif "模型" in text:
        reason = "模型问题"
    action = {
        "action_id": "a1",
        "tool": "feedback.create",
        "params": {"event_id": event_id, "feedback_type": feedback_type, "reason": reason, "description": text},
        "idempotency_key": f"feedback_{event_id}_{uuid.uuid4().hex[:8]}",
    }
    plan = create_plan(
        conn,
        user,
        conversation_id,
        "FEEDBACK_CREATE",
        "MEDIUM_WRITE",
        "READY_FOR_CONFIRM",
        {"event_id": event_id, "feedback_type": feedback_type, "reason": reason, "missing_slots": []},
        [action],
        ["permission.check", "event.exists", "evidence.exists"],
        True,
        f"将事件 {event_id} 标记为{feedback_label(feedback_type)}",
        {"passed": True, "event_id": event_id, "evidence_ids": json_loads(event["evidence_ids"], [])},
        action["idempotency_key"],
    )
    return plan, f"已生成反馈计划：{plan['summary']}。确认后会更新事件状态并写入 badcase 反馈记录。", {}


def feedback_label(feedback_type: str) -> str:
    return {"FALSE_POSITIVE": "误报", "TRUE_POSITIVE": "真警", "IGNORED": "忽略"}.get(feedback_type, feedback_type)


def ensure_plan_succeeded_from_result(conn, plan_obj: dict, result: dict) -> dict:
    if plan_obj.get("status") == "SUCCEEDED":
        return plan_obj
    ts = now_iso()
    conn.execute(
        """
        UPDATE plans
        SET status='SUCCEEDED',
            confirmed_at=COALESCE(confirmed_at, ?),
            result=?,
            updated_at=?
        WHERE plan_id=?
        """,
        (ts, json_dumps(result or {}), ts, plan_obj["plan_id"]),
    )
    return serialize_plan(one(conn, "SELECT * FROM plans WHERE plan_id=?", (plan_obj["plan_id"],)))


def refresh_confirmed_batch_scheduled_result(conn, user: dict, plan_obj: dict, result: dict) -> dict:
    if plan_obj.get("intent") != "BATCH_SCHEDULED_INSPECTION_CREATE":
        return result
    batch_id = result.get("batch_id") or (result.get("inspection_batch") or {}).get("batch_id")
    if not batch_id:
        return result
    batch_row = one(
        conn,
        "SELECT * FROM inspection_batches WHERE batch_id=? AND tenant_id=?",
        (batch_id, user["tenant_id"]),
    )
    if not batch_row:
        return result

    if batch_row["status"] == "CANCELLED":
        requeued = requeue_cancelled_inspection_batch(conn, user, plan_obj, batch_row)
        if requeued:
            batch = requeued["inspection_batch"]
            return {
                **result,
                "requeued": True,
                "batch_id": batch["batch_id"],
                "status": batch["status"],
                "inspection_batch": batch,
                "message": (
                    f"已恢复已取消的多门店周期巡检并排队首轮执行：恢复 {requeued['retried_store_count']} 家，"
                    f"仍需处理 {requeued['still_failed_store_count']} 家。首轮快照会由调度器立即拉取并回写分析结果。"
                ),
                "audit_action": "inspection_batch.retry",
            }

    repaired = ensure_batch_scheduled_tasks_queued(conn, user, plan_obj, batch_row)
    if repaired:
        batch = repaired["inspection_batch"]
        return {
            **result,
            "schedule_repaired": True,
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "inspection_batch": batch,
            "message": (
                f"已校准多门店周期巡检调度状态：恢复 {repaired['repaired_store_count']} 家，"
                f"补建 {repaired['created_store_count']} 家。首轮快照会由调度器立即拉取并回写模型分析结果。"
            ),
            "audit_action": "inspection_batch.schedule_repair",
        }

    batch = serialize_inspection_batch(conn, batch_row)
    return {
        **result,
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "inspection_batch": batch,
    }


def persist_confirmed_plan_result(conn, plan_obj: dict, result: dict):
    ts = now_iso()
    conn.execute(
        "UPDATE plans SET result=?, updated_at=? WHERE plan_id=?",
        (json_dumps(result), ts, plan_obj["plan_id"]),
    )
    conn.execute(
        "UPDATE idempotency_keys SET response_json=?, created_at=? WHERE idempotency_key=?",
        (json_dumps(result), ts, plan_obj["idempotency_key"]),
    )


def execute_plan(conn, user, plan_id: str):
    plan = one(conn, "SELECT * FROM plans WHERE plan_id=? AND tenant_id=?", (plan_id, user["tenant_id"]))
    if not plan:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"plan_id": plan_id})
    plan_obj = serialize_plan(plan)
    if plan_obj["user_id"] != user["user_id"] and user["role"] != "tenant_admin":
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    if plan_obj["status"] == "SUCCEEDED" and plan_obj["result"]:
        refreshed_result = refresh_confirmed_batch_scheduled_result(conn, user, plan_obj, plan_obj["result"])
        if refreshed_result != plan_obj["result"]:
            persist_confirmed_plan_result(conn, plan_obj, refreshed_result)
        return {"deduped": True, **refreshed_result}
    if plan_obj["status"] != "READY_FOR_CONFIRM":
        raise ApiError("PLAN_NOT_CONFIRMABLE", HTTPStatus.CONFLICT, {"status": plan_obj["status"]})
    existing = one(conn, "SELECT response_json FROM idempotency_keys WHERE idempotency_key=?", (plan_obj["idempotency_key"],))
    if existing:
        stored_result = json_loads(existing["response_json"], {})
        stored_result = refresh_confirmed_batch_scheduled_result(conn, user, plan_obj, stored_result)
        persist_confirmed_plan_result(conn, plan_obj, stored_result)
        ensure_plan_succeeded_from_result(conn, plan_obj, stored_result)
        return {"deduped": True, **stored_result}
    if plan_obj["intent"] == "SUBSCRIPTION_CREATE":
        result = execute_subscription_plan(conn, user, plan_obj)
    elif plan_obj["intent"] == "CREATE_SCHEDULED_INSPECTION":
        result = execute_scheduled_inspection_plan(conn, user, plan_obj)
    elif plan_obj["intent"] == "BATCH_INSPECTION_EXECUTE":
        result = execute_batch_visual_inspection_plan(conn, user, plan_obj)
    elif plan_obj["intent"] == "BATCH_SCHEDULED_INSPECTION_CREATE":
        result = execute_batch_scheduled_inspection_plan(conn, user, plan_obj)
    elif plan_obj["intent"] == "FEEDBACK_CREATE":
        result = execute_feedback_plan(conn, user, plan_obj)
    else:
        raise ApiError("VALIDATION_FAILED", HTTPStatus.BAD_REQUEST, {"message": f"Unsupported plan intent {plan_obj['intent']}"})
    conn.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?)", (plan_obj["idempotency_key"], user["user_id"], json_dumps(result), now_iso()))
    conn.execute(
        "UPDATE plans SET status='SUCCEEDED', confirmed_at=?, result=?, updated_at=? WHERE plan_id=?",
        (now_iso(), json_dumps(result), now_iso(), plan_id),
    )
    updated_plan = serialize_plan(one(conn, "SELECT * FROM plans WHERE plan_id=?", (plan_id,)))
    linked_object = {"plan": updated_plan, **result}
    if isinstance(linked_object.get("agent"), dict):
        trace_artifact = linked_object.get("artifact") if isinstance(linked_object.get("artifact"), dict) else {}
        if isinstance(linked_object.get("inspection_batch"), dict):
            trace_artifact = {**trace_artifact, "batchInspection": linked_object["inspection_batch"]}
        attach_agent_trace(linked_object, plan_obj.get("slots", {}).get("request_text") or result.get("message", ""), trace_artifact)
    add_message(conn, plan_obj["conversation_id"], "assistant", result["message"], plan_id, linked_object)
    return {"deduped": False, **result}


def cancel_plan(conn, user, plan_id: str) -> dict:
    plan = one(conn, "SELECT * FROM plans WHERE plan_id=? AND tenant_id=?", (plan_id, user["tenant_id"]))
    if not plan:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"plan_id": plan_id})
    plan_obj = serialize_plan(plan)
    if plan_obj["user_id"] != user["user_id"] and user["role"] != "tenant_admin":
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    if plan_obj["status"] == "SUCCEEDED":
        raise ApiError("PLAN_NOT_CONFIRMABLE", HTTPStatus.CONFLICT, {"status": plan_obj["status"], "message": "已执行的计划不能取消"})
    before = {"status": plan_obj["status"]}
    if plan_obj["status"] != "CANCELLED":
        conn.execute("UPDATE plans SET status='CANCELLED', updated_at=? WHERE plan_id=?", (now_iso(), plan_id))
        log_audit(
            conn,
            user["user_id"],
            user["tenant_id"],
            "plan.cancel",
            "plan",
            plan_id,
            before,
            {"status": "CANCELLED"},
            "agent",
            plan_id,
        )
    updated_plan = serialize_plan(one(conn, "SELECT * FROM plans WHERE plan_id=?", (plan_id,)))
    existing_cancel_message = one(
        conn,
        """
        SELECT * FROM messages
        WHERE linked_plan_id=? AND sender='assistant' AND content LIKE '已取消本次任务%'
        ORDER BY created_at DESC LIMIT 1
        """,
        (plan_id,),
    )
    if existing_cancel_message:
        message = existing_cancel_message
    else:
        message = add_message(
            conn,
            updated_plan["conversation_id"],
            "assistant",
            "已取消本次任务，没有修改任何配置，也不会再次提醒确认。",
            plan_id,
            {"plan": updated_plan, "source": "plan_cancel"},
        )
    return {"plan": updated_plan, "message": serialize_message(message, conn)}


def serialize_scheduled_task(conn, task: dict, include_runs: bool = False) -> dict:
    item = dict(task)
    item["camera_ids"] = json_loads(item.get("camera_ids"), [])
    item["camera_names"] = json_loads(item.get("camera_names"), [])
    item["schedule"] = json_loads(item.get("schedule"), {})
    item["thresholds"] = json_loads(item.get("thresholds"), {})
    item["kind"] = "SCHEDULED_VISUAL"
    if include_runs:
        run_rows = rows(conn, "SELECT * FROM inspection_runs WHERE task_id=? ORDER BY scheduled_at DESC LIMIT 20", (item["task_id"],))
        knowledge_titles = [hit["title"] for hit in inspection_knowledge_hits(item)]
        item["runs"] = [
            {**serialize_inspection_run(conn, run), "knowledge_titles": knowledge_titles}
            for run in run_rows
        ]
    return item


def serialize_scheduled_evidence(
    evidence: dict,
    anomaly_evidence_ids: set[str] | None = None,
    anomaly_reason: str | None = None,
    sku_labels: list[str] | None = None,
    analysis_pending: bool = False,
) -> dict:
    is_anomalous = evidence["evidence_id"] in (anomaly_evidence_ids or set())
    return {
        "evidence_id": evidence["evidence_id"],
        "camera_id": evidence["camera_id"],
        "camera_name": evidence["camera_name"],
        "org_id": evidence["org_id"],
        "org_name": evidence["org_name"],
        "captured_at": evidence["captured_at"],
        "snapshot_url": f"/api/scheduled-evidence/{evidence['evidence_id']}?access_token={evidence['access_token']}",
        "sha256": evidence["sha256"],
        "byte_size": evidence["byte_size"],
        "is_anomalous": is_anomalous,
        "anomaly_reason": anomaly_reason if is_anomalous else None,
        "sku_labels": list(dict.fromkeys(str(item)[:64] for item in (sku_labels or []) if str(item))),
        "analysis_pending": bool(analysis_pending),
        "analysis_note": "模型分析未完成，待复核" if analysis_pending else None,
    }


def validated_run_sku_matches(raw_matches, evidence_rows: list[dict]) -> list[dict]:
    """Keep only structured SKU matches tied to an archived frame in this run."""
    if not isinstance(raw_matches, list):
        return []
    valid_camera_names = {str(item.get("camera_name") or "") for item in evidence_rows}
    matches = []
    seen = set()
    for item in raw_matches[:48]:
        if not isinstance(item, dict):
            continue
        camera_name = str(item.get("camera_name") or "").strip()[:160]
        sku = str(item.get("sku") or "").strip().upper()
        if (
            camera_name not in valid_camera_names
            or not KNOWLEDGE_SKU_LABEL_PATTERN.fullmatch(sku)
            or (camera_name, sku) in seen
        ):
            continue
        seen.add((camera_name, sku))
        matches.append({"camera_name": camera_name, "sku": sku})
    return matches


def sku_labels_for_evidence(sku_matches: list[dict], evidence: dict) -> list[str]:
    camera_name = str(evidence.get("camera_name") or "")
    return list(
        dict.fromkeys(
            str(item.get("sku") or "")[:64]
            for item in sku_matches
            if isinstance(item, dict) and item.get("camera_name") == camera_name and item.get("sku")
        )
    )


def visual_analysis_partial_note(result: dict | None) -> str | None:
    """Explain unanalysed cameras without turning them into false SKU risks."""
    if not isinstance(result, dict):
        return None
    failed = list(
        dict.fromkeys(
            str(item).strip()
            for item in result.get("failed_camera_names") or []
            if str(item).strip()
        )
    )
    if not failed:
        return None
    shown = "、".join(failed[:12])
    suffix = "等" if len(failed) > 12 else ""
    return (
        f"{shown}{suffix} 共 {len(failed)} 路镜头模型分析连续失败，"
        "未被判定为风险，也未标注 SKU；请在下轮自动重试或人工复核。"
    )


def failed_camera_names_from_run_trace(run: dict, evidence_rows: list[dict]) -> list[str]:
    """Recover model-failed cameras for both new and pre-v1.3.4 run records."""
    raw_trace = run.get("trace_json")
    trace = raw_trace if isinstance(raw_trace, dict) else json_loads(raw_trace, {})
    nodes = trace.get("nodes") if isinstance(trace, dict) else []
    direct_names = []
    candidate_names = set()
    failed_count = 0
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        for container in (node.get("detail"), node.get("input"), node.get("output")):
            if not isinstance(container, dict):
                continue
            direct_names.extend(container.get("failed_camera_names") or [])
            try:
                failed_count = max(failed_count, int(container.get("failed_image_count") or 0))
            except (TypeError, ValueError):
                pass
            for candidate in container.get("candidate_outputs") or []:
                if isinstance(candidate, dict) and candidate.get("camera_name"):
                    candidate_names.add(str(candidate["camera_name"]))
    evidence_names = [str(item.get("camera_name") or "") for item in evidence_rows]
    available = set(evidence_names)
    direct = [
        name for name in dict.fromkeys(str(item).strip() for item in direct_names if str(item).strip())
        if name in available
    ]
    if direct:
        return direct
    # Older runs retained only a failed count and candidate outputs.  When the
    # missing archived frame can be identified, show it as pending review rather
    # than pretending it was a normal non-risk frame.
    if failed_count and candidate_names:
        missing = [name for name in evidence_names if name and name not in candidate_names]
        return missing[:failed_count]
    return []


def sku_risk_camera_names_from_run_trace(run: dict, evidence_rows: list[dict]) -> list[str]:
    """Restore complete per-camera SKU risks from an older, truncated trace."""
    raw_trace = run.get("trace_json")
    trace = raw_trace if isinstance(raw_trace, dict) else json_loads(raw_trace, {})
    nodes = trace.get("nodes") if isinstance(trace, dict) else []
    candidate_outputs = []
    question = ""
    explicit_risks = []
    sku_comparison_enabled = False
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        detail = node.get("detail") if isinstance(node.get("detail"), dict) else {}
        node_input = node.get("input") if isinstance(node.get("input"), dict) else {}
        question = question or str(node_input.get("question") or "")
        comparison = detail.get("sku_comparison") if isinstance(detail.get("sku_comparison"), dict) else {}
        if comparison.get("policy") == "ANY_MATCH_PER_CAMERA":
            sku_comparison_enabled = True
            explicit_risks.extend(comparison.get("risk_camera_names") or [])
        candidate_outputs.extend(detail.get("candidate_outputs") or [])
    evidence_names = [str(item.get("camera_name") or "") for item in evidence_rows]
    available = set(evidence_names)
    risk_names = [
        name for name in dict.fromkeys(str(item).strip() for item in explicit_risks if str(item).strip())
        if name in available
    ]
    # v1.3.3 stored raw candidate outputs but not sku_comparison in the trace.
    # Its inspection prompt contained an explicit SKU policy, which is enough to
    # reconstruct a deterministic risk set from model-provided target/relevance/
    # SKU fields without classifying an unanalysed frame as a risk.
    if not sku_comparison_enabled:
        sku_comparison_enabled = "SKU" in question.upper()
    if sku_comparison_enabled:
        for candidate in candidate_outputs:
            if not isinstance(candidate, dict):
                continue
            camera_name = str(candidate.get("camera_name") or "").strip()
            output = candidate.get("output") if isinstance(candidate.get("output"), dict) else {}
            matched_skus = output.get("matched_skus") if isinstance(output.get("matched_skus"), list) else []
            try:
                relevance = float(output.get("relevance") or 0)
            except (TypeError, ValueError):
                relevance = 0.0
            if (
                camera_name in available
                and not matched_skus
                and output.get("target_observed") is not False
                and relevance >= 0.2
                and str(output.get("status") or "").upper() != "UNCERTAIN"
                and camera_name not in risk_names
            ):
                risk_names.append(camera_name)
    risk_set = set(risk_names)
    return [name for name in evidence_names if name in risk_set]


def inspection_run_timing(run: dict, evidence_rows: list[dict]) -> dict:
    """Make planned and actual capture times explicit without reading image watermarks."""
    scheduled_at = run.get("scheduled_at")
    started_at = run.get("started_at")
    captured_values = [str(item.get("captured_at") or "") for item in evidence_rows if item.get("captured_at")]
    first_captured_at = min(captured_values) if captured_values else None

    def delay_seconds(actual_at: str | None) -> int | None:
        if not scheduled_at or not actual_at:
            return None
        try:
            return int((parse_iso_datetime(actual_at) - parse_iso_datetime(scheduled_at)).total_seconds())
        except (TypeError, ValueError):
            return None

    start_delay_seconds = delay_seconds(started_at)
    capture_delay_seconds = delay_seconds(first_captured_at)
    comparison_delay = capture_delay_seconds if capture_delay_seconds is not None else start_delay_seconds
    return {
        "scheduled_at": scheduled_at,
        "started_at": started_at,
        "first_captured_at": first_captured_at,
        "start_delay_seconds": start_delay_seconds,
        "capture_delay_seconds": capture_delay_seconds,
        "status": "DELAYED" if comparison_delay is not None and comparison_delay > 60 else "ON_TIME" if comparison_delay is not None else "PENDING",
    }


def serialize_inspection_run(conn, run: dict) -> dict:
    item = dict(run)
    item["observations"] = json_loads(item.get("observations"), [])
    item["trace_json"] = json_loads(item.get("trace_json"), {})
    evidence_ids = json_loads(item.get("evidence_ids"), [])
    anomaly_evidence_ids = set(json_loads(item.get("anomaly_evidence_ids"), []))
    if evidence_ids:
        evidence_rows = rows(
            conn,
            f"SELECT * FROM scheduled_evidence WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)}) ORDER BY captured_at",
            evidence_ids,
        )
    else:
        evidence_rows = []
    if str(item.get("result_status") or "").upper() == "POSITIVE":
        recovered_risk_names = set(sku_risk_camera_names_from_run_trace(item, evidence_rows))
        anomaly_evidence_ids.update(
            evidence["evidence_id"]
            for evidence in evidence_rows
            if str(evidence.get("camera_name") or "") in recovered_risk_names
        )
    anomaly_reason = item.get("conclusion") or item.get("business_reason")
    sku_matches = validated_run_sku_matches(json_loads(item.get("sku_matches_json"), []), evidence_rows)
    failed_camera_names = set(failed_camera_names_from_run_trace(item, evidence_rows))
    item["evidence"] = [
        serialize_scheduled_evidence(
            evidence,
            anomaly_evidence_ids,
            anomaly_reason,
            sku_labels_for_evidence(sku_matches, evidence),
            str(evidence.get("camera_name") or "") in failed_camera_names,
        )
        for evidence in evidence_rows
    ]
    item.pop("evidence_ids", None)
    item["anomaly_evidence_ids"] = sorted(anomaly_evidence_ids)
    item["sku_matches"] = sku_matches
    item["failed_camera_names"] = sorted(failed_camera_names)
    item["timing"] = inspection_run_timing(item, evidence_rows)
    item.pop("sku_matches_json", None)
    return item


def serialize_inspection_history_record(conn, run: dict) -> dict:
    serialized = serialize_inspection_run(conn, run)
    evidence = serialized.get("evidence") or []
    task_thresholds = json_loads(run.get("task_thresholds"), {})
    knowledge_titles = [
        item["title"]
        for item in inspection_knowledge_hits({"thresholds": task_thresholds})
    ]
    return {
        "record_type": "AI_INSPECTION",
        "run_id": serialized["run_id"],
        "task_id": serialized["task_id"],
        "task_name": run["task_name"],
        "task_status": run["task_status"],
        "org_id": run["org_id"],
        "org_name": run["org_name"],
        "inspection_goal": run["inspection_goal"],
        "camera_names": json_loads(run.get("camera_names"), []),
        "scheduled_at": serialized["scheduled_at"],
        "started_at": serialized["started_at"],
        "completed_at": serialized.get("completed_at"),
        "status": serialized["status"],
        "result_status": serialized.get("result_status"),
        "conclusion": serialized.get("conclusion"),
        "confidence": serialized.get("confidence"),
        "business_reason": serialized.get("business_reason"),
        "observations": serialized.get("observations") or [],
        "model_version": serialized.get("model_version"),
        "error_message": serialized.get("error_message"),
        "attempt": serialized.get("attempt"),
        "evidence_count": len(evidence),
        "anomaly_evidence_count": len(serialized.get("anomaly_evidence_ids") or []),
        "anomaly_evidence_ids": serialized.get("anomaly_evidence_ids") or [],
        "knowledge_titles": knowledge_titles,
        "evidence": evidence,
        "source": "scheduled_inspection",
    }


def create_scheduled_inspection_task(
    conn,
    user: dict,
    plan: dict,
    params: dict,
    batch_id: str | None = None,
    force_first_run: bool = False,
) -> dict:
    if not params.get("org_id") or not params.get("camera_ids"):
        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "周期巡检缺少门店或摄像头"})
    task_id = f"scheduled_{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    schedule = params["schedule"]
    daily_window = schedule.get("daily_window") if isinstance(schedule.get("daily_window"), dict) else {}
    task_name = (
        f"{params['org_name']}-{daily_window.get('label', '每日固定时间')}快照巡检"
        if daily_window.get("mode") == "fixed_daily"
        else f"{params['org_name']}-每{schedule['interval_minutes']}分钟快照巡检"
    )
    next_run_at = ts
    task_status = "ACTIVE"
    force_first_run = force_first_run or bool(params.get("force_first_run"))
    if daily_window.get("mode") == "fixed_daily" and not force_first_run:
        next_candidate = next_scheduled_time(
            {"schedule": json_dumps(schedule), "end_at": params["end_at"]},
            parse_iso_datetime(params["start_at"]) - timedelta(seconds=1),
        )
        next_run_at = next_candidate.isoformat(timespec="seconds") if next_candidate else None
        task_status = "ACTIVE" if next_candidate else "COMPLETED"
    conn.execute(
        """
        INSERT INTO scheduled_inspections(
          task_id, tenant_id, user_id, conversation_id, org_id, org_name, name,
          inspection_goal, camera_ids, camera_names, schedule, start_at, end_at,
          next_run_at, last_run_at, status, run_count, anomaly_count, uncertain_count,
          thresholds, plan_id, batch_id, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task_id,
            user["tenant_id"],
            user["user_id"],
            plan["conversation_id"],
            params["org_id"],
            params["org_name"],
            task_name,
            params["inspection_goal"],
            json_dumps(params["camera_ids"]),
            json_dumps(params["camera_names"]),
            json_dumps(schedule),
            params["start_at"],
            params["end_at"],
            next_run_at,
            None,
            task_status,
            0,
            0,
            0,
            json_dumps(params.get("thresholds") or {"confidence": 0.8}),
            plan["plan_id"],
            batch_id,
            user["user_id"],
            ts,
            ts,
        ),
    )
    return serialize_scheduled_task(conn, one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,)))


def reactivate_scheduled_inspection_task(
    conn,
    user: dict,
    plan: dict,
    params: dict,
    task_id: str | None,
    batch_id: str,
) -> dict | None:
    if not task_id:
        return None
    if user.get("role") in {"tenant_admin", "system_admin"}:
        task = one(
            conn,
            "SELECT * FROM scheduled_inspections WHERE task_id=? AND tenant_id=?",
            (task_id, user["tenant_id"]),
        )
    else:
        task = one(
            conn,
            "SELECT * FROM scheduled_inspections WHERE task_id=? AND tenant_id=? AND user_id=?",
            (task_id, user["tenant_id"], user["user_id"]),
        )
    if not task:
        return None
    if not params.get("org_id") or not params.get("camera_ids"):
        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "周期巡检缺少门店或摄像头"})
    ts = now_iso()
    schedule = params.get("schedule") or {}
    daily_window = schedule.get("daily_window") if isinstance(schedule.get("daily_window"), dict) else {}
    task_name = (
        f"{params['org_name']}-{daily_window.get('label', '每日固定时间')}快照巡检"
        if daily_window.get("mode") == "fixed_daily"
        else f"{params['org_name']}-每{schedule.get('interval_minutes', 0)}分钟快照巡检"
    )
    force_first_run = bool(params.get("force_first_run"))
    next_run_at = ts
    task_status = "ACTIVE"
    if daily_window.get("mode") == "fixed_daily" and not force_first_run:
        next_candidate = next_scheduled_time(
            {"schedule": json_dumps(schedule), "end_at": params["end_at"]},
            datetime.now(CN_TZ).replace(microsecond=0) - timedelta(seconds=1),
        )
        next_run_at = next_candidate.isoformat(timespec="seconds") if next_candidate else None
        task_status = "ACTIVE" if next_candidate else "COMPLETED"
    conn.execute(
        """
        UPDATE scheduled_inspections
        SET conversation_id=?,
            org_id=?,
            org_name=?,
            name=?,
            inspection_goal=?,
            camera_ids=?,
            camera_names=?,
            schedule=?,
            start_at=?,
            end_at=?,
            next_run_at=?,
            status=?,
            thresholds=?,
            plan_id=?,
            batch_id=?,
            updated_at=?
        WHERE task_id=?
        """,
        (
            plan["conversation_id"],
            params["org_id"],
            params["org_name"],
            task_name,
            params["inspection_goal"],
            json_dumps(params["camera_ids"]),
            json_dumps(params.get("camera_names") or []),
            json_dumps(schedule),
            params.get("start_at"),
            params.get("end_at"),
            next_run_at,
            task_status,
            json_dumps(params.get("thresholds") or {"confidence": 0.8}),
            plan["plan_id"],
            batch_id,
            ts,
            task_id,
        ),
    )
    return serialize_scheduled_task(conn, one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,)))


def create_immediate_inspection_task(conn, user: dict, plan: dict, params: dict, batch_id: str | None = None) -> dict:
    if not params.get("org_id") or not params.get("camera_ids"):
        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "即时巡检缺少门店或摄像头"})
    task_id = f"scheduled_{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    schedule = params.get("schedule") or {
        "mode": "one_off",
        "label": "立即执行一次",
        "interval_minutes": 0,
        "timezone": "Asia/Shanghai",
    }
    conn.execute(
        """
        INSERT INTO scheduled_inspections(
          task_id, tenant_id, user_id, conversation_id, org_id, org_name, name,
          inspection_goal, camera_ids, camera_names, schedule, start_at, end_at,
          next_run_at, last_run_at, status, run_count, anomaly_count, uncertain_count,
          thresholds, plan_id, batch_id, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task_id,
            user["tenant_id"],
            user["user_id"],
            plan["conversation_id"],
            params["org_id"],
            params["org_name"],
            f"{params['org_name']}-即时AI巡检",
            params["inspection_goal"],
            json_dumps(params["camera_ids"]),
            json_dumps(params.get("camera_names") or []),
            json_dumps(schedule),
            params.get("start_at") or ts,
            params.get("end_at") or ts,
            None,
            None,
            "ACTIVE",
            0,
            0,
            0,
            json_dumps(params.get("thresholds") or {"confidence": 0.8}),
            plan["plan_id"],
            batch_id,
            user["user_id"],
            ts,
            ts,
        ),
    )
    return serialize_scheduled_task(conn, one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,)))


def complete_immediate_inspection_run(
    conn,
    user: dict,
    task: dict,
    run_id: str,
    result: dict | None,
    error_message: str | None,
    partial: bool,
) -> dict:
    run = one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,))
    if not run:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"run_id": run_id})
    evidence_ids = json_loads(run.get("evidence_ids"), [])
    evidence_rows = rows(
        conn,
        f"SELECT * FROM scheduled_evidence WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)}) ORDER BY captured_at",
        evidence_ids,
    ) if evidence_ids else []
    completed_at = now_iso()
    if result:
        model_partial_note = visual_analysis_partial_note(result)
        if model_partial_note:
            partial = True
            error_message = "；".join(
                dict.fromkeys(item for item in (error_message, model_partial_note) if item)
            )[:500]
        result_status = str(result.get("status") or "UNCERTAIN")
        run_status = "PARTIAL" if partial else "SUCCEEDED"
        conclusion = str(result.get("conclusion") or "已完成即时巡检。")[:1000]
        confidence = float(result.get("confidence") or 0)
        business_reason = str(result.get("business_reason") or "")[:500]
        observations = result.get("observations") if isinstance(result.get("observations"), list) else []
        if model_partial_note:
            observations = list(dict.fromkeys([*observations, model_partial_note]))[:20]
        anomaly_camera_names = {
            str(item) for item in (result.get("anomaly_camera_names") or []) if str(item)
        }
        if result_status == "POSITIVE" and not anomaly_camera_names:
            anomaly_camera_names = {
                str(item) for item in (result.get("selected_camera_names") or []) if str(item)
            }
        anomaly_evidence_ids = [
            item["evidence_id"]
            for item in evidence_rows
            if result_status == "POSITIVE" and item.get("camera_name") in anomaly_camera_names
        ]
        sku_matches = validated_run_sku_matches(result.get("sku_matches"), evidence_rows)
        model_version = str(result.get("model") or "")[:200]
    else:
        result_status = "UNCERTAIN"
        run_status = "FAILED"
        conclusion = "本次即时巡检执行失败，已保留成功抓取的快照。"
        confidence = 0.0
        business_reason = "执行失败，不能形成正常或异常结论。"
        observations = []
        anomaly_evidence_ids = []
        sku_matches = []
        model_version = None
    conn.execute(
        """UPDATE inspection_runs
           SET completed_at=?, status=?, result_status=?, conclusion=?, confidence=?, business_reason=?,
               observations=?, anomaly_evidence_ids=?, sku_matches_json=?, model_version=?, error_message=? WHERE run_id=?""",
        (
            completed_at,
            run_status,
            result_status,
            conclusion,
            confidence,
            business_reason,
            json_dumps(observations),
            json_dumps(anomaly_evidence_ids),
            json_dumps(sku_matches),
            model_version,
            error_message,
            run_id,
        ),
    )
    conn.execute(
        """UPDATE scheduled_inspections
           SET next_run_at=NULL, last_run_at=?, status='COMPLETED', run_count=1,
               anomaly_count=?, uncertain_count=?, updated_at=?
           WHERE task_id=?""",
        (
            completed_at,
            1 if result_status == "POSITIVE" else 0,
            1 if result_status == "UNCERTAIN" else 0,
            completed_at,
            task["task_id"],
        ),
    )
    updated_run = one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,))
    artifact = scheduled_run_artifact(task, updated_run, evidence_rows)
    knowledge_hits = inspection_knowledge_hits(task)
    tool_calls = ["paas.media.snapshot", "evidence.archive"]
    if knowledge_hits:
        tool_calls.append("knowledge.retrieve")
    tool_calls.extend(["vlm.image.inspect" if result else "vlm.image.inspect:failed", "scheduler.run.persist"])
    linked = {
        "artifact": {"scheduledRun": artifact},
        "agent": {
            "intent": "BATCH_INSPECTION_EXECUTE",
            "skill": "multi_store_visual_inspection",
            "tenant_id": user["tenant_id"],
            "engine": "batch_visual_executor",
            "status": "SUCCEEDED",
            "tool_calls": tool_calls,
            "knowledge_hits": knowledge_hits,
        },
        "source": "batch_inspection",
    }
    trace_artifact = {"scheduledRun": artifact, "knowledgeHits": knowledge_hits}
    if isinstance(result, dict):
        trace_artifact["visualResult"] = result
    attach_agent_trace(linked, task["inspection_goal"], trace_artifact)
    trace_json = linked.get("agent", {}).get("trace") or {}
    conn.execute("UPDATE inspection_runs SET trace_json=? WHERE run_id=?", (json_dumps(trace_json), run_id))
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "scheduled_inspection.run.complete" if result else "scheduled_inspection.run.failed",
        "inspection_run",
        run_id,
        None,
        {"result_status": result_status, "evidence_count": len(evidence_rows), "batch_id": task.get("batch_id")},
        "batch",
        task.get("plan_id"),
    )
    return serialize_inspection_run(conn, one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,)))


def execute_immediate_inspection_task(conn, user: dict, task: dict, inspection_goal: str) -> dict:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    started_at = now_iso()
    partial_errors = []
    model_images = []
    evidence_rows = []
    conn.execute(
        """INSERT INTO inspection_runs(
             run_id, task_id, scheduled_at, started_at, completed_at, status, attempt,
             result_status, conclusion, confidence, business_reason, observations,
             evidence_ids, model_version, error_message, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            task["task_id"],
            started_at,
            started_at,
            None,
            "ANALYZING",
            1,
            None,
            "快照抓取中",
            None,
            None,
            "[]",
            "[]",
            None,
            None,
            started_at,
        ),
    )
    try:
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if not online:
            return complete_immediate_inspection_run(conn, user, task, run_id, None, "DeepVision 在线服务未连接", False)
        knowledge_hits = resolve_inspection_knowledge_context(conn, task, inspection_goal)
        model_question = inspection_question_with_knowledge(inspection_goal, knowledge_hits)
        reference_images = inspection_reference_images(user["tenant_id"], knowledge_hits)
        camera_ids = task.get("camera_ids") if isinstance(task.get("camera_ids"), list) else json_loads(task["camera_ids"], [])
        camera_names = task.get("camera_names") if isinstance(task.get("camera_names"), list) else json_loads(task["camera_names"], [])
        snapshots = online.capture_scheduled_snapshots(task["org_id"], camera_ids)
        captured_ids = {str(item.get("camera_id") or "") for item in snapshots}
        for index, camera_id in enumerate(camera_ids):
            if camera_id not in captured_ids:
                camera_name = camera_names[index] if index < len(camera_names) else camera_id
                partial_errors.append(f"{camera_name}：本轮抓图失败")
        for media in snapshots:
            try:
                evidence, model_image = archive_scheduled_snapshot(conn, task, run_id, media)
                evidence_rows.append(evidence)
                model_images.append(model_image)
            except OnlineAgentError as exc:
                partial_errors.append(f"{media.get('camera_name', '未知镜头')}：{exc.message}")
        if not model_images:
            raise OnlineAgentError("VISUAL_EVIDENCE_MISSING", "所有摄像头快照均归档失败")
        evidence_ids = [item["evidence_id"] for item in evidence_rows]
        conn.execute("UPDATE inspection_runs SET evidence_ids=? WHERE run_id=?", (json_dumps(evidence_ids), run_id))
        result = None
        for attempt in range(1, 3):
            try:
                result = online.analyze_scheduled_snapshots(model_question, model_images, reference_images)
                break
            except OnlineAgentError:
                conn.execute("UPDATE inspection_runs SET attempt=? WHERE run_id=?", (attempt, run_id))
                if attempt == 2:
                    raise
                time.sleep(2)
        if result is None:
            raise OnlineAgentError("VLM_UNAVAILABLE", "视觉分析服务未返回结果")
        return complete_immediate_inspection_run(
            conn,
            user,
            task,
            run_id,
            result,
            "；".join(partial_errors) or None,
            bool(partial_errors),
        )
    except Exception as exc:  # noqa: BLE001
        error_message = inspection_execution_error_message(exc)
        return complete_immediate_inspection_run(conn, user, task, run_id, None, error_message[:500], bool(evidence_rows))


def execute_scheduled_inspection_plan(conn, user: dict, plan: dict) -> dict:
    if not role_can_create_subscription(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    params = plan["actions"][0]["params"]
    task = create_scheduled_inspection_task(
        conn,
        user,
        plan,
        params,
        force_first_run=bool(params.get("force_first_run")),
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "scheduled_inspection.create",
        "scheduled_inspection",
        task["task_id"],
        None,
        {"org_id": params["org_id"], "camera_count": len(params["camera_ids"]), "schedule": params["schedule"]},
        "chat",
        plan["plan_id"],
    )
    return {
        "task_id": task["task_id"],
        "status": "ACTIVE",
        "scheduled_task": task,
        "message": (
            f"周期巡检已创建：{task['name']}。首轮将于 {task['next_run_at']} 抓取快照，结果会自动回写到当前对话。"
            if task.get("next_run_at") and (task.get("schedule") or {}).get("daily_window", {}).get("mode") == "fixed_daily" and not params.get("force_first_run")
            else f"周期巡检已创建并启动：{task['name']}。首轮快照正在执行，结果会自动回写到当前对话。"
        ),
        "audit_action": "scheduled_inspection.create",
    }


def serialize_inspection_batch_item(conn, item: dict) -> dict:
    result = dict(item)
    result["camera_ids"] = json_loads(result.get("camera_ids"), [])
    result["camera_names"] = json_loads(result.get("camera_names"), [])
    result["run_ids"] = json_loads(result.get("run_ids"), [])
    if result["run_ids"]:
        run_rows = rows(
            conn,
            f"SELECT * FROM inspection_runs WHERE run_id IN ({','.join('?' for _ in result['run_ids'])}) ORDER BY scheduled_at DESC",
            result["run_ids"],
        )
        result["runs"] = [serialize_inspection_run(conn, run) for run in run_rows]
    else:
        result["runs"] = []
    result["is_anomalous"] = any(run.get("result_status") == "POSITIVE" for run in result["runs"])
    if result.get("scheduled_task_id"):
        task = one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (result["scheduled_task_id"],))
        result["scheduled_task"] = serialize_scheduled_task(conn, task) if task else None
    else:
        result["scheduled_task"] = None
    return result


def serialize_inspection_batch(conn, batch: dict, include_items: bool = True) -> dict:
    item = dict(batch)
    item["scope_snapshot"] = json_loads(item.get("scope_snapshot"), {})
    item["kind"] = "BATCH_VISUAL" if item.get("execution_mode") == "immediate" else "BATCH_SCHEDULED_VISUAL"
    if include_items:
        batch_items = rows(
            conn,
            "SELECT * FROM inspection_batch_items WHERE batch_id=? ORDER BY store_name ASC",
            (item["batch_id"],),
        )
        item["items"] = [serialize_inspection_batch_item(conn, row) for row in batch_items]
    return item


def update_inspection_batch_counts(conn, batch_id: str, updated_at: str | None = None) -> dict:
    updated_at = updated_at or now_iso()
    counts = one(
        conn,
        """
        SELECT
          SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS success_count,
          SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed_count,
          SUM(CASE WHEN status='SKIPPED' THEN 1 ELSE 0 END) AS skipped_count,
          SUM(CASE WHEN status IN ('RUNNING','PENDING') THEN 1 ELSE 0 END) AS pending_count,
          COUNT(*) AS total_count
        FROM inspection_batch_items WHERE batch_id=?
        """,
        (batch_id,),
    )
    success_count = int(counts["success_count"] or 0)
    failed_count = int(counts["failed_count"] or 0)
    skipped_count = int(counts["skipped_count"] or 0)
    pending_count = int(counts["pending_count"] or 0)
    total_count = int(counts["total_count"] or 0)
    if pending_count:
        final_status = "RUNNING"
    elif total_count and success_count == total_count:
        final_status = "SUCCEEDED"
    elif success_count:
        final_status = "PARTIAL_SUCCESS"
    else:
        final_status = "FAILED"
    conn.execute(
        """
        UPDATE inspection_batches
        SET status=?, success_store_count=?, failed_store_count=?, skipped_store_count=?, updated_at=?
        WHERE batch_id=?
        """,
        (final_status, success_count, failed_count, skipped_count, updated_at, batch_id),
    )
    return {
        "status": final_status,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "pending_count": pending_count,
        "total_count": total_count,
    }


def sync_batch_item_after_scheduled_run(
    conn,
    task: dict,
    run_id: str,
    run_status: str,
    result_status: str,
    error_message: str | None,
    updated_at: str,
):
    batch_id = task.get("batch_id")
    if not batch_id:
        return
    item = one(
        conn,
        "SELECT * FROM inspection_batch_items WHERE batch_id=? AND scheduled_task_id=?",
        (batch_id, task["task_id"]),
    )
    if not item:
        return
    run_ids = json_loads(item.get("run_ids"), []) or []
    if run_id not in run_ids:
        run_ids.append(run_id)
    item_status = "SUCCEEDED" if run_status in {"SUCCEEDED", "PARTIAL"} else "FAILED"
    failure_code = None if item_status == "SUCCEEDED" else (error_message or result_status or run_status or "RUN_FAILED")[:120]
    conn.execute(
        """
        UPDATE inspection_batch_items
        SET status=?, failure_code=?, run_ids=?, updated_at=?
        WHERE item_id=?
        """,
        (item_status, failure_code, json_dumps(run_ids), updated_at, item["item_id"]),
    )
    update_inspection_batch_counts(conn, batch_id, updated_at)


def user_can_view_batch(conn, user: dict, batch: dict) -> bool:
    if batch["tenant_id"] != user["tenant_id"]:
        return False
    if user["role"] in {"tenant_admin", "system_admin"}:
        return True
    allowed = allowed_org_ids(conn, user)
    item = one(
        conn,
        f"SELECT store_id FROM inspection_batch_items WHERE batch_id=? AND store_id IN ({','.join('?' for _ in allowed)}) LIMIT 1",
        (batch["batch_id"], *sorted(allowed)),
    ) if allowed else None
    return bool(item)


def requeue_cancelled_inspection_batch(conn, user: dict, plan: dict, batch: dict) -> dict | None:
    params = (plan.get("actions") or [{}])[0].get("params") or {}
    retry_items = rows(
        conn,
        """
        SELECT * FROM inspection_batch_items
        WHERE batch_id=?
          AND (
            status IN ('FAILED','SKIPPED','CANCELLED')
            OR (status='SUCCEEDED' AND run_ids='[]')
          )
        ORDER BY store_name ASC
        """,
        (batch["batch_id"],),
    )
    if not retry_items:
        return None

    retried = 0
    still_failed = 0
    ts = now_iso()
    for item in retry_items:
        camera_ids = json_loads(item.get("camera_ids"), [])
        camera_names = json_loads(item.get("camera_names"), [])
        if not camera_ids:
            conn.execute(
                """
                UPDATE inspection_batch_items
                SET retry_count=retry_count+1,
                    failure_code=COALESCE(failure_code, 'NO_ONLINE_CAMERA'),
                    updated_at=?
                WHERE item_id=?
                """,
                (ts, item["item_id"]),
            )
            still_failed += 1
            continue
        child_params = {
            "org_id": item["store_id"],
            "org_name": item["store_name"],
            "camera_ids": camera_ids,
            "camera_names": camera_names,
            "inspection_goal": params.get("inspection_goal"),
            "schedule": params.get("schedule") or {},
            "start_at": params.get("start_at"),
            "end_at": params.get("end_at"),
            "thresholds": params.get("thresholds") or {"confidence": 0.8},
            "force_first_run": bool(params.get("force_first_run")),
        }
        try:
            task = reactivate_scheduled_inspection_task(
                conn,
                user,
                plan,
                child_params,
                item.get("scheduled_task_id"),
                batch["batch_id"],
            )
            if not task:
                task = create_scheduled_inspection_task(
                    conn,
                    user,
                    plan,
                    child_params,
                    batch_id=batch["batch_id"],
                    force_first_run=bool(params.get("force_first_run")),
                )
            conn.execute(
                """
                UPDATE inspection_batch_items
                SET status='RUNNING',
                    failure_code=NULL,
                    retry_count=retry_count+1,
                    scheduled_task_id=?,
                    run_ids='[]',
                    updated_at=?
                WHERE item_id=?
                """,
                (task["task_id"], now_iso(), item["item_id"]),
            )
            retried += 1
        except Exception as exc:
            conn.execute(
                """
                UPDATE inspection_batch_items
                SET status='FAILED',
                    failure_code=?,
                    retry_count=retry_count+1,
                    updated_at=?
                WHERE item_id=?
                """,
                (getattr(exc, "code", None) or exc.__class__.__name__, now_iso(), item["item_id"]),
            )
            still_failed += 1

    counts = update_inspection_batch_counts(conn, batch["batch_id"], now_iso())
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "inspection_batch.retry",
        "inspection_batch",
        batch["batch_id"],
        {"status": batch["status"]},
        {
            "retried_store_count": retried,
            "still_failed_store_count": still_failed,
            "status": counts["status"],
            "source": "confirm_requeue",
        },
        "chat",
        plan.get("plan_id"),
    )
    return {
        "counts": counts,
        "retried_store_count": retried,
        "still_failed_store_count": still_failed,
        "inspection_batch": serialize_inspection_batch(
            conn,
            one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch["batch_id"],)),
        ),
    }


def ensure_batch_scheduled_tasks_queued(conn, user: dict, plan: dict, batch: dict) -> dict | None:
    if plan.get("intent") != "BATCH_SCHEDULED_INSPECTION_CREATE":
        return None
    if batch.get("status") not in {"RUNNING", "PARTIAL_SUCCESS"}:
        return None
    params = (plan.get("actions") or [{}])[0].get("params") or {}
    scope_snapshot = json_loads(batch.get("scope_snapshot"), {}) or {}
    schedule = params.get("schedule") or scope_snapshot.get("schedule") or {}
    inspection_goal = (
        params.get("inspection_goal")
        or scope_snapshot.get("inspection_goal")
        or (plan.get("slots") or {}).get("inspection_goal")
        or "周期快照 AI 巡检"
    )
    start_at = params.get("start_at") or scope_snapshot.get("start_at") or now_iso()
    end_at = params.get("end_at") or scope_snapshot.get("end_at") or start_at
    thresholds = params.get("thresholds") or scope_snapshot.get("thresholds") or {"confidence": 0.8}
    candidates = rows(
        conn,
        """
        SELECT * FROM inspection_batch_items
        WHERE batch_id=?
          AND status NOT IN ('FAILED','SKIPPED','CANCELLED')
        ORDER BY store_name ASC
        """,
        (batch["batch_id"],),
    )
    if not candidates:
        return None

    repaired = 0
    created = 0
    skipped = 0
    ts = now_iso()
    for item in candidates:
        camera_ids = json_loads(item.get("camera_ids"), [])
        camera_names = json_loads(item.get("camera_names"), [])
        if not camera_ids:
            skipped += 1
            continue
        task = None
        if item.get("scheduled_task_id"):
            if user.get("role") in {"tenant_admin", "system_admin"}:
                task = one(
                    conn,
                    "SELECT * FROM scheduled_inspections WHERE task_id=? AND tenant_id=?",
                    (item["scheduled_task_id"], user["tenant_id"]),
                )
            else:
                task = one(
                    conn,
                    "SELECT * FROM scheduled_inspections WHERE task_id=? AND tenant_id=? AND user_id=?",
                    (item["scheduled_task_id"], user["tenant_id"], user["user_id"]),
                )
        needs_repair = (
            not task
            or task.get("status") != "ACTIVE"
            or not task.get("next_run_at")
            or task.get("batch_id") != batch["batch_id"]
        )
        if not needs_repair:
            continue
        child_params = {
            "org_id": item["store_id"],
            "org_name": item["store_name"],
            "camera_ids": camera_ids,
            "camera_names": camera_names,
            "inspection_goal": inspection_goal,
            "schedule": schedule,
            "start_at": start_at,
            "end_at": end_at,
            "thresholds": thresholds,
            "force_first_run": bool(params.get("force_first_run")),
        }
        if task:
            queued_task = reactivate_scheduled_inspection_task(
                conn,
                user,
                plan,
                child_params,
                item["scheduled_task_id"],
                batch["batch_id"],
            )
            repaired += 1
        else:
            queued_task = create_scheduled_inspection_task(
                conn,
                user,
                plan,
                child_params,
                batch_id=batch["batch_id"],
                force_first_run=bool(params.get("force_first_run")),
            )
            created += 1
        conn.execute(
            """
            UPDATE inspection_batch_items
            SET status='RUNNING',
                failure_code=NULL,
                scheduled_task_id=?,
                updated_at=?
            WHERE item_id=?
            """,
            (queued_task["task_id"], ts, item["item_id"]),
        )

    if not repaired and not created:
        return None

    counts = update_inspection_batch_counts(conn, batch["batch_id"], now_iso())
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "inspection_batch.schedule_repair",
        "inspection_batch",
        batch["batch_id"],
        {"status": batch["status"]},
        {
            "repaired_store_count": repaired,
            "created_store_count": created,
            "skipped_store_count": skipped,
            "status": counts["status"],
        },
        "chat",
        plan.get("plan_id"),
    )
    return {
        "counts": counts,
        "repaired_store_count": repaired,
        "created_store_count": created,
        "skipped_store_count": skipped,
        "inspection_batch": serialize_inspection_batch(
            conn,
            one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch["batch_id"],)),
        ),
    }


def repair_visible_batch_schedules(conn, user: dict) -> dict:
    summary = {"batch_count": 0, "repaired_store_count": 0, "created_store_count": 0}
    creator_filter = ""
    args = [user["tenant_id"]]
    if user.get("role") not in {"tenant_admin", "system_admin"}:
        creator_filter = "AND created_by=?"
        args.append(user["user_id"])
    batches = rows(
        conn,
        f"""
        SELECT *
        FROM inspection_batches
        WHERE tenant_id=?
          {creator_filter}
          AND intent='BATCH_SCHEDULED_INSPECTION_CREATE'
          AND status IN ('RUNNING','PARTIAL_SUCCESS')
        ORDER BY updated_at DESC
        """,
        args,
    )
    for batch in batches:
        plan_row = one(
            conn,
            "SELECT * FROM plans WHERE plan_id=? AND tenant_id=?",
            (batch["plan_id"], user["tenant_id"]),
        )
        if not plan_row:
            continue
        repaired = ensure_batch_scheduled_tasks_queued(conn, user, serialize_plan(plan_row), batch)
        if not repaired:
            continue
        summary["batch_count"] += 1
        summary["repaired_store_count"] += repaired.get("repaired_store_count", 0)
        summary["created_store_count"] += repaired.get("created_store_count", 0)
    return summary


def execute_batch_visual_inspection_plan(conn, user: dict, plan: dict) -> dict:
    if not role_can_create_batch_inspection(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    existing = one(conn, "SELECT * FROM inspection_batches WHERE plan_id=? AND tenant_id=?", (plan["plan_id"], user["tenant_id"]))
    if existing:
        batch = serialize_inspection_batch(conn, existing)
        return {
            "deduped": True,
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "inspection_batch": batch,
            "message": f"多门店即时巡检已经执行过：{batch['total_store_count']} 家门店，成功 {batch['success_store_count']} 家，跳过 {batch['skipped_store_count']} 家，失败 {batch['failed_store_count']} 家。",
            "audit_action": "inspection_batch.execute",
        }
    action = plan["actions"][0]
    params = action.get("params") or {}
    store_tasks = params.get("store_tasks") or []
    if not store_tasks:
        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "批量即时巡检缺少门店范围"})

    allowed = allowed_org_ids(conn, user)
    denied = [item.get("org_id") for item in store_tasks if item.get("org_id") not in allowed]
    if denied:
        raise ApiError("TENANT_SCOPE_DENIED", HTTPStatus.FORBIDDEN, {"denied_org_ids": denied})

    ts = now_iso()
    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    inspection_goal = params.get("inspection_goal") or plan.get("slots", {}).get("inspection_goal")
    scope_snapshot = {
        "summary": plan.get("summary"),
        "org_scope": plan.get("slots", {}).get("org_scope", {}),
        "camera_scope": {
            key: value
            for key, value in (plan.get("slots", {}).get("camera_scope", {}) or {}).items()
            if key != "store_tasks"
        },
        "schedule": params.get("schedule") or {"mode": "one_off", "label": "立即执行一次"},
        "inspection_goal": inspection_goal,
        "failure_policy": params.get("failure_policy") or "PARTIAL_SUCCESS_WITH_STORE_LEVEL_RETRY",
    }
    conn.execute(
        """
        INSERT INTO inspection_batches(
          batch_id, tenant_id, plan_id, conversation_id, intent, scope_snapshot, execution_mode,
          status, total_store_count, success_store_count, failed_store_count, skipped_store_count,
          created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            user["tenant_id"],
            plan["plan_id"],
            plan["conversation_id"],
            plan["intent"],
            json_dumps(scope_snapshot),
            "immediate",
            "RUNNING",
            len(store_tasks),
            0,
            0,
            0,
            user["user_id"],
            ts,
            ts,
        ),
    )
    success_count = 0
    failed_count = 0
    skipped_count = 0
    anomaly_count = 0
    for store_task in store_tasks:
        item_id = f"batch_item_{uuid.uuid4().hex[:12]}"
        camera_ids = store_task.get("camera_ids") or []
        camera_names = store_task.get("camera_names") or []
        scheduled_task_id = None
        run_ids = []
        status = "SUCCEEDED"
        failure_code = None
        if not camera_ids:
            status = "SKIPPED"
            failure_code = "NO_ONLINE_CAMERA"
            skipped_count += 1
        else:
            child_params = {
                "org_id": store_task.get("org_id"),
                "org_name": store_task.get("org_name"),
                "camera_ids": camera_ids,
                "camera_names": camera_names,
                "inspection_goal": inspection_goal,
                "schedule": params.get("schedule") or {"mode": "one_off", "label": "立即执行一次"},
                "start_at": params.get("start_at") or ts,
                "end_at": params.get("end_at") or ts,
                "thresholds": params.get("thresholds") or {"confidence": 0.8},
            }
            try:
                task = create_immediate_inspection_task(conn, user, plan, child_params, batch_id=batch_id)
                scheduled_task_id = task["task_id"]
                run = execute_immediate_inspection_task(conn, user, task, inspection_goal)
                run_ids = [run["run_id"]]
                if run.get("status") in {"SUCCEEDED", "PARTIAL"}:
                    success_count += 1
                    if run.get("result_status") == "POSITIVE":
                        anomaly_count += 1
                else:
                    status = "FAILED"
                    failure_code = run.get("error_message") or run.get("result_status") or "RUN_FAILED"
                    failed_count += 1
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "inspection_batch.item.execute",
                    "scheduled_inspection",
                    scheduled_task_id,
                    None,
                    {
                        "org_id": child_params["org_id"],
                        "camera_count": len(camera_ids),
                        "run_id": run.get("run_id"),
                        "run_status": run.get("status"),
                        "result_status": run.get("result_status"),
                        "batch_id": batch_id,
                    },
                    "batch",
                    plan["plan_id"],
                )
            except Exception as exc:
                status = "FAILED"
                failure_code = getattr(exc, "code", None) or str(exc)[:120] or exc.__class__.__name__
                failed_count += 1
        conn.execute(
            """
            INSERT INTO inspection_batch_items(
              item_id, batch_id, store_id, store_name, camera_ids, camera_names, status,
              failure_code, retry_count, subscription_id, scheduled_task_id, run_ids, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id,
                batch_id,
                store_task.get("org_id"),
                store_task.get("org_name"),
                json_dumps(camera_ids),
                json_dumps(camera_names),
                status,
                failure_code,
                0,
                None,
                scheduled_task_id,
                json_dumps(run_ids),
                ts,
                now_iso(),
            ),
        )
    final_status = "SUCCEEDED" if success_count == len(store_tasks) else "PARTIAL_SUCCESS" if success_count else "FAILED"
    conn.execute(
        """
        UPDATE inspection_batches
        SET status=?, success_store_count=?, failed_store_count=?, skipped_store_count=?, updated_at=?
        WHERE batch_id=?
        """,
        (final_status, success_count, failed_count, skipped_count, now_iso(), batch_id),
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "inspection_batch.execute",
        "inspection_batch",
        batch_id,
        None,
        {
            "store_count": len(store_tasks),
            "success_store_count": success_count,
            "failed_store_count": failed_count,
            "skipped_store_count": skipped_count,
            "anomaly_store_count": anomaly_count,
            "execution_mode": "immediate",
        },
        "chat",
        plan["plan_id"],
    )
    batch = serialize_inspection_batch(conn, one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch_id,)))
    status_words = f"成功 {success_count} 家，跳过 {skipped_count} 家，失败 {failed_count} 家"
    anomaly_words = f"，发现异常 {anomaly_count} 家" if anomaly_count else ""
    return {
        "batch_id": batch_id,
        "status": final_status,
        "inspection_batch": batch,
        "artifact": {"batchInspection": batch},
        "agent": {
            "intent": "BATCH_INSPECTION_EXECUTE",
            "skill": "multi_store_visual_inspection",
            "engine": "batch_visual_executor",
            "status": "SUCCEEDED" if success_count else "BLOCKED",
            "tool_calls": ["batch_inspection.execute", "paas.media.snapshot", "evidence.archive", "vlm.image.inspect", "scheduler.run.persist"],
        },
        "message": f"多门店即时巡检已完成：共 {len(store_tasks)} 家门店，{status_words}{anomaly_words}。所有快照和模型结论已归档，可在告警与证据中追溯。",
        "audit_action": "inspection_batch.execute",
    }


def execute_batch_scheduled_inspection_plan(conn, user: dict, plan: dict) -> dict:
    if not role_can_create_batch_inspection(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    existing = one(conn, "SELECT * FROM inspection_batches WHERE plan_id=? AND tenant_id=?", (plan["plan_id"], user["tenant_id"]))
    if existing:
        if existing["status"] == "CANCELLED":
            requeued = requeue_cancelled_inspection_batch(conn, user, plan, existing)
            if requeued:
                batch = requeued["inspection_batch"]
                return {
                    "deduped": True,
                    "requeued": True,
                    "batch_id": batch["batch_id"],
                    "status": batch["status"],
                    "inspection_batch": batch,
                    "message": (
                        f"已恢复已取消的多门店周期巡检并排队首轮执行：恢复 {requeued['retried_store_count']} 家，"
                        f"仍需处理 {requeued['still_failed_store_count']} 家。首轮快照会由调度器立即拉取并回写分析结果。"
                    ),
                    "audit_action": "inspection_batch.retry",
                }
        repaired = ensure_batch_scheduled_tasks_queued(conn, user, plan, existing)
        if repaired:
            batch = repaired["inspection_batch"]
            return {
                "deduped": True,
                "schedule_repaired": True,
                "batch_id": batch["batch_id"],
                "status": batch["status"],
                "inspection_batch": batch,
                "message": (
                    f"已校准多门店周期巡检调度状态：恢复 {repaired['repaired_store_count']} 家，"
                    f"补建 {repaired['created_store_count']} 家。首轮快照会由调度器立即拉取并回写模型分析结果。"
                ),
                "audit_action": "inspection_batch.schedule_repair",
            }
        batch = serialize_inspection_batch(conn, existing)
        return {
            "deduped": True,
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "inspection_batch": batch,
            "message": f"批量巡检已经创建过：{batch['total_store_count']} 家门店，成功 {batch['success_store_count']} 家，跳过 {batch['skipped_store_count']} 家。",
            "audit_action": "inspection_batch.create",
        }
    action = plan["actions"][0]
    params = action.get("params") or {}
    store_tasks = params.get("store_tasks") or []
    if not store_tasks:
        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "批量巡检缺少门店范围"})

    allowed = allowed_org_ids(conn, user)
    denied = [item.get("org_id") for item in store_tasks if item.get("org_id") not in allowed]
    if denied:
        raise ApiError("TENANT_SCOPE_DENIED", HTTPStatus.FORBIDDEN, {"denied_org_ids": denied})

    ts = now_iso()
    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    scope_snapshot = {
        "summary": plan.get("summary"),
        "org_scope": plan.get("slots", {}).get("org_scope", {}),
        "camera_scope": {
            key: value
            for key, value in (plan.get("slots", {}).get("camera_scope", {}) or {}).items()
            if key != "store_tasks"
        },
        "schedule": params.get("schedule") or {},
        "inspection_goal": params.get("inspection_goal") or plan.get("slots", {}).get("inspection_goal"),
        "start_at": params.get("start_at"),
        "end_at": params.get("end_at"),
        "thresholds": params.get("thresholds") or {"confidence": 0.8},
        "failure_policy": params.get("failure_policy") or "PARTIAL_SUCCESS_WITH_STORE_LEVEL_RETRY",
    }
    conn.execute(
        """
        INSERT INTO inspection_batches(
          batch_id, tenant_id, plan_id, conversation_id, intent, scope_snapshot, execution_mode,
          status, total_store_count, success_store_count, failed_store_count, skipped_store_count,
          created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            user["tenant_id"],
            plan["plan_id"],
            plan["conversation_id"],
            plan["intent"],
            json_dumps(scope_snapshot),
            params.get("execution_mode") or "scheduled_with_first_run",
            "RUNNING",
            len(store_tasks),
            0,
            0,
            0,
            user["user_id"],
            ts,
            ts,
        ),
    )
    execution_mode = params.get("execution_mode") or "scheduled_with_first_run"
    force_first_run = bool(params.get("force_first_run")) or execution_mode == "scheduled_with_first_run"
    success_count = 0
    failed_count = 0
    skipped_count = 0
    pending_count = 0
    for store_task in store_tasks:
        item_id = f"batch_item_{uuid.uuid4().hex[:12]}"
        camera_ids = store_task.get("camera_ids") or []
        camera_names = store_task.get("camera_names") or []
        scheduled_task_id = None
        status = "RUNNING" if force_first_run else "SUCCEEDED"
        failure_code = None
        if not camera_ids:
            status = "SKIPPED"
            failure_code = "NO_ONLINE_CAMERA"
            skipped_count += 1
        else:
            child_params = {
                "org_id": store_task.get("org_id"),
                "org_name": store_task.get("org_name"),
                "camera_ids": camera_ids,
                "camera_names": camera_names,
                "inspection_goal": params.get("inspection_goal"),
                "schedule": params.get("schedule") or {},
                "start_at": params.get("start_at"),
                "end_at": params.get("end_at"),
                "thresholds": params.get("thresholds") or {"confidence": 0.8},
                "force_first_run": force_first_run,
            }
            try:
                task = create_scheduled_inspection_task(
                    conn,
                    user,
                    plan,
                    child_params,
                    batch_id=batch_id,
                    force_first_run=force_first_run,
                )
                scheduled_task_id = task["task_id"]
                if force_first_run:
                    pending_count += 1
                else:
                    success_count += 1
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "scheduled_inspection.create",
                    "scheduled_inspection",
                    scheduled_task_id,
                    None,
                    {
                        "org_id": child_params["org_id"],
                        "camera_count": len(camera_ids),
                        "schedule": child_params["schedule"],
                        "batch_id": batch_id,
                    },
                    "batch",
                    plan["plan_id"],
                )
            except Exception as exc:
                status = "FAILED"
                failure_code = getattr(exc, "code", None) or exc.__class__.__name__
                failed_count += 1
        conn.execute(
            """
            INSERT INTO inspection_batch_items(
              item_id, batch_id, store_id, store_name, camera_ids, camera_names, status,
              failure_code, retry_count, subscription_id, scheduled_task_id, run_ids, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id,
                batch_id,
                store_task.get("org_id"),
                store_task.get("org_name"),
                json_dumps(camera_ids),
                json_dumps(camera_names),
                status,
                failure_code,
                0,
                None,
                scheduled_task_id,
                json_dumps([]),
                ts,
                now_iso(),
            ),
        )
    if pending_count:
        final_status = "RUNNING"
    elif success_count == len(store_tasks):
        final_status = "SUCCEEDED"
    elif success_count:
        final_status = "PARTIAL_SUCCESS"
    else:
        final_status = "FAILED"
    conn.execute(
        """
        UPDATE inspection_batches
        SET status=?, success_store_count=?, failed_store_count=?, skipped_store_count=?, updated_at=?
        WHERE batch_id=?
        """,
        (final_status, success_count, failed_count, skipped_count, now_iso(), batch_id),
    )
    log_audit(
        conn,
        user["user_id"],
        user["tenant_id"],
        "inspection_batch.create",
        "inspection_batch",
        batch_id,
        None,
        {
            "store_count": len(store_tasks),
            "success_store_count": success_count,
            "failed_store_count": failed_count,
            "skipped_store_count": skipped_count,
            "pending_store_count": pending_count,
            "execution_mode": params.get("execution_mode") or "scheduled_with_first_run",
        },
        "chat",
        plan["plan_id"],
    )
    batch = serialize_inspection_batch(conn, one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch_id,)))
    return {
        "batch_id": batch_id,
        "status": final_status,
        "inspection_batch": batch,
        "message": (
            f"多门店周期巡检已创建并排队首轮执行：共 {len(store_tasks)} 家门店，"
            f"已排队 {pending_count} 家，已完成配置 {success_count} 家，跳过 {skipped_count} 家，失败 {failed_count} 家。"
            "调度器会立即拉取首轮快照并回写分析结果。"
        ),
        "audit_action": "inspection_batch.create",
    }


def execute_subscription_plan(conn, user, plan: dict) -> dict:
    if not role_can_create_subscription(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    action = plan["actions"][0]
    params = action["params"]
    camera_ids = params["camera_ids"]
    cameras = rows(conn, f"SELECT * FROM cameras WHERE camera_id IN ({','.join('?' for _ in camera_ids)})", camera_ids)
    assert_org_access(conn, user, [c["org_id"] for c in cameras])
    capability = one(conn, "SELECT * FROM capabilities WHERE capability_id=?", (params["capability_id"],))
    subscription_id = f"sub_{uuid.uuid4().hex[:10]}"
    subscription_name = f"{org_label(conn, params['org_id'])}-{capability['name']}"
    conn.execute(
        "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            subscription_id,
            user["tenant_id"],
            params["org_id"],
            subscription_name,
            params["app_id"],
            params["app_version_id"],
            params["capability_id"],
            json_dumps(camera_ids),
            json_dumps(params["schedule"]),
            params["valid_from"],
            params["valid_to"],
            json_dumps(params["thresholds"]),
            json_dumps(params["dedupe_policy"]),
            "ACTIVE",
            "CHAT",
            plan["plan_id"],
            user["user_id"],
            now_iso(),
        ),
    )
    after = {"subscription_id": subscription_id, "camera_count": len(camera_ids), "status": "ACTIVE"}
    log_audit(conn, user["user_id"], user["tenant_id"], "subscription.create", "subscription", subscription_id, None, after, "chat", plan["plan_id"])
    return {
        "subscription_id": subscription_id,
        "status": "ACTIVE",
        "message": f"订阅已创建并生效：{subscription_name}，已关联 {len(camera_ids)} 路在线摄像头。",
        "audit_action": "subscription.create",
    }


def execute_feedback_plan(conn, user, plan: dict) -> dict:
    action = plan["actions"][0]
    return create_feedback(conn, user, action["params"], plan["plan_id"], "chat")


def create_feedback(conn, user, params: dict, plan_id: str | None, source: str) -> dict:
    if not role_can_feedback(user["role"]):
        raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
    event = one(conn, "SELECT * FROM events WHERE event_id=?", (params["event_id"],))
    if not event:
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"event_id": params["event_id"]})
    assert_org_access(conn, user, [event["org_id"]])
    before = serialize_event(conn, event)
    feedback_id = f"fb_{uuid.uuid4().hex[:10]}"
    feedback_type = params.get("feedback_type", "FALSE_POSITIVE")
    reason = params.get("reason") or "用户反馈"
    evidence_ids = json_loads(event["evidence_ids"], [])
    badcase_id = f"bc_{uuid.uuid4().hex[:10]}" if feedback_type in {"FALSE_POSITIVE", "FALSE_NEGATIVE"} else None
    conn.execute(
        "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?)",
        (feedback_id, event["event_id"], feedback_type, reason, params.get("description"), evidence_ids[0] if evidence_ids else None, user["user_id"], "CONFIRMED", badcase_id, now_iso()),
    )
    new_status = feedback_type if feedback_type in {"TRUE_POSITIVE", "FALSE_POSITIVE", "IGNORED"} else event["status"]
    conn.execute("UPDATE events SET status=? WHERE event_id=?", (new_status, event["event_id"]))
    after_event = one(conn, "SELECT * FROM events WHERE event_id=?", (event["event_id"],))
    after = serialize_event(conn, after_event)
    log_audit(conn, user["user_id"], user["tenant_id"], "event.feedback.create", "event", event["event_id"], before, after, source, plan_id)
    return {
        "feedback_id": feedback_id,
        "event_id": event["event_id"],
        "status": new_status,
        "badcase_id": badcase_id,
        "message": f"已将事件 {event['event_id']} 标记为{feedback_label(new_status)}，反馈记录 {feedback_id} 已写入。",
    }


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)


def next_scheduled_time(task: dict, previous_at: datetime) -> datetime | None:
    schedule = json_loads(task.get("schedule"), {})
    interval = int(schedule.get("interval_minutes") or 0)
    end_at = parse_iso_datetime(task["end_at"])
    window = schedule.get("daily_window") or {"mode": "all_day", "start_time": "00:00", "end_time": "24:00"}
    if window.get("mode") == "fixed_daily":
        fixed_hour, fixed_minute = (int(item) for item in window.get("fixed_time", "09:00").split(":"))
        candidate = previous_at.replace(hour=fixed_hour, minute=fixed_minute, second=0, microsecond=0)
        if candidate <= previous_at:
            candidate += timedelta(days=1)
        return candidate if candidate < end_at else None
    if interval <= 0:
        return None
    candidate = previous_at + timedelta(minutes=interval)
    if window.get("mode") != "all_day":
        start_hour, start_minute = (int(item) for item in window.get("start_time", "09:00").split(":"))
        end_hour, end_minute = (int(item) for item in window.get("end_time", "22:00").split(":"))
        day_start = candidate.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        day_end = candidate.replace(hour=min(end_hour, 23), minute=end_minute if end_hour < 24 else 59, second=0, microsecond=0)
        if candidate < day_start:
            candidate = day_start
        elif candidate > day_end:
            candidate = (day_start + timedelta(days=1)).replace(hour=start_hour, minute=start_minute)
    return candidate if candidate < end_at else None


def archive_scheduled_snapshot(conn, task: dict, run_id: str, media: dict) -> tuple[dict, dict]:
    source_url = str(media.get("snapshot_url") or "")
    if not source_url:
        raise OnlineAgentError("VISUAL_EVIDENCE_MISSING", "摄像头没有返回快照地址")
    request_obj = urlrequest.Request(source_url, headers={"User-Agent": "WanxiangAGIInspection/0.3", "Accept": "image/*"})
    last_error = None
    content = b""
    mime_type = "image/jpeg"
    for attempt in range(2):
        try:
            with urlrequest.urlopen(request_obj, timeout=20) as response:
                content = response.read(12 * 1024 * 1024 + 1)
                mime_type = (response.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            break
        except (urlerror.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
    if not content:
        raise OnlineAgentError("UPSTREAM_UNAVAILABLE", "监控快照归档失败") from last_error
    if not content or len(content) > 12 * 1024 * 1024:
        raise OnlineAgentError("UPSTREAM_INVALID_RESPONSE", "监控快照大小异常")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        mime_type = "image/jpeg"
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime_type]
    evidence_id = f"se_{uuid.uuid4().hex[:16]}"
    access_token = uuid.uuid4().hex + uuid.uuid4().hex
    folder = SCHEDULED_EVIDENCE_DIR / task["task_id"] / run_id
    folder.mkdir(parents=True, exist_ok=True)
    storage_path = folder / f"{evidence_id}{extension}"
    storage_path.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    captured_at = str(media.get("captured_at") or now_iso())
    conn.execute(
        "INSERT INTO scheduled_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            run_id,
            task["task_id"],
            task["org_id"],
            task["org_name"],
            media.get("camera_id"),
            media.get("camera_name"),
            captured_at,
            str(storage_path),
            mime_type,
            sha256,
            access_token,
            len(content),
            now_iso(),
        ),
    )
    evidence = one(conn, "SELECT * FROM scheduled_evidence WHERE evidence_id=?", (evidence_id,))
    model_image = {
        "org_id": task["org_id"],
        "org_name": task["org_name"],
        "camera_id": media.get("camera_id"),
        "camera_name": media.get("camera_name"),
        "captured_at": captured_at,
        "snapshot_url": f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}",
        "evidence_id": evidence_id,
        "sha256": sha256,
    }
    return evidence, model_image


def scheduled_run_artifact(task: dict, run: dict, evidence: list[dict]) -> dict:
    anomaly_evidence_ids = set(json_loads(run.get("anomaly_evidence_ids"), []))
    if str(run.get("result_status") or "").upper() == "POSITIVE":
        recovered_risk_names = set(sku_risk_camera_names_from_run_trace(run, evidence))
        anomaly_evidence_ids.update(
            item["evidence_id"]
            for item in evidence
            if str(item.get("camera_name") or "") in recovered_risk_names
        )
    anomaly_reason = run.get("conclusion") or run.get("business_reason")
    sku_matches = validated_run_sku_matches(json_loads(run.get("sku_matches_json"), []), evidence)
    failed_camera_names = set(failed_camera_names_from_run_trace(run, evidence))
    knowledge_hits = inspection_knowledge_hits(task)
    return {
        "task_id": task["task_id"],
        "task_name": task["name"],
        "run_id": run["run_id"],
        "status": run["status"],
        "result_status": run.get("result_status"),
        "inspection_goal": task["inspection_goal"],
        "scheduled_at": run["scheduled_at"],
        "started_at": run["started_at"],
        "completed_at": run.get("completed_at"),
        "conclusion": run.get("conclusion"),
        "confidence": run.get("confidence"),
        "business_reason": run.get("business_reason"),
        "observations": json_loads(run.get("observations"), []),
        "error_message": run.get("error_message"),
        "knowledge_context": knowledge_hits,
        "knowledge_titles": [item["title"] for item in knowledge_hits],
        "sku_matches": sku_matches,
        "evidence": [
            serialize_scheduled_evidence(
                item,
                anomaly_evidence_ids,
                anomaly_reason,
                sku_labels_for_evidence(sku_matches, item),
                str(item.get("camera_name") or "") in failed_camera_names,
            )
            for item in evidence
        ],
        "anomaly_evidence_ids": sorted(anomaly_evidence_ids),
        "failed_camera_names": sorted(failed_camera_names),
        "image_count": len(evidence),
        "model_version": run.get("model_version"),
        "timing": inspection_run_timing(run, evidence),
        "trace": json_loads(run.get("trace_json"), {}),
    }


def complete_scheduled_run(task_id: str, run_id: str, message_id: str, result: dict | None, error_message: str | None, partial: bool):
    with connect() as conn:
        task = one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,))
        run = one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,))
        if not task or not run:
            return
        evidence_ids = json_loads(run.get("evidence_ids"), [])
        evidence_rows = rows(
            conn,
            f"SELECT * FROM scheduled_evidence WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)}) ORDER BY captured_at",
            evidence_ids,
        ) if evidence_ids else []
        completed_at = now_iso()
        if result:
            model_partial_note = visual_analysis_partial_note(result)
            if model_partial_note:
                partial = True
                error_message = "；".join(
                    dict.fromkeys(item for item in (error_message, model_partial_note) if item)
                )[:500]
            result_status = str(result.get("status") or "UNCERTAIN")
            run_status = "PARTIAL" if partial else "SUCCEEDED"
            conclusion = str(result.get("conclusion") or "已完成周期巡检。")[:1000]
            confidence = float(result.get("confidence") or 0)
            business_reason = str(result.get("business_reason") or "")[:500]
            observations = result.get("observations") if isinstance(result.get("observations"), list) else []
            if model_partial_note:
                observations = list(dict.fromkeys([*observations, model_partial_note]))[:20]
            anomaly_camera_names = {
                str(item) for item in (result.get("anomaly_camera_names") or []) if str(item)
            }
            if result_status == "POSITIVE" and not anomaly_camera_names:
                anomaly_camera_names = {
                    str(item) for item in (result.get("selected_camera_names") or []) if str(item)
                }
            anomaly_evidence_ids = [
                item["evidence_id"]
                for item in evidence_rows
                if result_status == "POSITIVE" and item.get("camera_name") in anomaly_camera_names
            ]
            sku_matches = validated_run_sku_matches(result.get("sku_matches"), evidence_rows)
            model_version = str(result.get("model") or "")[:200]
        else:
            result_status = "UNCERTAIN"
            run_status = "FAILED"
            conclusion = "本次周期巡检执行失败，已保留成功抓取的快照。"
            confidence = 0.0
            business_reason = "执行失败，不能形成正常或异常结论。"
            observations = []
            anomaly_evidence_ids = []
            sku_matches = []
            model_version = None
        conn.execute(
            """UPDATE inspection_runs
               SET completed_at=?, status=?, result_status=?, conclusion=?, confidence=?, business_reason=?,
                   observations=?, anomaly_evidence_ids=?, sku_matches_json=?, model_version=?, error_message=? WHERE run_id=?""",
            (
                completed_at,
                run_status,
                result_status,
                conclusion,
                confidence,
                business_reason,
                json_dumps(observations),
                json_dumps(anomaly_evidence_ids),
                json_dumps(sku_matches),
                model_version,
                error_message,
                run_id,
            ),
        )
        previous_at = parse_iso_datetime(run["scheduled_at"])
        if task["status"] in {"PAUSED", "CANCELLED"}:
            next_at = None
            next_status = task["status"]
        else:
            next_at = next_scheduled_time(task, previous_at)
            next_status = "ACTIVE" if next_at else "COMPLETED"
        anomaly_increment = 1 if result_status == "POSITIVE" else 0
        uncertain_increment = 1 if result_status == "UNCERTAIN" else 0
        conn.execute(
            """UPDATE scheduled_inspections
               SET next_run_at=?, last_run_at=?, status=?, run_count=run_count+1,
                   anomaly_count=anomaly_count+?, uncertain_count=uncertain_count+?, updated_at=?
               WHERE task_id=?""",
            (
                next_at.isoformat(timespec="seconds") if next_at else None,
                completed_at,
                next_status,
                anomaly_increment,
                uncertain_increment,
                completed_at,
                task_id,
            ),
        )
        sync_batch_item_after_scheduled_run(
            conn,
            task,
            run_id,
            run_status,
            result_status,
            error_message,
            completed_at,
        )
        updated_run = one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,))
        artifact = scheduled_run_artifact(task, updated_run, evidence_rows)
        knowledge_hits = inspection_knowledge_hits(task)
        tool_calls = ["paas.media.snapshot", "evidence.archive"]
        if knowledge_hits:
            tool_calls.append("knowledge.retrieve")
        tool_calls.extend(["vlm.image.inspect" if result else "vlm.image.inspect:failed", "scheduler.run.persist"])
        linked = {
            "artifact": {"scheduledRun": artifact},
            "agent": {
                "intent": "CREATE_SCHEDULED_INSPECTION",
                "skill": "scheduled_snapshot_inspection",
                "tenant_id": task["tenant_id"],
                "confidence": 1.0,
                "engine": "scheduled_visual_executor",
                "status": "SUCCEEDED",
                "tool_calls": tool_calls,
                "knowledge_hits": knowledge_hits,
            },
            "source": "scheduled_inspection",
        }
        trace_artifact = {"scheduledRun": artifact, "knowledgeHits": knowledge_hits}
        if isinstance(result, dict):
            trace_artifact["visualResult"] = result
        attach_agent_trace(linked, task["inspection_goal"], trace_artifact)
        trace_json = linked.get("agent", {}).get("trace") or {}
        artifact["trace"] = trace_json
        linked["artifact"]["scheduledRun"] = artifact
        conn.execute("UPDATE inspection_runs SET trace_json=? WHERE run_id=?", (json_dumps(trace_json), run_id))
        conn.execute(
            "UPDATE messages SET content=?, linked_object=?, created_at=? WHERE message_id=?",
            (conclusion, json_dumps(linked), completed_at, message_id),
        )
        conn.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", (completed_at, task["conversation_id"]))
        log_audit(
            conn,
            task["user_id"],
            task["tenant_id"],
            "scheduled_inspection.run.complete" if result else "scheduled_inspection.run.failed",
            "inspection_run",
            run_id,
            None,
            {"result_status": result_status, "evidence_count": len(evidence_rows), "sha256": [item["sha256"] for item in evidence_rows]},
            "scheduler",
            task.get("plan_id"),
        )
        conn.commit()


def execute_scheduled_run(task_id: str, run_id: str):
    message_id = ""
    partial_errors = []
    model_images = []
    evidence_rows = []
    try:
        with connect() as conn:
            task = one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,))
        if not task:
            return
        with connect() as conn:
            online = online_agent_for_tenant(conn, task["tenant_id"])
        if not online:
            complete_scheduled_run(task_id, run_id, "", None, "DeepVision 在线服务未连接", False)
            return
        camera_ids = json_loads(task["camera_ids"], [])
        snapshots = online.capture_scheduled_snapshots(task["org_id"], camera_ids)
        captured_ids = {str(item.get("camera_id") or "") for item in snapshots}
        camera_names = json_loads(task["camera_names"], [])
        for index, camera_id in enumerate(camera_ids):
            if camera_id not in captured_ids:
                camera_name = camera_names[index] if index < len(camera_names) else camera_id
                partial_errors.append(f"{camera_name}：本轮抓图失败")
        with connect() as conn:
            for media in snapshots:
                try:
                    evidence, model_image = archive_scheduled_snapshot(conn, task, run_id, media)
                    evidence_rows.append(evidence)
                    model_images.append(model_image)
                except OnlineAgentError as exc:
                    partial_errors.append(f"{media.get('camera_name', '未知镜头')}：{exc.message}")
            if not model_images:
                raise OnlineAgentError("VISUAL_EVIDENCE_MISSING", "所有摄像头快照均归档失败")
            knowledge_hits = resolve_inspection_knowledge_context(conn, task, task["inspection_goal"])
            model_question = inspection_question_with_knowledge(task["inspection_goal"], knowledge_hits)
            reference_images = inspection_reference_images(task["tenant_id"], knowledge_hits)
            evidence_ids = [item["evidence_id"] for item in evidence_rows]
            conn.execute("UPDATE inspection_runs SET evidence_ids=? WHERE run_id=?", (json_dumps(evidence_ids), run_id))
            analyzing_run = one(conn, "SELECT * FROM inspection_runs WHERE run_id=?", (run_id,))
            artifact = scheduled_run_artifact(task, analyzing_run, evidence_rows)
            linked = {"artifact": {"scheduledRun": artifact}, "source": "scheduled_inspection"}
            message = add_message(
                conn,
                task["conversation_id"],
                "assistant",
                (
                    f"已按计划抓取 {len(evidence_rows)} 张监控快照，正在进行 AI 巡检分析。"
                    f"{inspection_knowledge_summary(knowledge_hits)}"
                ),
                task.get("plan_id"),
                linked,
            )
            message_id = message["message_id"]
            conn.commit()
        result = None
        for attempt in range(1, 3):
            try:
                result = online.analyze_scheduled_snapshots(model_question, model_images, reference_images)
                break
            except OnlineAgentError:
                with connect() as conn:
                    conn.execute("UPDATE inspection_runs SET attempt=? WHERE run_id=?", (attempt, run_id))
                    conn.commit()
                if attempt == 2:
                    raise
                time.sleep(2)
        if result is None:
            raise OnlineAgentError("VLM_UNAVAILABLE", "视觉分析服务未返回结果")
        complete_scheduled_run(task_id, run_id, message_id, result, "；".join(partial_errors) or None, bool(partial_errors))
    except Exception as exc:  # noqa: BLE001
        error_message = inspection_execution_error_message(exc)
        if not message_id:
            with connect() as conn:
                task = one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,))
                if task:
                    message = add_message(
                        conn,
                        task["conversation_id"],
                        "assistant",
                        "本次周期巡检执行失败，正在记录失败原因。",
                        task.get("plan_id"),
                        {"source": "scheduled_inspection"},
                    )
                    message_id = message["message_id"]
                    conn.commit()
        complete_scheduled_run(task_id, run_id, message_id, None, error_message[:500], bool(evidence_rows))

SCHEDULED_INSPECTION_SEED_LIMIT = 50
SCHEDULED_INSPECTION_CLAIM_LIMIT = 240


def due_scheduled_inspection_tasks(
    conn: sqlite3.Connection,
    due_at: str,
    seed_limit: int = SCHEDULED_INSPECTION_SEED_LIMIT,
    claim_limit: int = SCHEDULED_INSPECTION_CLAIM_LIMIT,
) -> list[dict]:
    seed = rows(
        conn,
        """SELECT * FROM scheduled_inspections
           WHERE status='ACTIVE' AND next_run_at IS NOT NULL AND next_run_at<=?
           ORDER BY next_run_at, created_at, task_id
           LIMIT ?""",
        (due_at, seed_limit),
    )
    if not seed:
        return []

    by_task_id = {task["task_id"]: task for task in seed}
    batch_ids = sorted({task.get("batch_id") for task in seed if task.get("batch_id")})
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        sibling_due = rows(
            conn,
            f"""SELECT * FROM scheduled_inspections
                WHERE status='ACTIVE' AND next_run_at IS NOT NULL AND next_run_at<=?
                  AND batch_id IN ({placeholders})
                ORDER BY next_run_at, created_at, task_id""",
            [due_at, *batch_ids],
        )
        for task in sibling_due:
            by_task_id.setdefault(task["task_id"], task)

    ordered = sorted(
        by_task_id.values(),
        key=lambda task: (task.get("next_run_at") or "", task.get("created_at") or "", task.get("task_id") or ""),
    )
    return ordered[: max(1, claim_limit)]


class ScheduledInspectionWorker:
    def __init__(self, poll_seconds: int = 5):
        self.poll_seconds = max(1, poll_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scheduled-inspection-worker", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[scheduler] {exc}\n")
            self._stop.wait(self.poll_seconds)

    def tick(self):
        now = now_iso()
        stale_before = (datetime.now(CN_TZ) - timedelta(minutes=15)).isoformat(timespec="seconds")
        stale_runs = []
        claimed = []
        with connect() as conn:
            stale = rows(
                conn,
                """SELECT r.run_id, r.task_id, s.conversation_id, s.plan_id
                   FROM inspection_runs r
                   JOIN scheduled_inspections s ON s.task_id=r.task_id
                   WHERE r.status='ANALYZING' AND r.started_at<?""",
                (stale_before,),
            )
            for item in stale:
                message = add_message(
                    conn,
                    item["conversation_id"],
                    "assistant",
                    "检测到上一次周期巡检执行中断，已自动恢复调度并记录本轮失败。",
                    item.get("plan_id"),
                    {"source": "scheduled_inspection_recovery"},
                )
                stale_runs.append((item["task_id"], item["run_id"], message["message_id"]))
            due = due_scheduled_inspection_tasks(conn, now)
            for task in due:
                scheduled_at = task["next_run_at"]
                run_id = f"run_{uuid.uuid4().hex[:12]}"
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO inspection_runs(
                         run_id, task_id, scheduled_at, started_at, completed_at, status, attempt,
                         result_status, conclusion, confidence, business_reason, observations,
                         evidence_ids, model_version, error_message, created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        task["task_id"],
                        scheduled_at,
                        now,
                        None,
                        "ANALYZING",
                        1,
                        None,
                        "快照抓取中",
                        None,
                        None,
                        "[]",
                        "[]",
                        None,
                        None,
                        now,
                    ),
                )
                if cursor.rowcount:
                    conn.execute("UPDATE scheduled_inspections SET next_run_at=NULL, updated_at=? WHERE task_id=?", (now, task["task_id"]))
                    claimed.append((task["task_id"], run_id))
            conn.commit()
        for task_id, run_id, message_id in stale_runs:
            complete_scheduled_run(task_id, run_id, message_id, None, "执行进程中断或超时，调度器已自动恢复", False)
        for task_id, run_id in claimed:
            execute_scheduled_run(task_id, run_id)


def visible_scheduled_inspection_tasks(conn, user: dict) -> list[dict]:
    owner_filter = ""
    args = [user["tenant_id"]]
    if user.get("role") not in {"tenant_admin", "system_admin"}:
        owner_filter = "AND s.user_id=?"
        args.append(user["user_id"])
    return rows(
        conn,
        f"""
        SELECT s.*
        FROM scheduled_inspections s
        WHERE s.tenant_id=?
          {owner_filter}
          AND (
            s.batch_id IS NULL
            OR EXISTS (
              SELECT 1
              FROM inspection_batch_items i
              WHERE i.batch_id=s.batch_id
                AND i.scheduled_task_id=s.task_id
            )
        )
        ORDER BY s.created_at DESC, s.updated_at DESC
        """,
        args,
    )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "WanxiangAGIInspection/0.2"

    def log_message(self, fmt, *args):
        message = re.sub(r"(access_token=)[^&\s]+", r"\1[REDACTED]", fmt % args)
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), message))

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.add_cors()
        self.end_headers()

    def do_GET(self):
        try:
            self.route_get()
        except ApiError as exc:
            self.send_error_json(exc.code, exc.status, exc.detail)
        except OfficePolicyError as exc:
            self.send_error_json(exc.code, HTTPStatus.BAD_REQUEST)
        except OnlineAgentError as exc:
            status = HTTPStatus.NOT_FOUND if exc.code == "RESOURCE_NOT_FOUND" else HTTPStatus.BAD_REQUEST if exc.code == "BAD_REQUEST" else HTTPStatus.BAD_GATEWAY
            self.send_error_json(exc.code, status, {"message": exc.message, **exc.detail})
        except Exception as exc:
            self.send_error_json("INTERNAL_ERROR", HTTPStatus.INTERNAL_SERVER_ERROR, {"message": str(exc)})

    def do_POST(self):
        try:
            self.route_post()
        except ApiError as exc:
            self.send_error_json(exc.code, exc.status, exc.detail)
        except OfficePolicyError as exc:
            self.send_error_json(exc.code, HTTPStatus.BAD_REQUEST)
        except OnlineAgentError as exc:
            status = HTTPStatus.NOT_FOUND if exc.code == "RESOURCE_NOT_FOUND" else HTTPStatus.BAD_REQUEST if exc.code == "BAD_REQUEST" else HTTPStatus.BAD_GATEWAY
            self.send_error_json(exc.code, status, {"message": exc.message, **exc.detail})
        except Exception as exc:
            self.send_error_json("INTERNAL_ERROR", HTTPStatus.INTERNAL_SERVER_ERROR, {"message": str(exc)})

    def do_DELETE(self):
        try:
            self.route_delete()
        except ApiError as exc:
            self.send_error_json(exc.code, exc.status, exc.detail)
        except OfficePolicyError as exc:
            self.send_error_json(exc.code, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_error_json("INTERNAL_ERROR", HTTPStatus.INTERNAL_SERVER_ERROR, {"message": str(exc)})

    def do_PATCH(self):
        try:
            self.route_patch()
        except ApiError as exc:
            self.send_error_json(exc.code, exc.status, exc.detail)
        except Exception as exc:
            self.send_error_json("INTERNAL_ERROR", HTTPStatus.INTERNAL_SERVER_ERROR, {"message": str(exc)})

    def do_PUT(self):
        try:
            self.route_put()
        except ApiError as exc:
            self.send_error_json(exc.code, exc.status, exc.detail)
        except Exception as exc:
            self.send_error_json("INTERNAL_ERROR", HTTPStatus.INTERNAL_SERVER_ERROR, {"message": str(exc)})

    def add_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-User-Id, X-Tenant-Code, If-Match")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json_dumps(payload).encode("utf-8")
        self.send_response(status)
        self.add_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_private_file(self, path: Path, *, download_name: str, inline: bool = False):
        content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        disposition = "inline" if inline else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{Path(download_name).name}"')
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, code, status=HTTPStatus.BAD_REQUEST, detail=None):
        detail_payload = detail if isinstance(detail, dict) else {}
        message = detail_payload.get("message") or ERRORS.get(code, "系统异常")
        self.send_json({"ok": False, "error": {"code": code, "message": message, "detail": detail_payload}}, status)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "请求体长度格式错误"}) from exc
        if length == 0:
            return {}
        if length < 0 or length > MAX_JSON_BODY_BYTES:
            raise ApiError("BAD_REQUEST", HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"message": "请求内容不能超过 48MB"})
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "Invalid JSON"})

    def read_office_upload(self):
        """Stage Office uploads in bounded files, never a 120 MB body buffer.

        Binary and multipart browsers write to short-lived, mode-0600 staging
        files.  The Office asset service then validates every item before its
        first promotion.  JSON/base64 survives only as a small test-client
        compatibility contract and is deliberately capped below the Office
        batch limit.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "请求体长度格式错误"}) from exc
        if length <= 0 or length > MAX_OFFICE_UPLOAD_BODY_BYTES:
            raise ApiError("OFFICE_BATCH_SIZE_LIMIT_EXCEEDED", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw_content_type = str(self.headers.get("Content-Type") or "")
        content_type = raw_content_type.lower()
        if content_type.startswith("application/octet-stream"):
            filename = str(self.headers.get("X-File-Name") or "").strip()
            if not filename:
                raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "X-File-Name is required"})
            if length > MAX_FILE_BYTES:
                raise ApiError("OFFICE_FILE_TOO_LARGE", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return {"files": [(Path(filename).name, self._stage_request_bytes(length))]}
        if content_type.startswith("multipart/form-data"):
            boundary_match = re.search(r"boundary=([^;]+)", raw_content_type, flags=re.IGNORECASE)
            if not boundary_match:
                raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "multipart boundary is required"})
            boundary = boundary_match.group(1).strip().strip('"').encode("utf-8")
            return {"files": self._stage_multipart_upload(boundary, length)}
        if content_type.startswith("application/json"):
            if length > MAX_JSON_BODY_BYTES:
                raise ApiError("OFFICE_BATCH_SIZE_LIMIT_EXCEEDED", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "Invalid JSON"}) from exc
            file_rows = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(file_rows, list):
                file_rows = [payload] if isinstance(payload, dict) else []
            files = []
            for item in file_rows:
                if not isinstance(item, dict):
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "文件格式不正确"})
                try:
                    content = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "content_base64 无效"}) from exc
                files.append((str(item.get("filename") or ""), content))
            return {"files": files}
        raise ApiError("BAD_REQUEST", HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"message": "仅支持 binary、multipart 或 JSON/base64 上传"})

    @staticmethod
    def cleanup_office_upload_staging(files) -> None:
        for _filename, source in files or []:
            if isinstance(source, Path):
                source.unlink(missing_ok=True)

    def _new_office_upload_staging_file(self) -> tuple[Path, object]:
        OFFICE_UPLOAD_STAGING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(mode="wb", prefix="office-upload-", suffix=".part", dir=OFFICE_UPLOAD_STAGING_DIR, delete=False)
        try:
            os.chmod(handle.name, 0o600)
        except OSError:
            pass
        return Path(handle.name), handle

    def _stage_request_bytes(self, length: int) -> Path:
        path, handle = self._new_office_upload_staging_file()
        try:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "上传请求体不完整"})
                remaining -= len(chunk)
                handle.write(chunk)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            handle.close()

    def _stage_multipart_upload(self, boundary: bytes, length: int) -> list[tuple[str, Path]]:
        reader = _MultipartBodyReader(self.rfile, length)
        opening = b"--" + boundary + b"\r\n"
        if reader.read_exact(len(opening)) != opening:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "multipart 起始 boundary 无效"})
        delimiter = b"\r\n--" + boundary
        files: list[tuple[str, Path]] = []
        total_file_bytes = 0

        class _Discard:
            def write(self, _chunk):
                return None

        try:
            while True:
                header_bytes = reader.read_until(b"\r\n\r\n", max_bytes=64 * 1024)
                headers = (header_bytes or b"").decode("utf-8", errors="replace")
                filename_match = re.search(r'filename="([^\"]*)"', headers, flags=re.IGNORECASE)
                filename = Path(filename_match.group(1)).name if filename_match and filename_match.group(1) else ""
                path: Path | None = None
                handle = None
                sink = _Discard()
                if filename:
                    if len(files) >= MAX_BATCH_FILES:
                        raise ApiError("OFFICE_BATCH_FILE_LIMIT_EXCEEDED", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    path, handle = self._new_office_upload_staging_file()
                    sink = handle
                try:
                    reader.read_until(delimiter, sink=sink, max_bytes=MAX_FILE_BYTES if filename else None)
                finally:
                    if handle:
                        handle.close()
                suffix = reader.read_exact(2)
                if filename and path:
                    file_size = path.stat().st_size
                    total_file_bytes += file_size
                    if total_file_bytes > MAX_BATCH_BYTES:
                        path.unlink(missing_ok=True)
                        raise ApiError("OFFICE_BATCH_SIZE_LIMIT_EXCEEDED", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    files.append((filename, path))
                if suffix == b"--":
                    # RFC 7578 permits a final CRLF after the closing marker.
                    if reader.remaining:
                        reader.read_exact(min(2, reader.remaining))
                    break
                if suffix != b"\r\n":
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "multipart boundary 结束格式无效"})
            if not files:
                raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "未找到文件字段"})
            return files
        except Exception:
            self.cleanup_office_upload_staging(files)
            # A current part has not joined ``files`` when an error occurs.
            if "path" in locals() and path:
                path.unlink(missing_ok=True)
            raise

    def route_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.serve_static("index.html")
        if path.startswith("/static/"):
            return self.serve_static(path.removeprefix("/static/"))
        scheduled_evidence_match = re.match(r"^/api/scheduled-evidence/([^/]+)$", path)
        if scheduled_evidence_match:
            query = parse_qs(parsed.query)
            access_token = (query.get("access_token") or [""])[0]
            return self.serve_scheduled_evidence(scheduled_evidence_match.group(1), access_token)
        online_snapshot_match = re.match(r"^/api/online-snapshot-evidence/([^/]+)$", path)
        if online_snapshot_match:
            query = parse_qs(parsed.query)
            access_token = (query.get("access_token") or [""])[0]
            return self.serve_online_snapshot_evidence(online_snapshot_match.group(1), access_token)
        media_stream_match = re.match(r"^/api/media/sessions/([^/]+)/stream$", path)
        if media_stream_match:
            query = parse_qs(parsed.query)
            access_token = (query.get("access_token") or [""])[0]
            tenant_code = (query.get("tenant_code") or [""])[0]
            return self.api_media_stream(media_stream_match.group(1), access_token, tenant_code)
        with connect() as conn:
            user = user_from_request(self, conn)
            if path == "/api/agent/feature-flags":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": tenant_feature_flag_settings(conn, user["tenant_id"])})
            if path == "/api/open-research/source-policies":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                policies = [item.public_dict() for item in load_active_source_policies(conn)]
                return self.send_json({"ok": True, "data": {"policies": policies, "read_only": user["role"] != "system_admin"}})
            if path == "/api/open-research/entity-aliases":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                aliases = rows(
                    conn,
                    """SELECT alias_id, alias_text, canonical_entity, confidence, reason, status, created_at, updated_at
                       FROM open_research_entity_aliases
                       WHERE tenant_id=? ORDER BY updated_at DESC, alias_id DESC""",
                    (user["tenant_id"],),
                )
                return self.send_json({"ok": True, "data": {"aliases": aliases}})
            if path == "/api/open-research/records":
                query = {key: (value[0] if value else "") for key, value in parse_qs(parsed.query).items()}
                return self.send_json({"ok": True, "data": list_open_research_history(conn, user, query)})
            research_record_match = re.match(r"^/api/open-research/records/([^/]+)$", path)
            if research_record_match:
                record = get_open_research_history_record(conn, user, research_record_match.group(1))
                if not record:
                    # Deliberately indistinguishable from a non-existent run.
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"record": record}})
            research_run_match = re.match(r"^/api/open-research/runs/([^/]+)$", path)
            if research_run_match:
                result = open_research_service_for_request(conn, user).get_run(run_id=research_run_match.group(1), tenant_id=user["tenant_id"], user_id=user["user_id"])
                if not result:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"run": result}})
            if path == "/api/open-research/memories":
                memories = rows(
                    conn,
                    """SELECT memory_id, topic, memory_json, created_at, updated_at, expires_at
                       FROM open_research_memory_index
                       WHERE tenant_id=? AND user_id=? AND status='ACTIVE' AND expires_at>?
                       ORDER BY updated_at DESC""",
                    (user["tenant_id"], user["user_id"], now_iso()),
                )
                for memory in memories:
                    memory["memory"] = json_loads(memory.pop("memory_json"), {})
                return self.send_json({"ok": True, "data": {"memories": memories}})
            office_asset_match = re.match(r"^/api/office/assets/([^/]+)$", path)
            if office_asset_match:
                asset = office_asset_service_for_request(conn, user).get(
                    asset_id=office_asset_match.group(1), tenant_id=user["tenant_id"], user_id=user["user_id"],
                )
                if not asset:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"asset": OfficeAssetService.public_asset(asset)}})
            office_job_match = re.match(r"^/api/office/jobs/([^/]+)$", path)
            if office_job_match:
                job = office_job_service_for_request(conn, user).get_job(job_id=office_job_match.group(1), tenant_id=user["tenant_id"], user_id=user["user_id"])
                if not job:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"job": job}})
            artifact_match = re.match(r"^/api/office/artifacts/([^/]+)/(preview|preview-png|download)$", path)
            if artifact_match:
                version_id, action = artifact_match.groups()
                artifact_kind = "preview" if action == "preview" else "preview_png" if action == "preview-png" else "download"
                found = office_job_service_for_request(conn, user).get_artifact(version_id=version_id, tenant_id=user["tenant_id"], user_id=user["user_id"], kind=artifact_kind)
                if not found:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                artifact, file_path = found
                return self.send_private_file(file_path, download_name=file_path.name, inline=action == "preview")
            if path == "/api/agent/effectiveness":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                research = rows(conn, "SELECT status, COUNT(*) AS count FROM open_research_runs WHERE tenant_id=? GROUP BY status", (user["tenant_id"],))
                research_source_tiers = rows(conn, """SELECT e.source_tier, COUNT(*) AS count
                    FROM open_research_evidence e JOIN open_research_runs r ON r.run_id=e.run_id
                    WHERE r.tenant_id=? GROUP BY e.source_tier""", (user["tenant_id"],))
                office = rows(conn, "SELECT status, COUNT(*) AS count FROM office_jobs WHERE tenant_id=? GROUP BY status", (user["tenant_id"],))
                feedback = rows(conn, "SELECT domain, feedback_type, COUNT(*) AS count FROM agent_feedback WHERE tenant_id=? GROUP BY domain, feedback_type", (user["tenant_id"],))
                provider_usage = rows(conn, "SELECT latency_ms, credits FROM open_research_provider_usage WHERE tenant_id=? AND provider='tavily'", (user["tenant_id"],))
                run_count = sum(int(item["count"] or 0) for item in research)
                evidence_delivery_count = sum(int(item["count"] or 0) for item in research if item["status"] in {"VERIFIED", "PARTIALLY_VERIFIED"})
                cited_run_count = int((one(conn, "SELECT COUNT(DISTINCT run_id) AS count FROM open_research_evidence WHERE run_id IN (SELECT run_id FROM open_research_runs WHERE tenant_id=?)", (user["tenant_id"],)) or {}).get("count") or 0)
                source_open_runs = int((one(conn, "SELECT COUNT(DISTINCT run_id) AS count FROM open_research_interactions WHERE tenant_id=? AND interaction_type='SOURCE_OPENED'", (user["tenant_id"],)) or {}).get("count") or 0)
                source_open_count = int((one(conn, "SELECT COUNT(*) AS count FROM open_research_interactions WHERE tenant_id=? AND interaction_type='SOURCE_OPENED'", (user["tenant_id"],)) or {}).get("count") or 0)
                claim_run_count = int((one(conn, """SELECT COUNT(DISTINCT c.run_id) AS count
                    FROM open_research_claims c JOIN open_research_runs r ON r.run_id=c.run_id
                    WHERE r.tenant_id=? AND c.predicate<>''""", (user["tenant_id"],)) or {}).get("count") or 0)
                no_evidence_deterministic_count = int((one(conn, """SELECT COUNT(*) AS count FROM (
                    SELECT r.run_id FROM open_research_runs r
                    LEFT JOIN open_research_evidence e ON e.run_id=r.run_id
                    WHERE r.tenant_id=? AND r.status='VERIFIED'
                    GROUP BY r.run_id HAVING COUNT(e.evidence_id)=0
                )""", (user["tenant_id"],)) or {}).get("count") or 0)
                research_feedback = [item for item in feedback if item["domain"] == "OPEN_RESEARCH" and item["feedback_type"] != "REFINE_SEARCH"]
                research_feedback_total = sum(int(item["count"] or 0) for item in research_feedback)
                helpful_count = sum(int(item["count"] or 0) for item in research_feedback if item["feedback_type"] == "HELPFUL")
                date_wrong_count = sum(int(item["count"] or 0) for item in research_feedback if item["feedback_type"] in {"DATE_WRONG", "CLAIM_WRONG"})
                retry_count = sum(int(item["count"] or 0) for item in feedback if item["domain"] == "OPEN_RESEARCH" and item["feedback_type"] == "REFINE_SEARCH")
                research_latencies = sorted(max(0, int(item["latency_ms"] or 0)) for item in provider_usage)
                office_rows = rows(conn, "SELECT created_at, completed_at FROM office_jobs WHERE tenant_id=? AND completed_at IS NOT NULL", (user["tenant_id"],))
                office_durations = []
                for item in office_rows:
                    try:
                        office_durations.append(max(0, round((datetime.fromisoformat(item["completed_at"]) - datetime.fromisoformat(item["created_at"])).total_seconds() * 1000)))
                    except (TypeError, ValueError):
                        continue
                def _p95(values):
                    if not values:
                        return None
                    return values[min(len(values) - 1, max(0, __import__("math").ceil(len(values) * 0.95) - 1))]
                return self.send_json({"ok": True, "data": {
                    "research": research,
                    "office": office,
                    "feedback": feedback,
                    "metrics": {
                        "research": {
                            "run_count": run_count,
                            "evidence_delivery_count": evidence_delivery_count,
                            "evidence_delivery_rate": round(evidence_delivery_count / run_count, 4) if run_count else None,
                            "claim_extraction_rate": round(claim_run_count / run_count, 4) if run_count else None,
                            "no_evidence_deterministic_count": no_evidence_deterministic_count,
                            "source_open_count": source_open_count,
                            "source_open_rate": round(source_open_runs / cited_run_count, 4) if cited_run_count else None,
                            "helpful_feedback_rate": round(helpful_count / research_feedback_total, 4) if research_feedback_total else None,
                            "date_wrong_feedback_rate": round(date_wrong_count / research_feedback_total, 4) if research_feedback_total else None,
                            "verified_answer_rate": round(sum(int(item["count"] or 0) for item in research if item["status"] == "VERIFIED") / run_count, 4) if run_count else None,
                            "no_authoritative_rate": round(sum(int(item["count"] or 0) for item in research if item["status"] == "NO_AUTHORITATIVE_SOURCE") / run_count, 4) if run_count else None,
                            "source_tier_distribution": {str(item["source_tier"]): int(item["count"] or 0) for item in research_source_tiers},
                            "refine_request_count": retry_count,
                            "tavily_request_count": len(provider_usage),
                            "tavily_credits": sum(max(0, int(item["credits"] or 0)) for item in provider_usage),
                            "provider_p95_ms": _p95(research_latencies),
                        },
                        "office": {"completed_job_count": len(office_durations), "job_p95_ms": _p95(sorted(office_durations))},
                    },
                }})
            workflow_match = re.match(r"^/api/agent/workflows/([^/]+)$", path)
            if workflow_match:
                workflow = get_workflow(conn, workflow_match.group(1), user["tenant_id"], user["user_id"])
                if not workflow:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"workflow": workflow}})
            document_match = re.match(
                r"^/api/conversations/([^/]+)/documents/([^/]+)/download$",
                path,
            )
            if document_match:
                return self.serve_open_qa_document(conn, user, *document_match.groups())
            if path == "/v1/catalog/skus":
                _comparison_access_required(user)
                version_id = (parse_qs(parsed.query).get("catalog_version_id") or [None])[0]
                return self.send_json({"ok": True, "data": list_catalog_skus(conn, user, version_id)})
            if path == "/v1/catalog-impact":
                _comparison_access_required(user)
                query = parse_qs(parsed.query)
                version_id = str((query.get("catalog_version_id") or [""])[0]).strip()
                if not version_id:
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "catalog_version_id is required"})
                sku_id = (query.get("sku_id") or [None])[0]
                return self.send_json({"ok": True, "data": catalog_impact(conn, user, version_id, sku_id)})
            comparison_session_match = re.match(r"^/v1/comparison-sessions/([^/]+)$", path)
            if comparison_session_match:
                _comparison_access_required(user)
                return self.send_json({"ok": True, "data": comparison_session_detail(conn, user, comparison_session_match.group(1))})
            if path == "/api/bootstrap":
                return self.api_bootstrap(conn, user)
            if path == "/api/conversations":
                conversations = rows(
                    conn,
                    """
                    SELECT c.*,
                           COUNT(m.message_id) AS message_count,
                           COALESCE((
                             SELECT content FROM messages lm
                             WHERE lm.conversation_id=c.conversation_id
                             ORDER BY lm.created_at DESC LIMIT 1
                           ), '') AS last_message
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id=c.conversation_id
                    WHERE c.user_id=? AND c.tenant_id=? AND c.status='ACTIVE'
                    GROUP BY c.conversation_id
                    ORDER BY c.updated_at DESC
                    LIMIT 50
                    """,
                    (user["user_id"], user["tenant_id"]),
                )
                return self.send_json({"ok": True, "data": {"conversations": conversations}})
            conversation_match = re.match(r"^/api/conversations/([^/]+)$", path)
            if conversation_match:
                conversation_id = conversation_match.group(1)
                conversation = one(
                    conn,
                    "SELECT * FROM conversations WHERE conversation_id=? AND user_id=? AND tenant_id=? AND status='ACTIVE'",
                    (conversation_id, user["user_id"], user["tenant_id"]),
                )
                if not conversation:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                messages = rows(
                    conn,
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC",
                    (conversation_id,),
                )
                return self.send_json(
                    {
                        "ok": True,
                        "data": {
                            "conversation": conversation,
                            "messages": [serialize_message(item, conn) for item in messages],
                        },
                    }
                )
            if path.startswith("/api/plans/"):
                plan_id = path.split("/")[-1]
                plan = one(conn, "SELECT * FROM plans WHERE plan_id=? AND tenant_id=?", (plan_id, user["tenant_id"]))
                if not plan:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"plan": serialize_plan(plan)}})
            if path == "/api/subscriptions":
                query = parse_qs(parsed.query)
                return self.api_subscriptions(conn, user, (query.get("org_id") or [None])[0])
            if path == "/api/integrations":
                if not role_can_manage_integrations(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": {"integrations": list_integrations(conn)}})
            if path == "/api/agent/catalog":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": agent_catalog_payload(conn, user)})
            if path == "/api/agent/web-search/config":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": public_web_search_config(conn, user["tenant_id"])})
            if path == "/api/agent/intents":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": {"intents": agent_catalog_payload(conn, user)["catalog"]["intents"]}})
            if path == "/api/agent/skills":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                payload = agent_catalog_payload(conn, user)
                return self.send_json({"ok": True, "data": {"skills": payload["catalog"]["skills"], "extensions": [item for item in payload["extensions"] if item["kind"] == "skill"]}})
            if path == "/api/agent/tools":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                payload = agent_catalog_payload(conn, user)
                return self.send_json({"ok": True, "data": {"tools": payload["catalog"]["tools"], "extensions": [item for item in payload["extensions"] if item["kind"] == "tool"]}})
            if path == "/api/agent/memories":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": {"memories": list_agent_memories(conn, user)}})
            if path == "/api/agent/knowledge":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                return self.send_json({"ok": True, "data": {"knowledge_items": list_agent_knowledge_items(conn, user)}})
            if path == "/api/inspection-batches":
                return self.api_inspection_batches(conn, user, parse_qs(parsed.query))
            inspection_batch_match = re.match(r"^/api/inspection-batches/([^/]+)$", path)
            if inspection_batch_match:
                return self.api_inspection_batch_detail(conn, user, inspection_batch_match.group(1))
            if path == "/api/scheduled-inspections":
                return self.api_scheduled_inspections(conn, user)
            scheduled_detail_match = re.match(r"^/api/scheduled-inspections/([^/]+)$", path)
            if scheduled_detail_match:
                task = one(
                    conn,
                    "SELECT * FROM scheduled_inspections WHERE task_id=? AND user_id=? AND tenant_id=?",
                    (scheduled_detail_match.group(1), user["user_id"], user["tenant_id"]),
                )
                if not task:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                return self.send_json({"ok": True, "data": {"scheduled_inspection": serialize_scheduled_task(conn, task, include_runs=True)}})
            if path == "/api/inspection-runs":
                return self.api_inspection_runs(conn, user, parse_qs(parsed.query))
            inspection_run_match = re.match(r"^/api/inspection-runs/([^/]+)$", path)
            if inspection_run_match:
                return self.api_inspection_run_detail(conn, user, inspection_run_match.group(1))
            if path.startswith("/api/events/") and path.endswith("/evidence"):
                event_id = path.split("/")[3]
                return self.api_event_evidence(conn, user, event_id)
            if path.startswith("/api/events/"):
                event_id = path.split("/")[-1]
                return self.api_event_detail(conn, user, event_id)
            if path == "/api/events":
                query = parse_qs(parsed.query)
                return self.api_events(conn, user, query)
            if path == "/api/audit-logs":
                if not role_can_view_audit(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                audits = rows(
                    conn,
                    "SELECT * FROM audit_logs WHERE tenant_id=? ORDER BY created_at DESC, rowid DESC LIMIT 50",
                    (user["tenant_id"],),
                )
                return self.send_json({"ok": True, "data": {"audit_logs": audits}})
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)

    def api_media_stream(self, session_id: str, access_token: str, tenant_code: str):
        with connect() as conn:
            online = online_agent_for_tenant(conn, tenant_code, required=True)
        if not online:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        source = online.media_stream_source(session_id, access_token)
        upstream_request = urlrequest.Request(
            source["url"],
            headers={"Accept": "video/x-flv,*/*", "User-Agent": "WanxiangAGIInspection/0.2"},
        )
        try:
            upstream = urlrequest.urlopen(upstream_request, timeout=15)
        except (urlerror.URLError, TimeoutError) as exc:
            raise OnlineAgentError("MEDIA_STREAM_UNAVAILABLE", "视频流连接失败") from exc

        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", upstream.headers.get("Content-Type") or source["content_type"])
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    def route_post(self):
        parsed = urlparse(self.path)
        path = parsed.path
        with connect() as conn:
            user = user_from_request(self, conn)
            # Authenticate before consuming an Office body.  An anonymous
            # 120 MB upload must not allocate staging space or exercise the
            # Office ingress path (GATE-001's zero-business-side-effect rule).
            body = self.read_office_upload() if path == "/api/office/assets" else self.read_json()
            if path == "/api/agent/feature-flags":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                payload = body if isinstance(body, dict) else {}
                updates = payload.get("flags") if isinstance(payload.get("flags"), dict) else payload
                if not isinstance(updates, dict):
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "flags 必须是对象"})
                try:
                    change = apply_feature_updates(conn, user["tenant_id"], updates, now_iso())
                except FeatureFlagPolicyError as exc:
                    messages = {
                        "UNKNOWN_FEATURE_FLAG": "包含未知租户能力开关。",
                        "FEATURE_FLAG_UPDATE_EMPTY": "请至少选择一个需要变更的能力开关。",
                        "FEATURE_FLAG_LOCKED_P0": "该能力在 P0 阶段固定关闭，暂不允许启用。",
                        "FEATURE_FLAG_DEPENDENCY_REQUIRED": "请先开启该能力依赖的上游能力。",
                    }
                    status = HTTPStatus.CONFLICT if exc.code in {"FEATURE_FLAG_LOCKED_P0", "FEATURE_FLAG_DEPENDENCY_REQUIRED"} else HTTPStatus.BAD_REQUEST
                    raise ApiError(exc.code, status, {"message": messages.get(exc.code, "租户能力开关不满足策略要求。"), "flag": exc.flag, "dependencies": list(exc.dependencies)}) from exc
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "agent.feature_flags.update",
                    "feature_flags",
                    user["tenant_id"],
                    change["before"],
                    {"flags": change["after"], "changed_flags": change["changed_flags"], "forced_disabled": change["forced_disabled"]},
                    "agent_governance",
                    None,
                )
                conn.commit()
                return self.send_json({"ok": True, "data": {**tenant_feature_flag_settings(conn, user["tenant_id"]), "changed_flags": change["changed_flags"], "forced_disabled": change["forced_disabled"]}})
            if path == "/api/open-research/source-policies":
                # Source trust is a platform control.  A tenant administrator
                # may inspect active policies but can never promote a domain.
                if user["role"] != "system_admin":
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                payload = body if isinstance(body, dict) else {}
                try:
                    domain = normalize_domain(payload.get("domain"))
                    tier = str(payload.get("tier") or "SECONDARY").upper()
                    fact_types = payload.get("allowed_fact_types") or ["*"]
                    reputation_weight = payload.get("reputation_weight")
                    if tier not in SOURCE_TIERS or not isinstance(fact_types, list) or (reputation_weight is not None and not 0.0 <= float(reputation_weight) <= 1.0):
                        raise ValueError("invalid source policy")
                except ValueError:
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "来源策略字段不合法"})
                if one(conn, "SELECT policy_id FROM research_source_policies WHERE domain=?", (domain,)):
                    raise ApiError("CONFLICT", HTTPStatus.CONFLICT, {"message": "该域名已有来源策略，请走平台变更流程。"})
                policy_id = f"rsp_{uuid.uuid4().hex[:16]}"
                upsert_source_policy(
                    conn,
                    policy_id=policy_id,
                    domain=domain,
                    match_subdomains=bool(payload.get("match_subdomains")),
                    tier=tier,
                    allowed_fact_types=fact_types,
                    status="DRAFT",
                    reviewed_by=None,
                    reviewed_at=None,
                    expires_at=str(payload.get("expires_at") or "").strip() or None,
                    created_by=user["user_id"],
                    now=now_iso(),
                    reputation_weight=reputation_weight,
                )
                log_audit(conn, user["user_id"], user["tenant_id"], "open_research.source_policy.create", "research_source_policy", policy_id, None,
                          audit_payload(domain=domain, tier=tier, allowed_fact_types=fact_types, reputation_weight=reputation_weight, status="DRAFT"), "open_research", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"policy_id": policy_id, "status": "DRAFT"}}, HTTPStatus.CREATED)
            source_policy_approve_match = re.match(r"^/api/open-research/source-policies/([^/]+)/approve$", path)
            if source_policy_approve_match:
                if user["role"] != "system_admin":
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                policy = one(conn, "SELECT * FROM research_source_policies WHERE policy_id=?", (source_policy_approve_match.group(1),))
                if not policy:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                if policy["status"] != "DRAFT" or policy["created_by"] == user["user_id"]:
                    raise ApiError("SOURCE_POLICY_REVIEW_REQUIRED", HTTPStatus.CONFLICT, {"message": "来源策略需由另一位平台管理员复核后启用。"})
                timestamp = now_iso()
                conn.execute(
                    "UPDATE research_source_policies SET status='ACTIVE', reviewed_by=?, reviewed_at=?, updated_at=? WHERE policy_id=?",
                    (user["user_id"], timestamp, timestamp, policy["policy_id"]),
                )
                log_audit(conn, user["user_id"], user["tenant_id"], "open_research.source_policy.approve", "research_source_policy", policy["policy_id"],
                          {"status": policy["status"]}, audit_payload(status="ACTIVE", domain=policy["domain"]), "open_research", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"policy_id": policy["policy_id"], "status": "ACTIVE", "reviewed_at": timestamp}})
            if path == "/api/open-research/entity-aliases":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                from open_research.boundary import classify_query
                payload = body if isinstance(body, dict) else {}
                alias_text = re.sub(r"\s+", " ", str(payload.get("alias_text") or "")).strip()
                canonical_entity = re.sub(r"\s+", " ", str(payload.get("canonical_entity") or "")).strip()
                reason = str(payload.get("reason") or "COMMON_TYPO").upper()
                status = str(payload.get("status") or "ACTIVE").upper()
                try:
                    confidence = float(payload.get("confidence"))
                except (TypeError, ValueError):
                    confidence = -1.0
                if (
                    not alias_text or not canonical_entity or alias_text == canonical_entity
                    or len(alias_text) > 120 or len(canonical_entity) > 120
                    or classify_query(alias_text) != "PUBLIC" or classify_query(canonical_entity) != "PUBLIC"
                    or not 0.5 <= confidence <= 1.0
                    or reason not in {"HOMOPHONIC_TYPO", "COMMON_TYPO", "OFFICIAL_ALIAS", "ENTITY_RESOLUTION"}
                    or status not in {"ACTIVE", "DISABLED"}
                ):
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "实体改写规则不符合受控目录要求"})
                existing = one(
                    conn,
                    "SELECT alias_id FROM open_research_entity_aliases WHERE tenant_id=? AND alias_text=?",
                    (user["tenant_id"], alias_text),
                )
                alias_id = str(existing["alias_id"]) if existing else f"eralias_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    """INSERT INTO open_research_entity_aliases(
                           alias_id, tenant_id, alias_text, canonical_entity, confidence, reason, status, created_by, created_at, updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(tenant_id, alias_text) DO UPDATE SET
                           canonical_entity=excluded.canonical_entity, confidence=excluded.confidence,
                           reason=excluded.reason, status=excluded.status, created_by=excluded.created_by,
                           updated_at=excluded.updated_at""",
                    (alias_id, user["tenant_id"], alias_text, canonical_entity, confidence, reason, status,
                     user["user_id"], now_iso(), now_iso()),
                )
                log_audit(
                    conn, user["user_id"], user["tenant_id"], "open_research.entity_alias.upsert", "entity_alias", alias_id,
                    None, audit_payload(alias_text=alias_text, canonical_entity=canonical_entity, confidence=confidence, reason_code=reason, status=status),
                    "open_research", None,
                )
                conn.commit()
                return self.send_json({"ok": True, "data": {"alias_id": alias_id, "status": status}}, HTTPStatus.CREATED)
            if path == "/api/office/assets":
                engine, context = new_domain_gate_engine(conn, user, domain="OFFICE", action="UPLOAD_ASSET", conversation_id=None, input_value={"file_count": len(body.get("files") or [])})
                enabled = feature_snapshot(conn, user["tenant_id"])["office_enabled"]
                decision = engine.record(context, GateDecision("G0", "ALLOW" if enabled else "BLOCK", "FEATURE_ENABLED" if enabled else "FEATURE_DISABLED"))
                if not decision.allowed:
                    self.cleanup_office_upload_staging(body.get("files") or [])
                    raise ApiError("FEATURE_DISABLED", HTTPStatus.CONFLICT)
                engine.record(context, GateDecision("G1", "ALLOW", "OFFICE_UPLOAD_ROUTE_CONFIRMED"))
                try:
                    assets = office_asset_service_for_request(conn, user).create_batch(
                        tenant_id=user["tenant_id"], user_id=user["user_id"], files=body.get("files") or [],
                    )
                except OfficePolicyError as exc:
                    engine.record(context, GateDecision("G2", "BLOCK", exc.code))
                    log_audit(conn, user["user_id"], user["tenant_id"], "office.asset.rejected", "office_asset", "upload", None,
                              audit_payload(reason_code=exc.code, document=body.get("files") or []), "office_agent", None)
                    raise
                finally:
                    # Staging files never become a second retention store:
                    # promotion copies atomically to private asset storage and
                    # both success and rejection remove the request scratch.
                    self.cleanup_office_upload_staging(body.get("files") or [])
                engine.record(context, GateDecision("G2", "ALLOW", "OFFICE_ASSET_ALLOWED", {"asset_count": len(assets)}))
                engine.record(context, GateDecision("G3", "ALLOW", "OFFICE_PRIVATE_ASSET_PLAN_ALLOWED"))
                engine.record(context, GateDecision("G4", "ALLOW", "READ_ONLY_ASSET_UPLOAD"))
                engine.record(context, GateDecision("G5", "ALLOW", "OFFICE_STORAGE_RUNTIME_AVAILABLE"))
                engine.record(context, GateDecision("G6", "ALLOW", "OFFICE_ASSET_SCAN_AND_POLICY_VALIDATED"))
                engine.record(context, GateDecision("G7", "ALLOW", "OFFICE_PRIVATE_ASSET_RETAINED", {"retention_days": 30}))
                log_audit(conn, user["user_id"], user["tenant_id"], "office.asset.upload", "office_asset_batch", summary_hash([item["sha256"] for item in assets]), None,
                          {"asset_count": len(assets), "sha256": [item["sha256"] for item in assets]}, "office_agent", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"assets": assets}}, HTTPStatus.CREATED)
            extract_match = re.match(r"^/api/office/assets/([^/]+)/extract$", path)
            if extract_match:
                payload = body if isinstance(body, dict) else {}
                extracted = office_job_service_for_request(conn, user).extract_asset(
                    asset_id=extract_match.group(1), tenant_id=user["tenant_id"], user_id=user["user_id"],
                    mode_override=str(payload.get("mode_override") or "AUTO"),
                )
                log_audit(conn, user["user_id"], user["tenant_id"], "office.asset.extract", "office_asset", extract_match.group(1), None,
                          {"extraction_hash": summary_hash(extracted)}, "office_agent", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"extraction": extracted}})
            if path == "/api/office/jobs":
                payload = body if isinstance(body, dict) else {}
                created = office_job_service_for_request(conn, user).create_ppt_job(
                    tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=payload.get("conversation_id"),
                    asset_ids=payload.get("asset_ids") if isinstance(payload.get("asset_ids"), list) else [],
                    brief_id=str(payload.get("research_brief_id") or "") or None,
                    title=str(payload.get("title") or "管理层汇报"), template_id=str(payload.get("template_id") or "template_default"),
                    workflow_id=str(payload.get("workflow_id") or "") or None,
                    mode_override=str(payload.get("mode_override") or "AUTO"), auto_run=bool(payload.get("run_now")),
                )
                conn.commit()
                return self.send_json({"ok": True, "data": created}, HTTPStatus.CREATED if created.get("job") else HTTPStatus.CONFLICT)
            office_job_action = re.match(r"^/api/office/jobs/([^/]+)/(run|cancel|retry)$", path)
            if office_job_action:
                job_id, action = office_job_action.groups()
                service = office_job_service_for_request(conn, user)
                if action == "run":
                    if str(os.environ.get("AGI_OFFICE_PRODUCTION") or "").lower() in {"1", "true", "yes"}:
                        raise ApiError("OFFICE_MANUAL_RUN_DISABLED", HTTPStatus.CONFLICT)
                    result = service.run_job(job_id=job_id, tenant_id=user["tenant_id"], user_id=user["user_id"])
                elif action == "cancel":
                    result = service.cancel_job(job_id=job_id, tenant_id=user["tenant_id"], user_id=user["user_id"])
                else:
                    result = service.retry_job(job_id=job_id, tenant_id=user["tenant_id"], user_id=user["user_id"])
                if not result:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                conn.commit()
                return self.send_json({"ok": True, "data": {"job": result}})
            research_refine_match = re.match(r"^/api/open-research/runs/([^/]+)/refine$", path)
            if research_refine_match:
                prior = one(
                    conn,
                    """SELECT run_id, conversation_id, question_hash FROM open_research_runs
                       WHERE run_id=? AND tenant_id=? AND user_id=?""",
                    (research_refine_match.group(1), user["tenant_id"], user["user_id"]),
                )
                if not prior:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                # A re-query deliberately looks up the user's already-owned
                # public chat message by hash.  The raw query is not copied to
                # a refinement/audit table, and no internal or blocked query
                # can be replayed through this endpoint.
                message_rows = rows(
                    conn,
                    """SELECT content FROM messages WHERE conversation_id=? AND sender='user'
                       ORDER BY created_at DESC""",
                    (prior["conversation_id"],),
                )
                question = next((item["content"] for item in message_rows if summary_hash(item["content"]) == prior["question_hash"]), None)
                if not question:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                result = open_research_service_for_request(conn, user).run(
                    tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=prior["conversation_id"], question=question,
                    force_refresh=True,
                )
                delivery = add_open_research_message(conn, user, prior["conversation_id"], question, result)
                feedback_id = f"feedback_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    "INSERT INTO agent_feedback VALUES (?,?,?,?,?,?,?,?,?)",
                    (feedback_id, user["tenant_id"], user["user_id"], None, "OPEN_RESEARCH", prior["run_id"], "REFINE_SEARCH", None, now_iso()),
                )
                log_audit(conn, user["user_id"], user["tenant_id"], "open_research.refine", "open_research_run", prior["run_id"], None,
                          audit_payload(prior_run_id=prior["run_id"], new_run_id=result.get("run_id"), status=result.get("status")), "open_research", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"research": result, "messages": delivery.get("messages") or [], "feedback_id": feedback_id}})
            research_source_open_match = re.match(r"^/api/open-research/runs/([^/]+)/source-open$", path)
            if research_source_open_match:
                payload = body if isinstance(body, dict) else {}
                run_id = research_source_open_match.group(1)
                evidence_id = str(payload.get("evidence_id") or "")
                run = one(conn, "SELECT run_id FROM open_research_runs WHERE run_id=? AND tenant_id=? AND user_id=?", (run_id, user["tenant_id"], user["user_id"]))
                evidence = one(conn, "SELECT evidence_id FROM open_research_evidence WHERE run_id=? AND evidence_id=?", (run_id, evidence_id)) if run and evidence_id else None
                if not run or not evidence:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                interaction_id = f"resint_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    "INSERT INTO open_research_interactions VALUES(?,?,?,?,?,?,?)",
                    (interaction_id, run_id, user["tenant_id"], user["user_id"], "SOURCE_OPENED", evidence_id, now_iso()),
                )
                log_audit(conn, user["user_id"], user["tenant_id"], "open_research.source_open", "open_research_evidence", evidence_id, None,
                          audit_payload(run_id=run_id, evidence_id=evidence_id, interaction_type="SOURCE_OPENED"), "open_research", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"interaction_id": interaction_id}}, HTTPStatus.CREATED)
            if path == "/api/open-research/feedback":
                payload = body if isinstance(body, dict) else {}
                run_id = str(payload.get("run_id") or "")
                run = one(conn, "SELECT run_id FROM open_research_runs WHERE run_id=? AND tenant_id=? AND user_id=?", (run_id, user["tenant_id"], user["user_id"]))
                if not run:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                feedback_type = str(payload.get("feedback_type") or "").upper()
                if feedback_type not in {
                    "HELPFUL", "INACCURATE", "OUTDATED", "NOT_FOUND", "UNTRUSTED_SOURCE", "REFINE_SEARCH",
                    "CLAIM_WRONG", "DATE_WRONG", "REGION_MISSING", "SOURCE_TIER_WRONG",
                }:
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "feedback_type 无效"})
                feedback_id = f"feedback_{uuid.uuid4().hex[:16]}"
                conn.execute("INSERT INTO agent_feedback VALUES (?,?,?,?,?,?,?,?,?)", (feedback_id, user["tenant_id"], user["user_id"], payload.get("workflow_id"), "OPEN_RESEARCH", run_id, feedback_type, feedback_reason_for_storage(payload.get("reason")), now_iso()))
                invalidated = 0
                if feedback_type in {"INACCURATE", "OUTDATED", "NOT_FOUND", "UNTRUSTED_SOURCE", "CLAIM_WRONG", "DATE_WRONG", "REGION_MISSING", "SOURCE_TIER_WRONG"}:
                    invalidated = invalidate_open_research_memories_for_run(conn, user, run_id, reason=feedback_type)
                log_audit(conn, user["user_id"], user["tenant_id"], "open_research.feedback", "open_research_run", run_id, None,
                          audit_payload(feedback_type=feedback_type, reason=payload.get("reason"), invalidated_memory_count=invalidated), "open_research", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"feedback_id": feedback_id, "invalidated_memory_count": invalidated}}, HTTPStatus.CREATED)
            if path == "/api/office/feedback":
                payload = body if isinstance(body, dict) else {}
                job_id = str(payload.get("job_id") or "")
                job = one(conn, "SELECT job_id FROM office_jobs WHERE job_id=? AND tenant_id=? AND user_id=?", (job_id, user["tenant_id"], user["user_id"]))
                if not job:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                feedback_type = str(payload.get("feedback_type") or "").upper()
                if feedback_type not in {"HELPFUL", "LAYOUT_ISSUE", "DATA_ISSUE", "SOURCE_UNCLEAR", "FILE_UNOPENABLE", "NEEDS_REVISION"}:
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "feedback_type 无效"})
                feedback_id = f"feedback_{uuid.uuid4().hex[:16]}"
                conn.execute("INSERT INTO agent_feedback VALUES (?,?,?,?,?,?,?,?,?)", (feedback_id, user["tenant_id"], user["user_id"], payload.get("workflow_id"), "OFFICE", job_id, feedback_type, feedback_reason_for_storage(payload.get("reason")), now_iso()))
                log_audit(conn, user["user_id"], user["tenant_id"], "office.feedback", "office_job", job_id, None,
                          audit_payload(feedback_type=feedback_type, reason=payload.get("reason")), "office_agent", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"feedback_id": feedback_id}}, HTTPStatus.CREATED)
            workflow_feedback_match = re.match(r"^/api/agent/workflows/([^/]+)/feedback$", path)
            if workflow_feedback_match:
                payload = body if isinstance(body, dict) else {}
                workflow = get_workflow(conn, workflow_feedback_match.group(1), user["tenant_id"], user["user_id"])
                if not workflow:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                feedback_id = f"feedback_{uuid.uuid4().hex[:16]}"
                conn.execute("INSERT INTO agent_feedback VALUES (?,?,?,?,?,?,?,?,?)", (feedback_id, user["tenant_id"], user["user_id"], workflow["workflow_id"], "HYBRID", workflow["workflow_id"], str(payload.get("feedback_type") or "OTHER")[:40], feedback_reason_for_storage(payload.get("reason")), now_iso()))
                conn.commit()
                return self.send_json({"ok": True, "data": {"feedback_id": feedback_id}}, HTTPStatus.CREATED)
            if path == "/v1/ovd/contract-test":
                _comparison_access_required(user)
                payload = body if isinstance(body, dict) else {}
                expected_prompts = _normalize_text_list(payload.get("expected_prompts"), limit=20, item_limit=120)
                if str(payload.get("provider") or "").strip().casefold() == "eas":
                    report = eas_ovd_contract_report(
                        payload.get("response"),
                        expected_prompts,
                        payload.get("image_width") or payload.get("source_image_width"),
                        payload.get("image_height") or payload.get("source_image_height"),
                        payload.get("model_version") or "pytrt_sam3",
                    )
                else:
                    report = ovd_contract_report(payload.get("response"), expected_prompts)
                log_audit(conn, user["user_id"], user["tenant_id"], "comparison.ovd.contract_test", "ovd_adapter", "external_ovd", None, {"ok": report["ok"], "code": report.get("code"), "model_version": report.get("model_version")}, "comparison_service", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"contract_test": report}})
            if path == "/v1/catalog-versions":
                version = create_catalog_version(conn, user, body if isinstance(body, dict) else {}, clone_published=bool((body or {}).get("clone_published")))
                conn.commit()
                return self.send_json({"ok": True, "data": {"catalog_version": version}}, HTTPStatus.CREATED)
            if path == "/v1/catalog/skus":
                result = create_catalog_sku(conn, user, body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": result}, HTTPStatus.CREATED)
            catalog_version_action_match = re.match(r"^/v1/catalog-versions/([^/]+)/(approve|publish|retire)$", path)
            if catalog_version_action_match:
                version_id, action = catalog_version_action_match.groups()
                if action == "approve":
                    result = approve_catalog_version(conn, user, version_id)
                elif action == "publish":
                    result = publish_catalog_version(conn, user, version_id)
                else:
                    result = {"catalog_version": retire_catalog_version(conn, user, version_id)}
                conn.commit()
                return self.send_json({"ok": True, "data": result})
            if path == "/v1/domain-profiles":
                profile = create_domain_profile(conn, user, body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": {"domain_profile": profile}}, HTTPStatus.CREATED)
            domain_profile_action_match = re.match(r"^/v1/domain-profiles/([^/]+)/approve$", path)
            if domain_profile_action_match:
                profile = approve_domain_profile(conn, user, domain_profile_action_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"domain_profile": profile}})
            if path == "/v1/calibrations":
                calibration = create_calibration_profile(conn, user, body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": {"calibration": calibration}}, HTTPStatus.CREATED)
            calibration_action_match = re.match(r"^/v1/calibrations/([^/]+)/approve$", path)
            if calibration_action_match:
                calibration = approve_calibration_profile(conn, user, calibration_action_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"calibration": calibration}})
            if path == "/v1/reference-assets":
                asset = create_reference_asset(conn, user, body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": {"reference_asset": asset}}, HTTPStatus.CREATED)
            reference_asset_action_match = re.match(r"^/v1/reference-assets/([^/]+)/approve$", path)
            if reference_asset_action_match:
                asset = approve_reference_asset(conn, user, reference_asset_action_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"reference_asset": asset}})
            if path == "/v1/display-slots":
                slot = create_display_slot(conn, user, body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": {"display_slot": slot}}, HTTPStatus.CREATED)
            display_slot_action_match = re.match(r"^/v1/display-slots/([^/]+)/approve$", path)
            if display_slot_action_match:
                slot = approve_display_slot(conn, user, display_slot_action_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"display_slot": slot}})
            if path == "/v1/comparison-sessions":
                session = create_comparison_session(conn, user, body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": {"comparison_session": session}}, HTTPStatus.CREATED)
            comparison_frame_match = re.match(r"^/v1/comparison-sessions/([^/]+)/frames$", path)
            if comparison_frame_match:
                frame = create_ovd_comparison_frame(conn, user, comparison_frame_match.group(1), body if isinstance(body, dict) else {})
                decisions = refresh_comparison_slot_decisions(conn, user, comparison_frame_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"frame": frame, "slot_decisions": decisions}}, HTTPStatus.CREATED)
            comparison_decision_match = re.match(r"^/v1/comparison-sessions/([^/]+)/decide$", path)
            if comparison_decision_match:
                decisions = refresh_comparison_slot_decisions(conn, user, comparison_decision_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"slot_decisions": decisions}})
            review_match = re.match(r"^/v1/reviews/([^/]+)$", path)
            if review_match:
                review = create_comparison_review(conn, user, review_match.group(1), body if isinstance(body, dict) else {})
                conn.commit()
                return self.send_json({"ok": True, "data": {"review": review}}, HTTPStatus.CREATED)
            if path == "/api/agent/manifests/validate":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                payload = body if isinstance(body, dict) else {}
                manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else body
                return self.send_json({"ok": True, "data": {"validation": validate_agent_manifest_for_user(conn, user, manifest)}})
            if path == "/api/agent/web-search/config":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                payload = dict(body) if isinstance(body, dict) else {}
                current_config, current_token = web_search_runtime_config(conn, user["tenant_id"])
                if current_token == "environment":
                    raise ApiError(
                        "WEB_SEARCH_ENV_MANAGED",
                        HTTPStatus.CONFLICT,
                        {"message": "公共网页检索由环境变量托管，请在部署环境更新配置。"},
                    )
                provider = str(payload.get("provider") or "").strip().lower()
                if not str(payload.get("api_key") or "").strip():
                    # A saved tenant credential may be retained while changing only
                    # non-secret settings. Platform defaults are deliberately not copied.
                    if current_token.startswith("tenant_config:") and provider == str(current_config.get("provider") or "").lower():
                        payload["api_key"] = current_config.get("api_key")
                before = public_web_search_config(conn, user["tenant_id"])
                try:
                    save_web_search_runtime_config(conn, payload, user["tenant_id"])
                except ValueError as exc:
                    raise ApiError(
                        "BAD_REQUEST",
                        HTTPStatus.BAD_REQUEST,
                        {"message": "请填写有效的搜索服务、访问密钥和参数。"},
                    ) from exc
                configured = public_web_search_config(conn, user["tenant_id"])
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "agent.web_search.configure",
                    "agent_tool",
                    "web.search",
                    before,
                    configured,
                    "agent_catalog",
                    None,
                )
                conn.commit()
                return self.send_json({"ok": True, "data": configured})
            if path == "/api/agent/web-search/usage/refresh":
                if not role_can_manage_agent_catalog(user["role"]):
                    raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
                config, _cache_token = web_search_runtime_config(conn, user["tenant_id"])
                if str(config.get("provider") or "").lower() != "tavily" or not WebSearchClient(config).configured:
                    raise ApiError(
                        "BAD_REQUEST",
                        HTTPStatus.BAD_REQUEST,
                        {"message": "请先配置可用的 Tavily 公共网页检索服务。"},
                    )
                try:
                    account_usage = WebSearchClient(config).usage()
                except WebSearchError as exc:
                    raise ApiError(
                        "UPSTREAM_UNAVAILABLE",
                        HTTPStatus.BAD_GATEWAY,
                        {"message": "暂时无法同步 Tavily 额度，请稍后重试。", "provider_code": exc.code},
                    ) from exc
                summary = record_web_search_usage(
                    conn,
                    tenant_id=user["tenant_id"],
                    conversation_id=None,
                    config=config,
                    operation="USAGE_SYNC",
                    status="SUCCEEDED",
                    account_usage=account_usage,
                )
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "agent.web_search.usage.refresh",
                    "agent_tool",
                    "web.search",
                    None,
                    {"remaining_credits": summary.get("remaining_credits"), "credit_limit": summary.get("credit_limit")},
                    "agent_catalog",
                    None,
                )
                conn.commit()
                return self.send_json({"ok": True, "data": {"usage": summary}})
            if path == "/api/agent/manifests/draft":
                payload = body if isinstance(body, dict) else {}
                draft = infer_manifest_draft_from_prompt(conn, user, payload.get("prompt"), payload.get("kind"))
                return self.send_json({"ok": True, "data": draft})
            if path == "/api/agent/manifests":
                payload = body if isinstance(body, dict) else {}
                manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else body
                imported = create_agent_manifest_import(conn, user, manifest)
                conn.commit()
                return self.send_json({"ok": True, "data": {"manifest": imported}}, HTTPStatus.CREATED)
            if path == "/api/agent/memories":
                memory = create_agent_memory(conn, user, body)
                conn.commit()
                return self.send_json({"ok": True, "data": {"memory": memory}}, HTTPStatus.CREATED)
            if path == "/api/agent/knowledge":
                knowledge = create_agent_knowledge_item(conn, user, body)
                conn.commit()
                return self.send_json({"ok": True, "data": {"knowledge": knowledge}}, HTTPStatus.CREATED)
            if path == "/api/agent/knowledge-assets":
                asset = create_agent_knowledge_asset(conn, user, body)
                conn.commit()
                return self.send_json({"ok": True, "data": {"asset": asset}}, HTTPStatus.CREATED)
            if path == "/api/integrations":
                message = None
                conversation_id = str(body.get("conversation_id") or "")
                if conversation_id:
                    conversation = one(
                        conn,
                        "SELECT conversation_id FROM conversations WHERE conversation_id=? AND user_id=? AND tenant_id=? AND status='ACTIVE'",
                        (conversation_id, user["user_id"], user["tenant_id"]),
                    )
                    if not conversation:
                        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                integration = create_tenant_integration(conn, user, body)
                if conversation_id:
                    message = integration_result_message(conn, conversation_id, integration)
                conn.commit()
                return self.send_json(
                    {"ok": True, "data": {"integration": integration_artifact_summary(integration), "message": serialize_message(message) if message else None}},
                    HTTPStatus.CREATED,
                )
            if path == "/api/conversations":
                conversation = create_conversation(conn, user, body.get("title", "新的巡检对话"), body.get("page_code", "agi-inspection"), body.get("org_id"))
                conn.commit()
                return self.send_json({"ok": True, "data": {"conversation": conversation}}, HTTPStatus.CREATED)
            media_stop_match = re.match(r"^/api/media/sessions/([^/]+)/stop$", path)
            if media_stop_match:
                online = online_agent_for_tenant(conn, user["tenant_id"])
                if not online:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                result = online.stop_media_session(media_stop_match.group(1))
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "media.session.stop",
                    "media_session",
                    result["session_id"],
                    None,
                    {"status": result["status"], "source": "deepvision_online"},
                    "agent",
                    None,
                )
                conn.commit()
                return self.send_json({"ok": True, "data": result})
            msg_match = re.match(r"^/api/conversations/([^/]+)/messages$", path)
            if msg_match:
                response = self.api_send_message(conn, user, msg_match.group(1), body)
                conn.commit()
                return self.send_json({"ok": True, "data": response})
            confirm_match = re.match(r"^/api/plans/([^/]+)/confirm$", path)
            if confirm_match:
                result = execute_plan(conn, user, confirm_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": result})
            cancel_match = re.match(r"^/api/plans/([^/]+)/cancel$", path)
            if cancel_match:
                result = cancel_plan(conn, user, cancel_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": result})
            batch_action_match = re.match(r"^/api/inspection-batches/([^/]+)/(retry|cancel)$", path)
            if batch_action_match:
                batch_id, action = batch_action_match.groups()
                if action == "retry":
                    result = self.api_retry_inspection_batch(conn, user, batch_id)
                else:
                    result = self.api_cancel_inspection_batch(conn, user, batch_id)
                conn.commit()
                return self.send_json({"ok": True, "data": result})
            scheduled_action_match = re.match(r"^/api/scheduled-inspections/([^/]+)/(pause|resume|run-now|cancel)$", path)
            if scheduled_action_match:
                task_id, action = scheduled_action_match.groups()
                task = one(
                    conn,
                    "SELECT * FROM scheduled_inspections WHERE task_id=? AND user_id=? AND tenant_id=?",
                    (task_id, user["user_id"], user["tenant_id"]),
                )
                if not task:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                before = {"status": task["status"], "next_run_at": task["next_run_at"]}
                if action == "pause":
                    conn.execute("UPDATE scheduled_inspections SET status='PAUSED', next_run_at=NULL, updated_at=? WHERE task_id=?", (now_iso(), task_id))
                elif action == "cancel":
                    conn.execute("UPDATE scheduled_inspections SET status='CANCELLED', next_run_at=NULL, updated_at=? WHERE task_id=?", (now_iso(), task_id))
                elif action == "resume":
                    if task["status"] == "CANCELLED":
                        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "已取消的任务不能恢复"})
                    conn.execute("UPDATE scheduled_inspections SET status='ACTIVE', next_run_at=?, updated_at=? WHERE task_id=?", (now_iso(), now_iso(), task_id))
                else:
                    if task["status"] == "CANCELLED":
                        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "已取消的任务不能执行"})
                    active_run = one(conn, "SELECT run_id FROM inspection_runs WHERE task_id=? AND status='ANALYZING'", (task_id,))
                    if active_run:
                        raise ApiError("VALIDATION_FAILED", HTTPStatus.CONFLICT, {"message": "当前已有一轮巡检正在分析"})
                    conn.execute("UPDATE scheduled_inspections SET status='ACTIVE', next_run_at=?, updated_at=? WHERE task_id=?", (now_iso(), now_iso(), task_id))
                updated = one(conn, "SELECT * FROM scheduled_inspections WHERE task_id=?", (task_id,))
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    f"scheduled_inspection.{action}",
                    "scheduled_inspection",
                    task_id,
                    before,
                    {"status": updated["status"], "next_run_at": updated["next_run_at"]},
                    "page",
                    task.get("plan_id"),
                )
                conn.commit()
                return self.send_json({"ok": True, "data": {"scheduled_inspection": serialize_scheduled_task(conn, updated)}})
            feedback_match = re.match(r"^/api/events/([^/]+)/feedback$", path)
            if feedback_match:
                if online_agent_for_tenant(conn, user["tenant_id"]):
                    raise ApiError(
                        "INTEGRATION_READ_ONLY",
                        HTTPStatus.CONFLICT,
                        {"message": f"{tenant_name_for_code(conn, user['tenant_id'])} 当前接入为线上只读模式，告警反馈未执行"},
                    )
                body["event_id"] = feedback_match.group(1)
                result = create_feedback(conn, user, body, None, "page")
                conn.commit()
                return self.send_json({"ok": True, "data": result}, HTTPStatus.CREATED)
            if path == "/api/analytics/query":
                online = online_agent_for_tenant(conn, user["tenant_id"])
                if online:
                    response = online.handle_message(body.get("question", ""), body.get("context", {}), [])
                    if not response.get("analytics"):
                        raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "这句话未被识别为统计分析请求"})
                    log_audit(
                        conn,
                        user["user_id"],
                        user["tenant_id"],
                        "agent.online.analytics",
                        "query",
                        response["analytics"]["query_id"],
                        None,
                        {"source": "deepvision_online", "intent": response.get("intent")},
                        "agent",
                        None,
                    )
                    conn.commit()
                    return self.send_json({"ok": True, "data": {"analytics": response["analytics"]}})
                result = analytics_query(conn, user, body.get("question", ""), body.get("context", {}))
                conn.commit()
                return self.send_json({"ok": True, "data": {"analytics": result}})
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)

    def route_delete(self):
        path = urlparse(self.path).path
        with connect() as conn:
            user = user_from_request(self, conn)
            research_memory_match = re.match(r"^/api/open-research/memories/([^/]+)$", path)
            if research_memory_match:
                deleted = delete_memory(
                    conn, memory_id=research_memory_match.group(1), tenant_id=user["tenant_id"], user_id=user["user_id"],
                    now=datetime.now(CN_TZ),
                )
                if not deleted:
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                log_audit(conn, user["user_id"], user["tenant_id"], "open_research.memory.delete", "open_research_memory", research_memory_match.group(1), None,
                          {"deleted": True}, "open_research", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"memory_id": research_memory_match.group(1), "status": "DELETED"}})
            office_asset_match = re.match(r"^/api/office/assets/([^/]+)$", path)
            if office_asset_match:
                service = office_asset_service_for_request(conn, user)
                if not service.delete(asset_id=office_asset_match.group(1), tenant_id=user["tenant_id"], user_id=user["user_id"]):
                    raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
                log_audit(conn, user["user_id"], user["tenant_id"], "office.asset.delete", "office_asset", office_asset_match.group(1), None,
                          {"deleted": True}, "office_agent", None)
                conn.commit()
                return self.send_json({"ok": True, "data": {"asset_id": office_asset_match.group(1), "status": "DELETED"}})
            manifest_match = re.match(r"^/api/agent/manifests/([^/]+)$", path)
            if manifest_match:
                manifest = delete_agent_manifest_import(conn, user, manifest_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"manifest": manifest}})
            memory_match = re.match(r"^/api/agent/memories/([^/]+)$", path)
            if memory_match:
                memory = delete_agent_memory(conn, user, memory_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"memory": memory}})
            knowledge_match = re.match(r"^/api/agent/knowledge/([^/]+)$", path)
            if knowledge_match:
                knowledge = delete_agent_knowledge_item(conn, user, knowledge_match.group(1))
                conn.commit()
                return self.send_json({"ok": True, "data": {"knowledge": knowledge}})
            if path == "/api/conversations":
                body = self.read_json()
                requested_ids = body.get("conversation_ids")
                if not isinstance(requested_ids, list):
                    raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "conversation_ids must be a list"})
                conversation_ids = []
                seen = set()
                for value in requested_ids:
                    conversation_id = str(value or "").strip()
                    if conversation_id and conversation_id not in seen:
                        seen.add(conversation_id)
                        conversation_ids.append(conversation_id)
                if not conversation_ids:
                    return self.send_json({"ok": True, "data": {"closed_count": 0, "conversation_ids": []}})
                placeholders = ",".join("?" for _ in conversation_ids)
                active_conversations = rows(
                    conn,
                    f"""
                    SELECT conversation_id, status
                    FROM conversations
                    WHERE user_id=? AND tenant_id=? AND status='ACTIVE'
                      AND conversation_id IN ({placeholders})
                    """,
                    (user["user_id"], user["tenant_id"], *conversation_ids),
                )
                closed_ids = [item["conversation_id"] for item in active_conversations]
                if not closed_ids:
                    return self.send_json({"ok": True, "data": {"closed_count": 0, "conversation_ids": []}})
                closed_at = now_iso()
                close_placeholders = ",".join("?" for _ in closed_ids)
                conn.execute(
                    f"UPDATE conversations SET status='CLOSED', updated_at=? WHERE conversation_id IN ({close_placeholders})",
                    (closed_at, *closed_ids),
                )
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "conversation.clear",
                    "conversation_history",
                    "bulk",
                    {"conversation_ids": closed_ids, "status": "ACTIVE"},
                    {"conversation_ids": closed_ids, "status": "CLOSED", "closed_at": closed_at, "closed_count": len(closed_ids)},
                    "page",
                    None,
                )
                conn.commit()
                return self.send_json({"ok": True, "data": {"closed_count": len(closed_ids), "conversation_ids": closed_ids}})
            conversation_match = re.match(r"^/api/conversations/([^/]+)$", path)
            if not conversation_match:
                raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
            conversation_id = conversation_match.group(1)
            conversation = one(
                conn,
                "SELECT * FROM conversations WHERE conversation_id=? AND user_id=? AND tenant_id=? AND status='ACTIVE'",
                (conversation_id, user["user_id"], user["tenant_id"]),
            )
            if not conversation:
                raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
            closed_at = now_iso()
            conn.execute(
                "UPDATE conversations SET status='CLOSED', updated_at=? WHERE conversation_id=?",
                (closed_at, conversation_id),
            )
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "conversation.close",
                "conversation",
                conversation_id,
                {"status": conversation["status"]},
                {"status": "CLOSED", "closed_at": closed_at},
                "page",
                None,
            )
            conn.commit()
            return self.send_json(
                {
                    "ok": True,
                    "data": {"conversation_id": conversation_id, "status": "CLOSED"},
                }
            )

    def route_put(self):
        path = urlparse(self.path).path
        body = self.read_json()
        with connect() as conn:
            user = user_from_request(self, conn)
            catalog_sku_match = re.match(r"^/v1/catalog/skus/([^/]+)$", path)
            if catalog_sku_match:
                sku = update_catalog_sku(conn, user, catalog_sku_match.group(1), body, self.headers.get("If-Match") or "")
                conn.commit()
                return self.send_json({"ok": True, "data": {"sku": sku}})
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)

    def route_patch(self):
        path = urlparse(self.path).path
        body = self.read_json()
        with connect() as conn:
            user = user_from_request(self, conn)
            knowledge_match = re.match(r"^/api/agent/knowledge/([^/]+)$", path)
            if knowledge_match:
                knowledge = update_agent_knowledge_item(conn, user, knowledge_match.group(1), body)
                conn.commit()
                return self.send_json({"ok": True, "data": {"knowledge": knowledge}})
        raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)

    def serve_static(self, relative_path: str):
        relative_path = "index.html" if relative_path in {"", "/"} else relative_path
        target = (STATIC_DIR / relative_path).resolve()
        if not target.exists() and relative_path.startswith("evidence/") and relative_path.endswith(".svg"):
            return self.serve_generated_evidence(relative_path)
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or not target.is_file():
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        data = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            mime = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime in {"text/javascript", "image/svg+xml"} else mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_scheduled_evidence(self, evidence_id: str, access_token: str):
        with connect() as conn:
            evidence = one(
                conn,
                "SELECT * FROM scheduled_evidence WHERE evidence_id=? AND access_token=?",
                (evidence_id, access_token),
            )
        if not evidence:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        storage_path = Path(evidence["storage_path"]).resolve()
        evidence_root = SCHEDULED_EVIDENCE_DIR.resolve()
        if not str(storage_path).startswith(str(evidence_root)) or not storage_path.exists():
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        data = storage_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", evidence["mime_type"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def serve_online_snapshot_evidence(self, evidence_id: str, access_token: str):
        with connect() as conn:
            evidence = one(
                conn,
                "SELECT * FROM online_snapshot_evidence WHERE evidence_id=? AND access_token=?",
                (evidence_id, access_token),
            )
        if not evidence:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        storage_path = Path(evidence["storage_path"]).resolve()
        evidence_root = ONLINE_SNAPSHOT_EVIDENCE_DIR.resolve()
        if not str(storage_path).startswith(str(evidence_root)) or not storage_path.is_file():
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        data = storage_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", evidence["mime_type"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def serve_open_qa_document(self, conn, user: dict, conversation_id: str, document_id: str):
        conversation = one(
            conn,
            "SELECT * FROM conversations WHERE conversation_id=? AND user_id=? AND tenant_id=? AND status='ACTIVE'",
            (conversation_id, user["user_id"], user["tenant_id"]),
        )
        if not conversation:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        target = _open_qa_document_path(user["tenant_id"], conversation_id, document_id)
        if not target.exists() or not target.is_file():
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        data = target.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        log_audit(
            conn,
            user["user_id"],
            user["tenant_id"],
            "agent.document.download",
            "open_qa_document",
            document_id,
            None,
            {"conversation_id": conversation_id, "mime_type": "application/pdf", "size_bytes": len(data)},
            "chat",
            None,
        )
        conn.commit()
        filename = f"open-qa-{document_id}.pdf"
        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def serve_generated_evidence(self, relative_path: str):
        label = Path(relative_path).stem.upper()
        palette = "#246BFE" if "10231" in label or "LEAVE" in label else "#17A66A"
        if "SM" in label:
            palette = "#F59E0B"
        if "10234" in label or "FIRE" in label:
            palette = "#D93026"
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="720" height="420" viewBox="0 0 720 420">
  <rect width="720" height="420" fill="#172033"/>
  <rect x="36" y="34" width="648" height="352" rx="10" fill="#2a3447"/>
  <path d="M48 310 C140 260 214 288 302 240 C394 190 482 210 670 132 L670 386 L48 386 Z" fill="#3d4b62"/>
  <rect x="112" y="108" width="120" height="180" rx="6" fill="#59687c"/>
  <rect x="254" y="142" width="92" height="146" rx="6" fill="#4c5a70"/>
  <rect x="432" y="94" width="178" height="194" rx="6" fill="#526176"/>
  <rect x="120" y="118" width="42" height="62" fill="#8fa3ba"/>
  <rect x="444" y="108" width="58" height="76" fill="#8fa3ba"/>
  <rect x="150" y="204" width="78" height="84" fill="#303a4d"/>
  <rect x="286" y="202" width="52" height="86" fill="#303a4d"/>
  <rect x="92" y="76" width="312" height="210" fill="none" stroke="{palette}" stroke-width="5" stroke-dasharray="13 9"/>
  <circle cx="96" cy="76" r="8" fill="{palette}"/>
  <rect x="36" y="34" width="648" height="42" fill="#0f1728"/>
  <text x="58" y="61" font-family="Arial, sans-serif" font-size="18" fill="#e5e7eb">EVIDENCE {label}</text>
  <text x="520" y="61" font-family="Arial, sans-serif" font-size="14" fill="#cbd5e1">bbox / model frame</text>
</svg>"""
        data = svg.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.add_cors()
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_bootstrap(self, conn, user):
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if online:
            if user["tenant_id"] == environment_tenant_code():
                data = online.bootstrap(user)
                data["integration"]["tenant_name"] = tenant_name_for_code(conn, user["tenant_id"])
                data["agent_catalog"] = agent_catalog_payload(conn, user)
                return self.send_json({"ok": True, "data": data})
            integration = one(conn, "SELECT * FROM tenant_integrations WHERE tenant_code=?", (user["tenant_id"],))
            stores = rows(
                conn,
                """SELECT org_id, parent_id, name, org_type, status, camera_count, synced_at
                   FROM tenant_integration_stores WHERE integration_id=? ORDER BY name""",
                (integration["integration_id"],),
            )
            orgs = [
                {
                    "org_id": item["org_id"],
                    "tenant_id": user["tenant_id"],
                    "parent_id": item.get("parent_id"),
                    "name": item["name"],
                    "org_type": item.get("org_type") or "store",
                    "status": item.get("status"),
                    "camera_count": item.get("camera_count"),
                }
                for item in stores
            ]
            return self.send_json(
                {
                    "ok": True,
                    "data": {
                        "user": dict(user),
                        "orgs": orgs,
                        "cameras": [],
                        "capabilities": [],
                        "events": [],
                        "today": CURRENT_DATE.isoformat(),
                        "integration": {
                            "mode": "deepvision_online",
                            "tenant_code": user["tenant_id"],
                            "tenant_name": integration["tenant_name"],
                            "read_only": True,
                            "write_enabled": False,
                            "intent_engine": "llm" if online.analyzer.configured else "local_fallback",
                            "warnings": [],
                            "refreshed_at": now_iso(),
                            "store_count": len(orgs),
                            "lazy_store_data": True,
                        },
                        "agent_skills": public_skill_catalog(),
                        "agent_catalog": agent_catalog_payload(conn, user),
                    },
                }
            )
        allowed = sorted(allowed_org_ids(conn, user))
        orgs = rows(conn, f"SELECT * FROM orgs WHERE org_id IN ({','.join('?' for _ in allowed)}) ORDER BY org_type, name", allowed)
        cameras = rows(conn, f"SELECT * FROM cameras WHERE org_id IN ({','.join('?' for _ in allowed)}) ORDER BY org_id, name", allowed)
        caps = rows(conn, "SELECT * FROM capabilities WHERE status='ACTIVE' ORDER BY name")
        latest_events = rows(conn, f"SELECT * FROM events WHERE org_id IN ({','.join('?' for _ in allowed)}) ORDER BY started_at DESC LIMIT 6", allowed)
        return self.send_json(
            {
                "ok": True,
                "data": {
                    "user": dict(user),
                    "orgs": orgs,
                    "cameras": [serialize_camera(c) for c in cameras],
                    "capabilities": [{**c, "aliases": json_loads(c["aliases"], []), "thresholds_default": json_loads(c["thresholds_default"], {})} for c in caps],
                    "events": [serialize_event(conn, e) for e in latest_events],
                    "today": CURRENT_DATE.isoformat(),
                    "agent_skills": public_skill_catalog(),
                    "agent_catalog": agent_catalog_payload(conn, user),
                },
            }
        )

    def api_send_message(self, conn, user, conversation_id: str, body: dict):
        conversation = one(
            conn,
            "SELECT * FROM conversations WHERE conversation_id=? AND tenant_id=? AND status='ACTIVE'",
            (conversation_id, user["tenant_id"]),
        )
        if not conversation:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"conversation_id": conversation_id})
        if conversation["user_id"] != user["user_id"]:
            raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
        content = body.get("content", "").strip()
        if not content:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "content is required"})
        context = body.get("context", {})
        mode_override = str(context.get("mode_override") or "AUTO").upper()
        if mode_override not in {"AUTO", "OPEN_QA", "INSPECTION"}:
            mode_override = "AUTO"
        is_integration_request = integration_setup_request(content)
        safe_content = redact_integration_message(content) if is_integration_request else content
        has_user_message = one(
            conn,
            "SELECT message_id FROM messages WHERE conversation_id=? AND sender='user' LIMIT 1",
            (conversation_id,),
        )
        if not has_user_message and conversation.get("title") in {"", "新的巡检对话"}:
            generated_title = re.sub(r"\s+", " ", safe_content).strip()[:36]
            conn.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE conversation_id=?",
                (generated_title or "新的巡检对话", now_iso(), conversation_id),
            )
        if is_integration_request:
            if not role_can_manage_integrations(user["role"]):
                raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
            user_message = add_message(conn, conversation_id, "user", safe_content)
            parsed_credentials = parse_integration_credentials(content)
            previous_prefill = latest_integration_setup_prefill(conn, conversation_id)
            credentials = {**previous_prefill, **parsed_credentials}
            required = ("tenant_code", "app_key", "app_secret")
            if all(credentials.get(key) for key in required):
                existing = one(
                    conn,
                    "SELECT * FROM tenant_integrations WHERE tenant_code=?",
                    (credentials["tenant_code"],),
                )
                try:
                    integration = serialize_integration_row(conn, existing) if existing else create_tenant_integration(conn, user, credentials)
                except (ApiError, OnlineAgentError) as exc:
                    detail = exc.detail.get("message") if isinstance(exc, ApiError) and isinstance(exc.detail, dict) else None
                    reason = detail or (exc.message if isinstance(exc, OnlineAgentError) else ERRORS.get(exc.code, "连接验证失败"))
                    agent = {
                        "intent": "CONFIGURE_TENANT_INTEGRATION",
                        "skill": "secure_tenant_onboarding",
                        "engine": "secure_integration_manager",
                        "status": "BLOCKED",
                        "tool_calls": ["credential.redact", "paas.auth.verify:failed"],
                    }
                    linked = attach_agent_trace(
                        {"agent": agent, "source": "integration_manager"},
                        safe_content,
                    )
                    assistant = add_message(
                        conn,
                        conversation_id,
                        "assistant",
                        f"租户接入未完成：{reason}。本次凭证没有保存，请检查后重试。",
                        None,
                        linked,
                    )
                    log_audit(
                        conn,
                        user["user_id"],
                        user["tenant_id"],
                        "integration.create.failed",
                        "tenant_integration",
                        credentials["tenant_code"],
                        None,
                        {"reason_code": exc.code, "credentials_saved": False},
                        "agent_secure_chat",
                        None,
                    )
                    return {
                        "intent": "CONFIGURE_TENANT_INTEGRATION",
                        "confidence": 0.99,
                        "agent": linked["agent"],
                        "messages": [serialize_message(assistant)],
                    }
                assistant = integration_result_message(conn, conversation_id, integration)
                serialized_assistant = serialize_message(assistant)
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "integration.chat.complete",
                    "tenant_integration",
                    integration["integration_id"],
                    None,
                    {"tenant_code": integration["tenant_code"], "store_count": integration["store_count"], "deduped": bool(existing)},
                    "agent_secure_chat",
                    None,
                )
                return {
                    "intent": "CONFIGURE_TENANT_INTEGRATION",
                    "confidence": 0.99,
                    "integration": integration_artifact_summary(integration),
                    "agent": serialized_assistant["linked_object"]["agent"],
                    "messages": [serialized_assistant],
                }
            response_prefill = integration_setup_prefill(credentials, include_secrets=True)
            stored_prefill = integration_setup_prefill(credentials, include_secrets=False)
            setup_missing_fields = integration_setup_missing_fields(response_prefill)
            stored_missing_fields = integration_setup_missing_fields(stored_prefill)
            secure_prefill_fields = sorted(
                field for field in INTEGRATION_SECRET_FIELDS if response_prefill.get(field)
            )
            setup = {
                "mode": "CREATE",
                "title": "安全接入 DeepVision 租户",
                "description": "填写租户信息并验证连接。AppSecret 不会写入对话历史或审计日志。",
                "required_fields": ["tenant_name", "tenant_code", "app_key", "app_secret"],
                "prefill": stored_prefill,
                "missing_fields": stored_missing_fields,
                "auto_extract": bool(stored_prefill or secure_prefill_fields),
                "secure_prefill_fields": secure_prefill_fields,
            }
            response_setup = {
                **setup,
                "prefill": response_prefill,
                "missing_fields": setup_missing_fields,
                "auto_extract": bool(response_prefill),
                "transient_secret_prefill": bool(secure_prefill_fields),
            }
            agent = {
                "intent": "CONFIGURE_TENANT_INTEGRATION",
                "skill": "secure_tenant_onboarding",
                "engine": "deterministic_secure_form",
                "status": "NEED_CLARIFICATION",
                "tool_calls": [],
            }
            linked = attach_agent_trace(
                {"artifact": {"integrationSetup": setup}, "agent": agent, "source": "integration_manager"},
                safe_content,
            )
            assistant = add_message(
                conn,
                conversation_id,
                "assistant",
                integration_setup_assistant_copy(setup["prefill"], response_setup["missing_fields"], secure_prefill_fields),
                None,
                linked,
            )
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "integration.setup.request",
                "conversation",
                conversation_id,
                None,
                {"intent": "CONFIGURE_TENANT_INTEGRATION", "secret_redacted": safe_content != content},
                "agent",
                None,
            )
            serialized_assistant = serialize_message(assistant)
            linked_object = serialized_assistant.get("linked_object")
            if isinstance(linked_object, dict):
                artifact = linked_object.setdefault("artifact", {})
                if isinstance(artifact, dict):
                    artifact["integrationSetup"] = response_setup
            return {
                "intent": "CONFIGURE_TENANT_INTEGRATION",
                "confidence": 0.99,
                "integration_setup": response_setup,
                "agent": linked["agent"],
                "messages": [serialized_assistant],
            }
        if mode_override != "INSPECTION" and is_open_qa_pdf_followup(content):
            export_source = latest_open_qa_export_source(conn, conversation_id)
            if export_source:
                response = open_qa_pdf_followup_response(export_source, mode_selection=mode_override)
                return complete_open_qa_message(conn, user, conversation_id, content, response)
        attachment_ids = body.get("attachment_ids") or []
        if not isinstance(attachment_ids, list) or any(not isinstance(item, str) or not item.strip() for item in attachment_ids):
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "attachment_ids 必须是文件 ID 数组"})
        if len(attachment_ids) > 3:
            raise ApiError("OFFICE_BATCH_FILE_LIMIT_EXCEEDED", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        context, active_conversation_context, continuation_decision = prepare_conversation_turn_context(
            conn,
            user,
            conversation_id,
            content,
            context,
            mode_override,
        )
        if (
            continuation_decision.get("decision") == "CLARIFY"
            and continuation_decision.get("domain") == "VISUAL_INSPECTION"
        ):
            current_scope = public_context_summary(active_conversation_context) if active_conversation_context else {}
            current_scope["scope_operation"] = "CLARIFY_SCOPE"
            current_scope["reason_code"] = continuation_decision.get("reason_code")
            page_name = str((context.get("page_scope") or {}).get("org_name") or "页面当前门店")
            active_names = list(((active_conversation_context or {}).get("task_scope") or {}).get("org_names") or [])
            active_label = "、".join(active_names) or "上一轮任务门店"
            assistant_content = (
                f"你说的“这家/这里”可能指页面当前门店“{page_name}”，"
                f"也可能指上一轮实际检查的“{active_label}”。"
                "请说“当前门店”或“刚才那家”，我再继续取证。"
            )
            agent = {
                "intent": "CLARIFY_VISUAL_SCOPE",
                "mode": "INSPECTION",
                "engine": "conversation_context_resolver",
                "status": "NEED_CLARIFICATION",
                "tool_calls": ["conversation.context.resolve", "scope.clarify"],
                "stages": ["UNDERSTAND", "RESOLVE_SCOPE", "REQUEST_CLARIFICATION"],
                "decision": continuation_decision,
            }
            linked = attach_agent_trace(
                {
                    "source": "conversation_context",
                    "agent": agent,
                    "artifact": {"conversationScope": current_scope},
                },
                content,
            )
            user_message = add_message(conn, conversation_id, "user", content)
            assistant = add_message(conn, conversation_id, "assistant", assistant_content, None, linked)
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "conversation.scope.clarify",
                "message",
                user_message["message_id"],
                None,
                {
                    "reason_code": continuation_decision.get("reason_code"),
                    "page_org_id": (context.get("page_scope") or {}).get("org_id"),
                    "active_org_ids": list(((active_conversation_context or {}).get("task_scope") or {}).get("org_ids") or []),
                },
                "agent",
                None,
            )
            return {
                "intent": "CLARIFY_VISUAL_SCOPE",
                "confidence": continuation_decision.get("confidence"),
                "conversation_context": current_scope,
                "agent": linked["agent"],
                "messages": [serialize_message(assistant)],
            }
        continuation_is_visual = (
            continuation_decision.get("decision") == "CONTINUE"
            and continuation_decision.get("domain") == "VISUAL_INSPECTION"
        )
        routing_content = continuation_decision.get("effective_query") if continuation_is_visual else content
        scope_routing_content = content if continuation_is_visual else routing_content
        routing_mode = "INSPECTION" if continuation_is_visual else mode_override
        if continuation_is_visual:
            context["effective_visual_query"] = routing_content
        followup_query = None if routing_mode == "INSPECTION" else realtime_research_followup_query(conn, user, conversation_id, content)
        if followup_query:
            result = open_research_service_for_request(conn, user).run(
                tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=conversation_id,
                question=content, planning_query=followup_query, force_refresh=True,
            )
            return add_open_research_message(conn, user, conversation_id, content, result)
        # The new domains receive one narrow, high-confidence chance before the
        # legacy OPEN_QA/online path.  INSPECTION is a hard mode lock and the
        # router has no side effects, so the existing inspection path is intact.
        domain_route = DomainRouter().classify(routing_content, mode_override=routing_mode, attachment_ids=attachment_ids)
        # A high-confidence OPEN_RESEARCH route must never silently fall back
        # to the legacy OPEN_QA search integration.  That integration can be
        # configured with non-P0 providers (for example Brave); letting it run
        # here would violate the Tavily-only contract even though the router
        # already classified the request as evidence-first research.  The
        # service evaluates G0 before provider creation, and returns the safe
        # SEARCH_UNAVAILABLE degradation when Tavily is absent or unapproved.
        if domain_route["domain"] == "OPEN_RESEARCH":
            result = open_research_service_for_request(conn, user).run(
                tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=conversation_id, question=content,
            )
            return add_open_research_message(conn, user, conversation_id, content, result)
        if domain_route["domain"] == "CLARIFY":
            user_message = add_message(conn, conversation_id, "user", content)
            assistant = add_message(
                conn, conversation_id, "assistant",
                "我同时识别到了公开检索和 Office 需求，但没有看到明确依赖关系。请说明是“先查公开信息再做 PPT”，还是分别处理。",
                None, {"source": "agent_governance", "agent": {"intent": "CLARIFY_CROSS_DOMAIN", "status": "NEED_CLARIFICATION", "tool_calls": []}},
            )
            return {"intent": "CLARIFY_CROSS_DOMAIN", "confidence": domain_route["confidence"], "messages": [serialize_message(assistant)]}
        if domain_route["domain"] == "OFFICE_EGRESS":
            engine, gate_context = new_domain_gate_engine(conn, user, domain="HYBRID", action="OFFICE_TO_RESEARCH", conversation_id=conversation_id, input_value={"attachment_ids": attachment_ids, "question": content})
            decision = engine.record(gate_context, GateDecision("G2", "BLOCK", "OFFICE_TO_RESEARCH_EGRESS_DISABLED"))
            user_message = add_message(conn, conversation_id, "user", content)
            assistant = add_message(
                conn, conversation_id, "assistant",
                "P0 不会把内部 Office 内容、字段或附件发送给公开搜索服务。请改为直接输入不含内部数据的公开问题，或等待后续逐条 Query 确认能力开放。",
                None, {"source": "agent_governance", "agent": {"intent": "OFFICE_TO_RESEARCH", "status": "BLOCKED", "reason_code": decision.reason_code, "tool_calls": []}},
            )
            return {"intent": "OFFICE_TO_RESEARCH", "confidence": domain_route["confidence"], "reason_code": decision.reason_code, "messages": [serialize_message(assistant)]}
        if domain_route["domain"] == "OFFICE":
            job_result = office_job_service_for_request(conn, user).create_ppt_job(
                tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=conversation_id,
                asset_ids=attachment_ids, title=content, mode_override=mode_override,
            )
            user_message = add_message(conn, conversation_id, "user", content)
            job = job_result.get("job") if isinstance(job_result.get("job"), dict) else None
            blocked = job_result.get("status") == "BLOCKED"
            response_text = (
                "Office 任务未创建：" + str(job_result.get("reason_code") or "请检查文件和权限。")
                if blocked else
                (f"已创建 Office 任务 {job.get('job_id')}，当前阶段：{job.get('stage')}。文件将在独立 Worker 完成提取、生成与渲染校验后交付。" if job else "请先上传 Excel 或 Word 文件，再发起管理层 PPT 生成。")
            )
            linked = {"source": "office_agent", "agent": {"intent": "OFFICE_CREATE_PPT", "mode": "OFFICE", "status": "BLOCKED" if blocked else "QUEUED", "tool_calls": []}, "artifact": {"office": job or {"reason_code": job_result.get("reason_code")}}}
            assistant = add_message(conn, conversation_id, "assistant", response_text, None, linked)
            return {"intent": "OFFICE_CREATE_PPT", "confidence": domain_route["confidence"], "office": job_result, "agent": linked["agent"], "messages": [serialize_message(assistant)]}
        if domain_route["domain"] == "HYBRID":
            workflow = create_workflow(
                conn, tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=conversation_id,
                kind="RESEARCH_TO_OFFICE", input_value={"question": content, "attachment_ids": attachment_ids}, now=now_iso(),
            )
            research = open_research_service_for_request(conn, user).run(
                tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=conversation_id,
                question=content, workflow_id=workflow["workflow_id"],
            )
            update_workflow(conn, workflow["workflow_id"], status="RUNNING" if research.get("run_id") else "FAILED", now=now_iso(), research_run_id=research.get("run_id"))
            research_response = add_open_research_message(conn, user, conversation_id, content, research)
            office_result = None
            if research.get("status") in {"VERIFIED", "PARTIALLY_VERIFIED"} and research.get("brief"):
                office_result = office_job_service_for_request(conn, user).create_ppt_job(
                    tenant_id=user["tenant_id"], user_id=user["user_id"], conversation_id=conversation_id,
                    asset_ids=attachment_ids, brief_id=research["brief"]["brief_id"], title=content,
                    workflow_id=workflow["workflow_id"], mode_override=mode_override,
                )
                job = office_result.get("job") if isinstance(office_result.get("job"), dict) else None
                update_workflow(conn, workflow["workflow_id"], status="RUNNING" if job else "PARTIAL", now=now_iso(), office_job_id=job.get("job_id") if job else None)
                note = "已将已核验的 ResearchBrief 交给 Office 任务处理。" if job else f"公开检索已完成，但 Office 未创建：{office_result.get('reason_code')}。"
            else:
                update_workflow(conn, workflow["workflow_id"], status="PARTIAL", now=now_iso())
                note = "公开检索未形成可用于事实性汇报的 ResearchBrief，因此未创建 Office 任务。"
            follow_up = add_message(conn, conversation_id, "assistant", note, None, {"source": "agent_workflow", "artifact": {"workflow_id": workflow["workflow_id"], "office": office_result}})
            research_response["intent"] = "RESEARCH_TO_OFFICE"
            research_response["workflow"] = get_workflow(conn, workflow["workflow_id"], user["tenant_id"], user["user_id"])
            research_response["office"] = office_result
            research_response["messages"].append(serialize_message(follow_up))
            return research_response
        if mode_override == "OPEN_QA":
            history = open_qa_history(conn, conversation_id)
            response = open_question_responder_for_request(conn, user["tenant_id"]).agent_response(
                content,
                force_open=True,
                mode_selection="OPEN_QA",
                history=history,
            )
            return complete_open_qa_message(conn, user, conversation_id, content, response)
        pending_scheduled_row = one(
            conn,
            """SELECT * FROM plans
               WHERE conversation_id=?
                 AND intent IN ('CREATE_SCHEDULED_INSPECTION', 'BATCH_SCHEDULED_INSPECTION_CREATE', 'BATCH_INSPECTION_EXECUTE')
                 AND status='NEED_CLARIFICATION'
               ORDER BY created_at DESC LIMIT 1""",
            (conversation_id,),
        )
        pending_scheduled = serialize_plan(pending_scheduled_row) if pending_scheduled_row else None
        # An unfinished inspection plan may consume slot supplements, but it
        # must not capture an explicit new Office/public-data/OpenQA request.
        # The active visual context remains available for a later explicit
        # return; only this turn is isolated from the pending plan.
        if continuation_decision.get("reason_code") in {
            "EXPLICIT_CROSS_DOMAIN_REQUEST",
            "EXPLICIT_CONTEXT_RESET",
            "EXPLICIT_OPEN_QA_MODE",
        }:
            pending_scheduled = None
        pending_batch_visual = pending_scheduled if pending_scheduled and pending_scheduled.get("intent") == "BATCH_INSPECTION_EXECUTE" else None
        continuation_scope_operation = str(continuation_decision.get("scope_operation") or "KEEP_SCOPE")
        active_continuation_org_ids = list((continuation_decision.get("active_task_scope") or {}).get("org_ids") or [])
        continuation_requires_batch = continuation_is_visual and (
            continuation_scope_operation in {"EXPAND_SCOPE", "COMPARE_SCOPE"}
            or (len(active_continuation_org_ids) > 1 and continuation_scope_operation in {"KEEP_SCOPE", "NARROW_SCOPE"})
        )
        if is_batch_visual_inspection_request(scope_routing_content) or continuation_requires_batch or pending_batch_visual:
            user_message = add_message(conn, conversation_id, "user", content)
            plan, assistant_content = build_batch_visual_inspection_plan(
                conn,
                user,
                conversation_id,
                scope_routing_content,
                context,
                pending_batch_visual,
            )
            agent = {
                "intent": "BATCH_INSPECTION_EXECUTE",
                "skill": "multi_store_visual_inspection",
                "engine": "deterministic_batch_visual_planner",
                "status": "NEED_CLARIFICATION" if plan.get("status") == "NEED_CLARIFICATION" else "SUCCEEDED",
                "tool_calls": [
                    "conversation.context.resolve",
                    "permission.scope.check",
                    "scope.resolve",
                    "paas.org.resolve",
                    "paas.camera.page",
                    "batch_inspection.execute",
                ],
            }
            persisted_context = persist_plan_conversation_context(
                conn,
                user,
                conversation_id,
                context,
                plan,
                routing_content,
            )
            context_artifact = (
                {"conversationScope": {key: value for key, value in persisted_context.items() if key != "status"}}
                if isinstance(persisted_context, dict) and persisted_context.get("status") == "ACTIVE"
                else {}
            )
            linked = attach_agent_trace(
                {"plan": plan, "agent": agent, "source": "batch_visual_inspection", "artifact": context_artifact},
                content,
            )
            assistant = add_message(
                conn,
                conversation_id,
                "assistant",
                assistant_content,
                plan["plan_id"],
                linked,
            )
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "inspection_batch.plan",
                "plan",
                plan["plan_id"],
                None,
                {"status": plan["status"], "missing_slots": plan["slots"].get("missing_slots", []), "execution_mode": "immediate"},
                "agent",
                plan["plan_id"],
            )
            return {
                "intent": "BATCH_INSPECTION_EXECUTE",
                "confidence": 0.98,
                "plan": plan,
                "conversation_context": persisted_context,
                "agent": linked["agent"],
                "messages": [serialize_message(assistant)],
            }
        pending_periodic = pending_scheduled if pending_scheduled and pending_scheduled.get("intent") != "BATCH_INSPECTION_EXECUTE" else None
        if is_scheduled_inspection_request(scope_routing_content) or pending_periodic:
            user_message = add_message(conn, conversation_id, "user", content)
            should_use_batch = (
                (pending_periodic and pending_periodic.get("intent") == "BATCH_SCHEDULED_INSPECTION_CREATE")
                or is_batch_scheduled_inspection_request(scope_routing_content)
            )
            if should_use_batch:
                plan, assistant_content = build_batch_scheduled_inspection_plan(
                    conn,
                    user,
                    conversation_id,
                    scope_routing_content,
                    context,
                    pending_periodic,
                )
                agent = {
                    "intent": "BATCH_SCHEDULED_INSPECTION_CREATE",
                    "skill": "multi_store_scheduled_inspection",
                    "engine": "deterministic_batch_scheduler_planner",
                    "status": "SUCCEEDED",
                    "tool_calls": ["paas.org.resolve", "paas.camera.page", "batch.scheduler.plan.validate"],
                }
                source = "batch_scheduled_inspection"
                response_intent = "BATCH_SCHEDULED_INSPECTION_CREATE"
            else:
                plan, assistant_content = build_scheduled_inspection_plan(
                    conn,
                    user,
                    conversation_id,
                    scope_routing_content,
                    context,
                    pending_periodic,
                )
                agent = {
                    "intent": "CREATE_SCHEDULED_INSPECTION",
                    "skill": "scheduled_snapshot_inspection",
                    "engine": "deterministic_scheduler_planner",
                    "status": "SUCCEEDED",
                    "tool_calls": ["paas.camera.page", "scheduler.plan.validate"],
                }
                source = "scheduled_inspection"
                response_intent = "CREATE_SCHEDULED_INSPECTION"
            agent = {
                **agent,
                "status": "NEED_CLARIFICATION" if plan.get("status") == "NEED_CLARIFICATION" else "SUCCEEDED",
            }
            knowledge_hits = plan.get("slots", {}).get("knowledge_hits", [])
            if isinstance(knowledge_hits, list) and knowledge_hits:
                agent["knowledge_hits"] = knowledge_hits
                agent["tool_calls"] = ["knowledge.retrieve", *(agent.get("tool_calls") or [])]
            persisted_context = persist_plan_conversation_context(
                conn,
                user,
                conversation_id,
                context,
                plan,
                routing_content,
            )
            context_artifact = (
                {"conversationScope": {key: value for key, value in persisted_context.items() if key != "status"}}
                if isinstance(persisted_context, dict) and persisted_context.get("status") == "ACTIVE"
                else {}
            )
            agent["tool_calls"] = [
                "conversation.context.resolve",
                "permission.scope.check",
                "scope.resolve",
                *[item for item in agent.get("tool_calls") or [] if item not in {"conversation.context.resolve", "permission.scope.check", "scope.resolve"}],
            ]
            linked = attach_agent_trace(
                {"plan": plan, "agent": agent, "source": source, "artifact": context_artifact},
                content,
            )
            assistant = add_message(
                conn,
                conversation_id,
                "assistant",
                assistant_content,
                plan["plan_id"],
                linked,
            )
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "inspection_batch.plan" if should_use_batch else "scheduled_inspection.plan",
                "plan",
                plan["plan_id"],
                None,
                {"status": plan["status"], "missing_slots": plan["slots"].get("missing_slots", [])},
                "agent",
                plan["plan_id"],
            )
            return {
                "intent": response_intent,
                "confidence": 0.98,
                "plan": plan,
                "conversation_context": persisted_context,
                "agent": linked["agent"],
                "messages": [serialize_message(assistant)],
            }
        subscription_intent, subscription_confidence = classify_intent(content)
        if subscription_intent == "SUBSCRIPTION_CREATE":
            user_message = add_message(conn, conversation_id, "user", content)
            plan, assistant_content, linked = build_subscription_plan(conn, user, conversation_id, content, context)
            agent = {
                "intent": "SUBSCRIPTION_CREATE",
                "skill": "subscription_create",
                "engine": "deterministic_subscription_planner",
                "status": "NEED_CLARIFICATION" if plan and plan.get("status") == "NEED_CLARIFICATION" else "SUCCEEDED",
                "tool_calls": ["capability.resolve", "permission.check", "camera.online_check", "subscription.create"],
            }
            linked = attach_agent_trace(
                {**linked, "plan": plan, "agent": agent, "source": "subscription_planner"},
                content,
            )
            assistant = add_message(conn, conversation_id, "assistant", assistant_content, plan["plan_id"] if plan else None, linked)
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "subscription.plan",
                "plan",
                plan["plan_id"] if plan else user_message["message_id"],
                None,
                {"status": plan.get("status") if plan else None, "missing_slots": plan.get("slots", {}).get("missing_slots", []) if plan else []},
                "agent",
                plan["plan_id"] if plan else None,
            )
            return {
                "intent": subscription_intent,
                "confidence": subscription_confidence,
                "plan": plan,
                "agent": linked.get("agent"),
                "messages": [serialize_message(assistant)],
                **{key: value for key, value in linked.items() if key not in {"plan", "agent", "source"}},
            }
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if online:
            history = open_qa_history(conn, conversation_id)
            user_message = add_message(conn, conversation_id, "user", content)
            try:
                response = online.handle_message(content, context, history)
            except OnlineAgentError as exc:
                delivery = public_online_delivery_failure(exc)
                failed_linked_object = json_loads(user_message.get("linked_object"), {}) or {}
                failed_linked_object["delivery"] = delivery
                conn.execute(
                    "UPDATE messages SET linked_object=? WHERE message_id=?",
                    (json_dumps(sanitize_linked_object_for_storage(failed_linked_object)), user_message["message_id"]),
                )
                failed_user_message = one(conn, "SELECT * FROM messages WHERE message_id=?", (user_message["message_id"],))
                log_audit(
                    conn,
                    user["user_id"],
                    user["tenant_id"],
                    "agent.online.query.failed",
                    "message",
                    user_message["message_id"],
                    None,
                    {
                        "failure_state": delivery["state"],
                        "failure_code": delivery["code"],
                        "retryable": delivery["retryable"],
                        "correlation_id": delivery["correlation_id"],
                    },
                    "agent",
                    None,
                )
                return {
                    "intent": "UNKNOWN",
                    "confidence": 0.0,
                    "delivery": delivery,
                    "message": serialize_message(failed_user_message),
                    "messages": [],
                }
            recovered_context_source = str(context.get("_conversation_context_recovered") or "")
            if recovered_context_source and isinstance(response.get("agent"), dict):
                tool_calls = response["agent"].setdefault("tool_calls", [])
                if "conversation.context.recover" not in tool_calls:
                    tool_calls.insert(0, "conversation.context.recover")
                response["agent"].setdefault("decision", {})["context_recovered_from"] = recovered_context_source
            apply_web_search_usage_to_response(conn, user, conversation_id, response)
            if (
                response.get("intent") == "OPEN_QA"
                or response.get("source") == "open_qa"
                or (response.get("agent") or {}).get("mode") == "OPEN_QA"
            ):
                # AUTO mode can route OPEN_QA through the tenant online agent.
                # Keep its post-processing aligned with forced OPEN_QA without
                # persisting the already-recorded user message a second time.
                apply_requested_open_qa_document(user, conversation_id, content, response)
            # Archive signed vendor snapshot URLs before message persistence.  The
            # browser receives only tenant-scoped local proxy URLs, while the raw
            # vendor URL remains in memory just long enough to fetch the image.
            archive_online_response_snapshots(conn, user["tenant_id"], response)
            restore_archived_context_public_urls(conn, user["tenant_id"], response)
            visual_context = response.pop("_visual_context", None)
            if not isinstance(response.get("_conversation_context"), dict) and isinstance(visual_context, dict):
                context_images = [item for item in visual_context.get("images") or [] if isinstance(item, dict)]
                resolved_orgs = {}
                for item in context_images:
                    org_id = str(item.get("org_id") or "")
                    if org_id:
                        resolved_orgs[org_id] = str(item.get("org_name") or org_id)
                if resolved_orgs:
                    response["_conversation_context"] = {
                        "domain": "VISUAL_INSPECTION",
                        "task_kind": str(response.get("intent") or "CAPTURE_SNAPSHOT"),
                        "effective_query": str(continuation_decision.get("effective_query") or content)[:1800],
                        "task_scope": {
                            "type": "MULTI_STORE" if len(resolved_orgs) > 1 else "SINGLE_STORE",
                            "source": "INHERITED_TASK" if continuation_is_visual else "EXPLICIT_QUERY",
                            "org_ids": list(resolved_orgs),
                            "org_names": list(resolved_orgs.values()),
                        },
                        "predicate": {"strategy": "MEDIA_CONTEXT", "turn_query": content[:600]},
                        "temporal": {"mode": "CURRENT"},
                        "decision": {
                            **continuation_decision,
                            "evidence_mode": str(continuation_decision.get("evidence_mode") or "RECAPTURE_RESOLVED_SCOPE"),
                            "resolved_org_ids": list(resolved_orgs),
                        },
                        "result_refs": [],
                    }
            persisted_context = persist_online_conversation_context(
                conn,
                user,
                conversation_id,
                context,
                response,
                visual_context,
            )
            if isinstance(persisted_context, dict):
                response["conversation_context"] = persisted_context
            source = str(response.get("source") or "deepvision_online")
            linked_object = {"agent": response.get("agent"), "plan": response.get("plan"), "source": source}
            if isinstance(visual_context, dict):
                linked_object["visual_context"] = visual_context
            if isinstance(persisted_context, dict):
                linked_object["conversation_context"] = persisted_context
            artifact = conversation_artifact(response)
            if artifact:
                linked_object["artifact"] = artifact
            attach_agent_trace(linked_object, content)
            assistant = add_message(
                conn,
                conversation_id,
                "assistant",
                response.pop("assistant_content"),
                None,
                linked_object,
            )
            log_audit(
                conn,
                user["user_id"],
                user["tenant_id"],
                "agent.online.query",
                "message",
                user_message["message_id"],
                None,
                {
                    "intent": response.get("intent"),
                    "engine": (response.get("agent") or {}).get("engine"),
                    "tools": (response.get("agent") or {}).get("tool_calls", []),
                    "source": source,
                    "conversation_context": persisted_context,
                },
                "agent",
                None,
            )
            response["messages"] = [serialize_message(assistant)]
            response["agent"] = linked_object.get("agent")
            return response
        # The local deterministic classifier treats phrases such as "怎么样" as
        # HELP. Give only time-sensitive public questions one early pass through
        # the isolated open-QA planner, otherwise they would miss web.search in
        # AUTO mode. Business and inspection requests still return None here.
        if mode_override != "INSPECTION":
            history = open_qa_history(conn, conversation_id)
            realtime_open_response = open_question_responder_for_request(conn, user["tenant_id"]).agent_response(
                content,
                mode_selection="AUTO",
                history=history,
            )
            if (
                realtime_open_response
                and (realtime_open_response.get("agent") or {}).get("decision", {}).get("response_strategy") == "SEARCH_AND_CITE"
            ):
                return complete_open_qa_message(conn, user, conversation_id, content, realtime_open_response)
        effective_content = content
        intent, confidence = classify_intent(effective_content)
        if confidence < 0.60:
            pending_row = one(
                conn,
                "SELECT * FROM plans WHERE conversation_id=? AND status='NEED_CLARIFICATION' ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            )
            if pending_row:
                pending_plan = serialize_plan(pending_row)
                missing_slots = pending_plan.get("slots", {}).get("missing_slots", [])
                supplements_pending = (
                    ("schedule" in missing_slots and parse_schedule(content) is not None)
                    or ("time_range" in missing_slots and parse_time_range(content) is not None)
                    or ("capability" in missing_slots and find_capability(conn, content) is not None)
                    or ("org_scope" in missing_slots and bool(find_org_candidates(conn, content, None, user)[0]))
                    or ("event_id" in missing_slots and re.search(r"EV[-A-Z0-9]+", content) is not None)
                )
                if supplements_pending:
                    previous_message = one(
                        conn,
                        "SELECT content FROM messages WHERE conversation_id=? AND sender='user' ORDER BY created_at DESC LIMIT 1",
                        (conversation_id,),
                    )
                    if previous_message:
                        effective_content = f"{previous_message['content']}，补充信息：{content}"
                        intent, confidence = classify_intent(effective_content)
        if confidence < 0.60 and mode_override != "INSPECTION":
            history = open_qa_history(conn, conversation_id)
            open_response = open_question_responder_for_request(conn, user["tenant_id"]).agent_response(
                content,
                mode_selection="AUTO",
                history=history,
            )
            if open_response:
                return complete_open_qa_message(conn, user, conversation_id, content, open_response)
        add_message(conn, conversation_id, "user", content)
        if confidence < 0.60:
            assistant = add_message(conn, conversation_id, "assistant", "我已尝试自动分析这句话，但还缺少可执行的业务对象或时间范围。请直接补充你要处理的门店/区域、能力、时间或事件编号，我会继续解析，不需要选择意图。")
            return {"intent": intent, "confidence": confidence, "messages": [serialize_message(assistant)]}
        linked = {}
        if intent == "SUBSCRIPTION_CREATE":
            plan, assistant_content, linked = build_subscription_plan(conn, user, conversation_id, effective_content, context)
            assistant = add_message(conn, conversation_id, "assistant", assistant_content, plan["plan_id"] if plan else None, linked)
            return {"intent": intent, "confidence": confidence, "plan": plan, "messages": [serialize_message(assistant)], **linked}
        if intent == "RESULT_QUERY":
            result = query_events(conn, user, effective_content, context)
            assistant = add_message(conn, conversation_id, "assistant", f"已按权限范围查询到 {result['summary']['total']} 条事件，所有结论都绑定了证据入口。", None, result)
            return {"intent": intent, "confidence": confidence, "result": result, "messages": [serialize_message(assistant)]}
        if intent == "DATA_STATS":
            result = analytics_query(conn, user, effective_content, context)
            assistant = add_message(conn, conversation_id, "assistant", f"已生成统计查询 {result['query_id']}。口径：{result['scope']['caliber']}", None, {"analytics": result})
            return {"intent": intent, "confidence": confidence, "analytics": result, "messages": [serialize_message(assistant)]}
        if intent == "CAMERA_SEARCH":
            result = camera_search(conn, user, effective_content, context)
            assistant = add_message(conn, conversation_id, "assistant", f"查询到 {len(result['cameras'])} 路摄像头。{result['redaction']}", None, result)
            return {"intent": intent, "confidence": confidence, "cameras": result["cameras"], "messages": [serialize_message(assistant)]}
        if intent == "FEEDBACK_CREATE":
            plan, assistant_content, linked = build_feedback_plan(conn, user, conversation_id, effective_content, context)
            assistant = add_message(conn, conversation_id, "assistant", assistant_content, plan["plan_id"] if plan else None, linked)
            return {"intent": intent, "confidence": confidence, "plan": plan, "messages": [serialize_message(assistant)]}
        assistant = add_message(conn, conversation_id, "assistant", "我可以帮你创建巡检订阅、查询告警和证据、做统计问数、查看摄像头状态、反馈误报。写操作都会先生成计划卡并等待确认。")
        return {"intent": intent, "confidence": confidence, "messages": [serialize_message(assistant)]}

    def api_subscriptions(self, conn, user, org_id: str | None = None):
        repair_visible_batch_schedules(conn, user)
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if online:
            warning = None
            try:
                subscriptions = online.subscriptions(org_id)
            except OnlineAgentError as exc:
                subscriptions = []
                warning = {
                    **public_online_delivery_failure(exc),
                    "scope_org_id": org_id,
                    "recoverable": True,
                }
            scheduled = visible_scheduled_inspection_tasks(conn, user)
            subscriptions.extend(self.scheduled_task_subscription(conn, task) for task in scheduled)
            data = {"subscriptions": subscriptions}
            if warning:
                data["degraded"] = True
                data["warning"] = warning
            return self.send_json({"ok": True, "data": data})
        allowed = sorted(allowed_org_ids(conn, user))
        subscriptions = rows(conn, f"SELECT * FROM subscriptions WHERE tenant_id=? AND org_id IN ({','.join('?' for _ in allowed)}) ORDER BY created_at DESC", [user["tenant_id"], *allowed])
        items = []
        for sub in subscriptions:
            item = dict(sub)
            item["org_name"] = org_label(conn, sub["org_id"])
            item["camera_ids"] = json_loads(item["camera_ids"], [])
            item["schedule"] = json_loads(item["schedule"], {})
            item["thresholds"] = json_loads(item["thresholds"], {})
            item["dedupe_policy"] = json_loads(item["dedupe_policy"], {})
            items.append(item)
        scheduled = visible_scheduled_inspection_tasks(conn, user)
        items.extend(self.scheduled_task_subscription(conn, task) for task in scheduled)
        return self.send_json({"ok": True, "data": {"subscriptions": items}})

    def scheduled_task_subscription(self, conn, task: dict) -> dict:
        item = serialize_scheduled_task(conn, task)
        last_run = one(conn, "SELECT * FROM inspection_runs WHERE task_id=? ORDER BY scheduled_at DESC LIMIT 1", (task["task_id"],))
        return {
            "subscription_id": task["task_id"],
            "task_id": task["task_id"],
            "kind": "SCHEDULED_VISUAL",
            "name": task["name"],
            "org_id": task["org_id"],
            "org_name": task["org_name"],
            "camera_ids": item["camera_ids"],
            "camera_names": item["camera_names"],
            "schedule": item["schedule"],
            "status": task["status"],
            "next_run_at": task["next_run_at"],
            "last_run_at": task["last_run_at"],
            "run_count": task["run_count"],
            "anomaly_count": task["anomaly_count"],
            "uncertain_count": task["uncertain_count"],
            "inspection_goal": task["inspection_goal"],
            "last_run": serialize_inspection_run(conn, last_run) if last_run else None,
        }

    def api_inspection_batches(self, conn, user, query):
        batches = rows(
            conn,
            "SELECT * FROM inspection_batches WHERE tenant_id=? ORDER BY created_at DESC LIMIT 100",
            (user["tenant_id"],),
        )
        visible = [serialize_inspection_batch(conn, batch, include_items=False) for batch in batches if user_can_view_batch(conn, user, batch)]
        return self.send_json({"ok": True, "data": {"inspection_batches": visible}})

    def api_inspection_batch_detail(self, conn, user, batch_id: str):
        batch = one(conn, "SELECT * FROM inspection_batches WHERE batch_id=? AND tenant_id=?", (batch_id, user["tenant_id"]))
        if not batch or not user_can_view_batch(conn, user, batch):
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"batch_id": batch_id})
        return self.send_json({"ok": True, "data": {"inspection_batch": serialize_inspection_batch(conn, batch, include_items=True)}})

    def api_cancel_inspection_batch(self, conn, user, batch_id: str) -> dict:
        batch = one(conn, "SELECT * FROM inspection_batches WHERE batch_id=? AND tenant_id=?", (batch_id, user["tenant_id"]))
        if not batch or not user_can_view_batch(conn, user, batch):
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"batch_id": batch_id})
        if not role_can_create_batch_inspection(user["role"]) and batch["created_by"] != user["user_id"]:
            raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
        before = {"status": batch["status"]}
        ts = now_iso()
        task_ids = [
            item["scheduled_task_id"]
            for item in rows(conn, "SELECT scheduled_task_id FROM inspection_batch_items WHERE batch_id=? AND scheduled_task_id IS NOT NULL", (batch_id,))
        ]
        if task_ids:
            conn.execute(
                f"UPDATE scheduled_inspections SET status='CANCELLED', next_run_at=NULL, updated_at=? WHERE task_id IN ({','.join('?' for _ in task_ids)})",
                (ts, *task_ids),
            )
        conn.execute(
            """
            UPDATE inspection_batch_items
            SET status='SKIPPED',
                failure_code=COALESCE(failure_code, 'USER_CANCELLED'),
                updated_at=?
            WHERE batch_id=?
              AND (
                status IN ('RUNNING','PENDING')
                OR (status='SUCCEEDED' AND run_ids='[]')
              )
            """,
            (ts, batch_id),
        )
        conn.execute("UPDATE inspection_batches SET status='CANCELLED', updated_at=? WHERE batch_id=?", (ts, batch_id))
        log_audit(
            conn,
            user["user_id"],
            user["tenant_id"],
            "inspection_batch.cancel",
            "inspection_batch",
            batch_id,
            before,
            {"status": "CANCELLED", "cancelled_task_count": len(task_ids)},
            "page",
            batch.get("plan_id"),
        )
        updated = one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch_id,))
        return {"inspection_batch": serialize_inspection_batch(conn, updated), "message": "批量巡检已取消，关联子任务已停止。"}

    def api_retry_inspection_batch(self, conn, user, batch_id: str) -> dict:
        batch = one(conn, "SELECT * FROM inspection_batches WHERE batch_id=? AND tenant_id=?", (batch_id, user["tenant_id"]))
        if not batch or not user_can_view_batch(conn, user, batch):
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"batch_id": batch_id})
        if not role_can_create_batch_inspection(user["role"]) and batch["created_by"] != user["user_id"]:
            raise ApiError("PERMISSION_DENIED", HTTPStatus.FORBIDDEN)
        plan_row = one(conn, "SELECT * FROM plans WHERE plan_id=? AND tenant_id=?", (batch["plan_id"], user["tenant_id"]))
        if not plan_row:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND, {"plan_id": batch["plan_id"]})
        plan = serialize_plan(plan_row)
        params = (plan.get("actions") or [{}])[0].get("params") or {}
        retry_items = rows(
            conn,
            """
            SELECT * FROM inspection_batch_items
            WHERE batch_id=?
              AND (
                status IN ('FAILED','SKIPPED','CANCELLED')
                OR (status='SUCCEEDED' AND run_ids='[]')
              )
            ORDER BY store_name ASC
            """,
            (batch_id,),
        )
        if not retry_items:
            return {"inspection_batch": serialize_inspection_batch(conn, batch), "message": "当前批次没有需要重试的门店。"}

        retried = 0
        still_failed = 0
        ts = now_iso()
        for item in retry_items:
            camera_ids = json_loads(item.get("camera_ids"), [])
            camera_names = json_loads(item.get("camera_names"), [])
            if not camera_ids:
                conn.execute(
                    "UPDATE inspection_batch_items SET retry_count=retry_count+1, failure_code='NO_ONLINE_CAMERA', updated_at=? WHERE item_id=?",
                    (ts, item["item_id"]),
                )
                still_failed += 1
                continue
            child_params = {
                "org_id": item["store_id"],
                "org_name": item["store_name"],
                "camera_ids": camera_ids,
                "camera_names": camera_names,
                "inspection_goal": params.get("inspection_goal"),
                "schedule": params.get("schedule") or {},
                "start_at": params.get("start_at"),
                "end_at": params.get("end_at"),
                "thresholds": params.get("thresholds") or {"confidence": 0.8},
                "force_first_run": bool(params.get("force_first_run")),
            }
            try:
                task = reactivate_scheduled_inspection_task(
                    conn,
                    user,
                    plan,
                    child_params,
                    item.get("scheduled_task_id"),
                    batch_id,
                )
                if not task:
                    task = create_scheduled_inspection_task(
                        conn,
                        user,
                        plan,
                        child_params,
                        batch_id=batch_id,
                        force_first_run=bool(params.get("force_first_run")),
                    )
                conn.execute(
                    """
                    UPDATE inspection_batch_items
                    SET status='RUNNING', failure_code=NULL, retry_count=retry_count+1,
                        scheduled_task_id=?, run_ids='[]', updated_at=?
                    WHERE item_id=?
                    """,
                    (task["task_id"], now_iso(), item["item_id"]),
                )
                retried += 1
            except Exception as exc:
                conn.execute(
                    """
                    UPDATE inspection_batch_items
                    SET status='FAILED', failure_code=?, retry_count=retry_count+1, updated_at=?
                    WHERE item_id=?
                    """,
                    (getattr(exc, "code", None) or exc.__class__.__name__, now_iso(), item["item_id"]),
                )
                still_failed += 1
        counts = update_inspection_batch_counts(conn, batch_id, now_iso())
        log_audit(
            conn,
            user["user_id"],
            user["tenant_id"],
            "inspection_batch.retry",
            "inspection_batch",
            batch_id,
            {"status": batch["status"]},
            {"retried_store_count": retried, "still_failed_store_count": still_failed, "status": counts["status"]},
            "page",
            batch.get("plan_id"),
        )
        updated = one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch_id,))
        return {
            "inspection_batch": serialize_inspection_batch(conn, updated),
            "message": f"批量巡检已重试并排队首轮执行：恢复 {retried} 家，仍需处理 {still_failed} 家。首轮快照会由调度器立即拉取并回写分析结果。",
        }

    def api_scheduled_inspections(self, conn, user):
        repair_visible_batch_schedules(conn, user)
        tasks = visible_scheduled_inspection_tasks(conn, user)
        return self.send_json(
            {
                "ok": True,
                "data": {"scheduled_inspections": [serialize_scheduled_task(conn, task, include_runs=True) for task in tasks]},
            }
        )

    def api_inspection_runs(self, conn, user, query):
        try:
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["50"])[0])
        except (TypeError, ValueError) as exc:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "page 和 page_size 必须为整数"}) from exc
        if page < 1 or page > 10000:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "page 必须在 1 到 10000 之间"})
        if page_size not in {10, 20, 50, 100}:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "page_size 仅支持 10、20、50、100"})

        filters = ["s.tenant_id=?", "s.user_id=?"]
        args = [user["tenant_id"], user["user_id"]]
        org_id = query.get("org_id", [None])[0]
        if org_id:
            filters.append("s.org_id=?")
            args.append(org_id)
        where_sql = " AND ".join(filters)
        total = one(
            conn,
            f"""SELECT COUNT(*) AS total
                FROM inspection_runs r
                JOIN scheduled_inspections s ON s.task_id=r.task_id
                WHERE {where_sql}""",
            args,
        )["total"]
        total_pages = (total + page_size - 1) // page_size if total else 0
        offset = (page - 1) * page_size
        run_rows = rows(
            conn,
            f"""SELECT r.*, s.name AS task_name, s.status AS task_status, s.org_id, s.org_name,
                       s.inspection_goal, s.camera_names, s.thresholds AS task_thresholds
                FROM inspection_runs r
                JOIN scheduled_inspections s ON s.task_id=r.task_id
                WHERE {where_sql}
                ORDER BY COALESCE(r.completed_at, r.started_at) DESC, r.scheduled_at DESC
                LIMIT ? OFFSET ?""",
            [*args, page_size, offset],
        )
        records = [serialize_inspection_history_record(conn, run) for run in run_rows]
        range_start = offset + 1 if records else 0
        pagination = {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": page < total_pages,
            "range_start": range_start,
            "range_end": offset + len(records) if records else 0,
            "page_size_options": [10, 20, 50, 100],
        }
        return self.send_json({"ok": True, "data": {"inspection_runs": records, "pagination": pagination}})

    def api_inspection_run_detail(self, conn, user, run_id: str):
        run = one(
            conn,
            """SELECT r.*, s.name AS task_name, s.status AS task_status, s.org_id, s.org_name,
                      s.inspection_goal, s.camera_names, s.thresholds AS task_thresholds
               FROM inspection_runs r
               JOIN scheduled_inspections s ON s.task_id=r.task_id
               WHERE r.run_id=? AND s.tenant_id=? AND s.user_id=?""",
            (run_id, user["tenant_id"], user["user_id"]),
        )
        if not run:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        record = serialize_inspection_history_record(conn, run)
        log_audit(
            conn,
            user["user_id"],
            user["tenant_id"],
            "inspection_run.view",
            "inspection_run",
            run_id,
            None,
            {"task_id": run["task_id"], "evidence_count": record["evidence_count"]},
            "page",
            None,
        )
        conn.commit()
        return self.send_json({"ok": True, "data": {"inspection_run": record}})

    def api_events(self, conn, user, query):
        try:
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["50"])[0])
        except (TypeError, ValueError) as exc:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "page 和 page_size 必须为整数"}) from exc
        if page < 1 or page > 10000:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "page 必须在 1 到 10000 之间"})
        if page_size not in {10, 20, 50, 100}:
            raise ApiError("BAD_REQUEST", HTTPStatus.BAD_REQUEST, {"message": "page_size 仅支持 10、20、50、100"})

        org_id = query.get("org_id", [None])[0]
        context = {"org_id": org_id}
        words = " ".join(query.get("q", [])) or "近7天"
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if online:
            org_ids = []
            for value in query.get("org_ids", []):
                org_ids.extend(item for item in value.split(",") if item)
            result = online.paginated_events(
                org_id=org_id,
                org_ids=org_ids or None,
                query_text=words,
                page=page,
                page_size=page_size,
                begin_time=query.get("begin_time", [None])[0],
                end_time=query.get("end_time", [None])[0],
                alarm_type=query.get("alarm_type", [None])[0],
            )
            return self.send_json({"ok": True, "data": result})
        result = query_events(conn, user, words, context, page=page, page_size=page_size)
        return self.send_json({"ok": True, "data": result})

    def api_event_detail(self, conn, user, event_id):
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if online:
            event = online.event_detail(event_id)
            log_audit(conn, user["user_id"], user["tenant_id"], "evidence.view", "event", event_id, None, {"source": "deepvision_online"}, "page", None)
            conn.commit()
            return self.send_json({"ok": True, "data": {"event": event}})
        event = one(conn, "SELECT * FROM events WHERE event_id=?", (event_id,))
        if not event:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        assert_org_access(conn, user, [event["org_id"]])
        log_audit(conn, user["user_id"], user["tenant_id"], "evidence.view", "event", event_id, None, {"event_id": event_id}, "page", None)
        conn.commit()
        return self.send_json({"ok": True, "data": {"event": serialize_event(conn, event)}})

    def api_event_evidence(self, conn, user, event_id):
        online = online_agent_for_tenant(conn, user["tenant_id"])
        if online:
            event = online.event_detail(event_id)
            log_audit(conn, user["user_id"], user["tenant_id"], "evidence.view", "evidence", event_id, None, {"source": "deepvision_online", "evidence_count": event["evidence_count"]}, "page", None)
            conn.commit()
            return self.send_json({"ok": True, "data": {"evidence": event["evidence"]}})
        event = one(conn, "SELECT * FROM events WHERE event_id=?", (event_id,))
        if not event:
            raise ApiError("RESOURCE_NOT_FOUND", HTTPStatus.NOT_FOUND)
        assert_org_access(conn, user, [event["org_id"]])
        evidence_ids = json_loads(event["evidence_ids"], [])
        evidences = rows(conn, f"SELECT * FROM evidence WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)})", evidence_ids) if evidence_ids else []
        log_audit(conn, user["user_id"], user["tenant_id"], "evidence.view", "evidence", event_id, None, {"evidence_count": len(evidences)}, "page", None)
        conn.commit()
        return self.send_json({"ok": True, "data": {"evidence": [serialize_evidence(e) for e in evidences]}})


def run_server(port: int):
    init_db()
    readiness_errors = production_readiness_errors()
    if readiness_errors:
        raise RuntimeError("Office F0 readiness failed: " + ", ".join(readiness_errors))
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    worker = ScheduledInspectionWorker(int(os.environ.get("AGI_SCHEDULER_POLL_SECONDS", "5")))
    worker.start()
    office_worker = None
    # The in-process adapter exists only for local development / deterministic
    # tests.  Production launches ``office_worker.py`` in a resource-limited
    # worker deployment; the Web process must never host Office rendering.
    if (
        str(os.environ.get("AGI_OFFICE_PRODUCTION") or "").lower() not in {"1", "true", "yes"}
        and str(os.environ.get("AGI_OFFICE_WORKER_ENABLED") or "").lower() in {"1", "true", "yes"}
    ):
        office_worker = OfficeJobWorker(connect, office_job_service_for_worker, poll_seconds=float(os.environ.get("AGI_OFFICE_WORKER_POLL_SECONDS", "1")))
        office_worker.start()
    print(f"AGI Inspection MVP running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    finally:
        worker.stop()
        if office_worker:
            office_worker.stop()
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=None)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)
    init_db(reset=args.reset)
    run_server(args.port)


if __name__ == "__main__":
    main()
