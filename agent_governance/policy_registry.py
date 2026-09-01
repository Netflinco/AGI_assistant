"""Feature switches and P0 policy constants.

The database lookup is intentionally tiny so the same policy can be enforced
at the HTTP edge and again by a worker.  A missing flag is fail-closed.
"""

from __future__ import annotations

from typing import Any


POLICY_VERSION = "p0-2026-08-18"

DEFAULT_FEATURE_FLAGS = {
    "open_research_enabled": False,
    "office_enabled": False,
    "research_to_office_enabled": False,
    "office_to_research_egress_enabled": False,
    "office_external_share_enabled": False,
    "office_model_processing_enabled": False,
}

# This registry is the server-side source of truth for the tenant feature
# centre.  The browser only renders these definitions; it never decides which
# capability can be enabled or which prerequisite may be bypassed.
FEATURE_FLAG_DEFINITIONS = (
    {
        "flag": "open_research_enabled",
        "label": "开放信息检索",
        "description": "允许当前租户对公开问题发起 Tavily 证据检索。仅最小化的公开 Query 可出站，内部数据、附件与个人敏感信息仍会被门禁拦截。",
        "risk": "PUBLIC_EGRESS",
        "confirmation": "开启后，符合公开数据边界的问题可发送至 Tavily 进行联网检索。",
        "dependencies": (),
        "locked": False,
    },
    {
        "flag": "office_enabled",
        "label": "Office 文档处理",
        "description": "允许当前租户上传受支持的 Excel、Word 文件并生成私有 PPT。文件安全检查、大小限制、用户级访问控制仍持续生效。",
        "risk": "PRIVATE_PROCESSING",
        "confirmation": "开启后，当前租户用户可以发起受控的 Office 文件处理任务。",
        "dependencies": (),
        "locked": False,
    },
    {
        "flag": "research_to_office_enabled",
        "label": "检索结果生成 PPT",
        "description": "允许把已完成且带引用的公开检索摘要传递给 Office 生成流程；不会传递网页全文，也不会把 Office 内容发往公网。",
        "risk": "CROSS_DOMAIN_READ_ONLY",
        "confirmation": "开启后，可基于已核验的公开检索摘要生成私有 PPT。",
        "dependencies": ("open_research_enabled", "office_enabled"),
        "locked": False,
    },
    {
        "flag": "office_model_processing_enabled",
        "label": "已批准模型网关处理",
        "description": "允许通过文件安全与敏感信息检查后的最小文档片段进入已批准模型网关，用于 Office 内容编排；不发送网页全文或任何密钥。",
        "risk": "APPROVED_MODEL_EGRESS",
        "confirmation": "开启后，符合策略的最小 Office 文档片段可发送至已批准模型网关。",
        "dependencies": ("office_enabled",),
        "locked": False,
    },
    {
        "flag": "office_to_research_egress_enabled",
        "label": "基于 Office 内容联网检索",
        "description": "P0 阶段固定关闭。Office 文档、表格和附件内容不得自动发送至公开搜索服务。",
        "risk": "BLOCKED_P0",
        "confirmation": "",
        "dependencies": ("office_enabled", "open_research_enabled"),
        "locked": True,
    },
    {
        "flag": "office_external_share_enabled",
        "label": "Office 产物外部共享",
        "description": "P0 阶段固定关闭。邮件、IM、外部网盘和第三方 Office 写入均不在本期开放范围。",
        "risk": "BLOCKED_P0",
        "confirmation": "",
        "dependencies": ("office_enabled",),
        "locked": True,
    },
)

FEATURE_FLAG_BY_NAME = {item["flag"]: item for item in FEATURE_FLAG_DEFINITIONS}


class FeatureFlagPolicyError(ValueError):
    """A stable policy error returned by tenant feature-switch updates."""

    def __init__(self, code: str, flag: str, dependencies: tuple[str, ...] = ()):
        self.code = code
        self.flag = flag
        self.dependencies = dependencies
        super().__init__(code)

P0_BLOCKED_OFFICE_ACTIONS = {
    "OVERWRITE",
    "EXTERNAL_SHARE",
    "EMAIL_SEND",
    "M365_WRITE",
    "WPS_WRITE",
    "DATABASE_WRITE",
}


def feature_enabled(conn: Any, tenant_id: str, flag: str) -> bool:
    default = bool(DEFAULT_FEATURE_FLAGS.get(flag, False))
    row = conn.execute(
        "SELECT enabled FROM agent_feature_flags WHERE tenant_id=? AND flag=?",
        (tenant_id, flag),
    ).fetchone()
    if not row:
        return default
    return bool(row["enabled"] if hasattr(row, "keys") else row[0])


def feature_snapshot(conn: Any, tenant_id: str) -> dict[str, bool]:
    return {flag: feature_enabled(conn, tenant_id, flag) for flag in DEFAULT_FEATURE_FLAGS}


def feature_flag_definitions() -> list[dict[str, Any]]:
    """Return public, non-secret metadata for the tenant feature centre."""
    return [
        {
            "flag": item["flag"],
            "label": item["label"],
            "description": item["description"],
            "risk": item["risk"],
            "confirmation": item["confirmation"],
            "dependencies": list(item["dependencies"]),
            "locked": bool(item["locked"]),
        }
        for item in FEATURE_FLAG_DEFINITIONS
    ]


def set_feature(conn: Any, tenant_id: str, flag: str, enabled: bool, now: str) -> None:
    if flag not in DEFAULT_FEATURE_FLAGS:
        raise ValueError("UNKNOWN_FEATURE_FLAG")
    conn.execute(
        """INSERT INTO agent_feature_flags(tenant_id, flag, enabled, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(tenant_id, flag) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at""",
        (tenant_id, flag, 1 if enabled else 0, now),
    )


def apply_feature_updates(conn: Any, tenant_id: str, updates: dict[str, Any], now: str) -> dict[str, Any]:
    """Apply a coherent, fail-closed tenant feature change atomically.

    A child capability cannot be enabled until every prerequisite is enabled.
    When a parent is disabled, active children are forced off in the same
    transaction so a later re-enable never revives an unintended data flow.
    P0's Office-to-web egress and external-share paths are immutable here.
    """
    if not isinstance(updates, dict) or not updates:
        raise FeatureFlagPolicyError("FEATURE_FLAG_UPDATE_EMPTY", "")
    normalized: dict[str, bool] = {}
    for raw_flag, raw_enabled in updates.items():
        flag = str(raw_flag or "")
        if flag not in FEATURE_FLAG_BY_NAME:
            raise FeatureFlagPolicyError("UNKNOWN_FEATURE_FLAG", flag)
        definition = FEATURE_FLAG_BY_NAME[flag]
        enabled = bool(raw_enabled)
        if definition["locked"] and enabled:
            raise FeatureFlagPolicyError("FEATURE_FLAG_LOCKED_P0", flag)
        normalized[flag] = enabled

    before = feature_snapshot(conn, tenant_id)
    after = dict(before)
    after.update(normalized)
    for flag, enabled in normalized.items():
        if not enabled:
            continue
        dependencies = tuple(FEATURE_FLAG_BY_NAME[flag]["dependencies"])
        if any(not after.get(dependency, False) for dependency in dependencies):
            raise FeatureFlagPolicyError("FEATURE_FLAG_DEPENDENCY_REQUIRED", flag, dependencies)

    forced_disabled: list[str] = []
    changed = True
    while changed:
        changed = False
        for definition in FEATURE_FLAG_DEFINITIONS:
            flag = definition["flag"]
            if not after.get(flag, False):
                continue
            if any(not after.get(dependency, False) for dependency in definition["dependencies"]):
                after[flag] = False
                forced_disabled.append(flag)
                changed = True

    changed_flags = [flag for flag in DEFAULT_FEATURE_FLAGS if before.get(flag) != after.get(flag)]
    for flag in changed_flags:
        set_feature(conn, tenant_id, flag, after[flag], now)
    return {
        "before": before,
        "after": after,
        "changed_flags": changed_flags,
        "forced_disabled": forced_disabled,
    }
