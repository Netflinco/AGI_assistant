#!/usr/bin/env python3
"""Standard Agent core contracts for intent, skill, tool, and execution routing.

This module is intentionally business-agnostic. Existing product behavior keeps
living in online_agent.py and agent_skills.py; the core provides a stable
registry/routing layer that those modules can adopt gradually.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


CN_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _dedupe(values: Iterable[str | None]) -> tuple[str, ...]:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            items.append(item)
    return tuple(items)


MANIFEST_RISK_LEVELS = {"READ_ONLY", "TRANSIENT_SESSION", "HIGH_WRITE", "DESIGN_ONLY"}
MANIFEST_KINDS = {"skill": "skill.v1", "tool": "tool.v1"}
SENSITIVE_MANIFEST_KEYS = {
    "api_key",
    "apikey",
    "app_key",
    "appkey",
    "app_secret",
    "appsecret",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "private_key",
    "authorization",
}


def _manifest_string(value: Any) -> str:
    return str(value or "").strip()


def _manifest_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = manifest.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _manifest_key_name(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_")


def _find_manifest_secret_fields(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = _manifest_key_name(raw_key)
            child_path = f"{path}.{raw_key}"
            if key in SENSITIVE_MANIFEST_KEYS and key != "credential_ref" and str(child or "").strip():
                findings.append(child_path)
            findings.extend(_find_manifest_secret_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_find_manifest_secret_fields(child, f"{path}[{index}]"))
    return findings


def validate_agent_manifest(
    manifest: Any,
    *,
    known_tools: Iterable[str] | None = None,
    builtin_intents: Iterable[str] | None = None,
    tenant_intents: dict[str, str] | None = None,
    current_manifest_name: str | None = None,
) -> dict[str, Any]:
    """Validate a lightweight skill/tool manifest before it enters the catalog.

    The first implementation registers imported capabilities as catalog metadata
    only. Runtime execution must be bound explicitly by a later executor adapter.
    """

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        return {
            "ok": False,
            "errors": ["manifest must be a JSON object"],
            "warnings": [],
            "normalized": {},
        }

    kind = _manifest_string(manifest.get("kind")).lower()
    schema_version = _manifest_string(manifest.get("schema_version"))
    metadata = _manifest_metadata(manifest)
    name = _manifest_string(metadata.get("name"))
    label = _manifest_string(metadata.get("label") or name)
    version = _manifest_string(metadata.get("version") or "0.0.1")
    risk = manifest.get("risk") if isinstance(manifest.get("risk"), dict) else {}
    risk_level = _manifest_string(risk.get("level") or manifest.get("risk_level") or "READ_ONLY").upper()
    confirm_value = risk.get("confirm_required")
    confirm_required = bool(confirm_value)

    for secret_path in _find_manifest_secret_fields(manifest):
        if not secret_path.endswith(".credential_ref"):
            errors.append(f"{secret_path} must use credential_ref; raw secret values are not allowed in Manifest")

    if kind not in MANIFEST_KINDS:
        errors.append("kind must be skill or tool")
    elif schema_version != MANIFEST_KINDS[kind]:
        errors.append(f"schema_version must be {MANIFEST_KINDS[kind]} for {kind}")
    if not name:
        errors.append("metadata.name is required")
    elif not all(ch.isalnum() or ch in "._-" for ch in name):
        errors.append("metadata.name only supports letters, numbers, dot, underscore, and hyphen")
    if not label:
        errors.append("metadata.label is required")
    if risk_level not in MANIFEST_RISK_LEVELS:
        errors.append(f"risk.level must be one of {', '.join(sorted(MANIFEST_RISK_LEVELS))}")
    if risk_level == "HIGH_WRITE" and confirm_value is not True:
        errors.append("HIGH_WRITE manifests must explicitly set risk.confirm_required=true")

    normalized: dict[str, Any] = {
        "kind": kind,
        "schema_version": schema_version,
        "name": name,
        "label": label,
        "version": version,
        "risk_level": risk_level,
        "confirm_required": confirm_required,
        "runtime_status": "pending_validation",
    }

    if kind == "skill":
        known_tool_set = {str(item).strip() for item in (known_tools or []) if str(item).strip()}
        builtin_intent_set = {str(item).strip() for item in (builtin_intents or []) if str(item).strip()}
        tenant_intent_map = {
            str(key).strip(): str(value).strip()
            for key, value in (tenant_intents or {}).items()
            if str(key).strip() and str(value).strip()
        }
        current_name = _manifest_string(current_manifest_name or name)
        intent = manifest.get("intent") if isinstance(manifest.get("intent"), dict) else {}
        intent_name = _manifest_string(intent.get("name"))
        aliases = intent.get("aliases") if isinstance(intent.get("aliases"), list) else []
        slots = manifest.get("slots") if isinstance(manifest.get("slots"), dict) else {}
        required_slots = slots.get("required") if isinstance(slots.get("required"), list) else []
        execution = manifest.get("execution") if isinstance(manifest.get("execution"), dict) else {}
        steps = execution.get("steps") if isinstance(execution.get("steps"), list) else []
        if not intent_name:
            errors.append("skill.intent.name is required")
        elif intent_name in builtin_intent_set:
            errors.append(f"skill.intent.name conflicts with builtin intent {intent_name}; use similar_intents to associate instead")
        elif tenant_intent_map.get(intent_name) and tenant_intent_map[intent_name] != current_name:
            errors.append(f"skill.intent.name already belongs to imported skill {tenant_intent_map[intent_name]}")
        if not aliases:
            warnings.append("skill.intent.aliases is empty; routing recall may be weak")
        if not steps:
            errors.append("skill.execution.steps must contain at least one step")
        else:
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    errors.append(f"skill.execution.steps[{index}] must be an object")
                    continue
                step_tool = _manifest_string(step.get("tool"))
                step_skill = _manifest_string(step.get("skill"))
                if not (step_tool or step_skill):
                    errors.append(f"skill.execution.steps[{index}] must reference tool or skill")
                if step_tool and known_tool_set and step_tool not in known_tool_set:
                    errors.append(f"skill.execution.steps[{index}].tool references unknown tool {step_tool}")
        if not errors and steps:
            normalized["runtime_status"] = "callable"
        normalized.update(
            {
                "intent": intent_name,
                "aliases": [_manifest_string(item) for item in aliases if _manifest_string(item)],
                "similar_intents": [
                    _manifest_string(item)
                    for item in (intent.get("similar_intents") if isinstance(intent.get("similar_intents"), list) else [])
                    if _manifest_string(item)
                ],
                "required_slots": [_manifest_string(item) for item in required_slots if _manifest_string(item)],
                "step_count": len(steps),
            }
        )
    elif kind == "tool":
        runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
        runtime_type = _manifest_string(runtime.get("type")).lower()
        input_schema = manifest.get("input_schema")
        output_schema = manifest.get("output_schema")
        if runtime_type not in {"http", "local", "mcp", "builtin"}:
            errors.append("tool.runtime.type must be http, local, mcp, or builtin")
        if runtime_type == "http":
            endpoint = _manifest_string(runtime.get("endpoint"))
            if not endpoint:
                errors.append("http tool.runtime.endpoint is required")
            elif "example.com/api/replace-me" in endpoint or "example.invalid" in endpoint:
                errors.append("http tool.runtime.endpoint must be a real callable endpoint, not a placeholder")
            method = _manifest_string(runtime.get("method") or "POST").upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                errors.append("http tool.runtime.method is invalid")
        if runtime_type == "builtin" and not _manifest_string(runtime.get("handler")):
            errors.append("builtin tool.runtime.handler is required")
        if not isinstance(input_schema, dict):
            errors.append("tool.input_schema must be an object")
        if not isinstance(output_schema, dict):
            errors.append("tool.output_schema must be an object")
        auth = runtime.get("auth") if isinstance(runtime.get("auth"), dict) else {}
        if isinstance(manifest.get("auth"), dict):
            errors.append("tool.auth is not supported at top level; put credential_ref under tool.runtime.auth")
        if runtime_type == "http" and auth and auth.get("api_key"):
            errors.append("tool.runtime.auth must use credential_ref; raw api_key is not allowed")
        if risk_level == "HIGH_WRITE" and not confirm_required:
            warnings.append("write-like tools should require confirmation before execution")
        if not errors and runtime_type:
            normalized["runtime_status"] = "callable"
        normalized.update(
            {
                "runtime_type": runtime_type,
                "input_schema": input_schema if isinstance(input_schema, dict) else {},
                "output_schema": output_schema if isinstance(output_schema, dict) else {},
            }
        )

    return {
        "ok": not errors,
        "kind": kind,
        "schema_version": schema_version,
        "name": name,
        "label": label,
        "version": version,
        "risk_level": risk_level,
        "confirm_required": confirm_required,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized,
    }


@dataclass(frozen=True)
class InputEnvelope:
    """Normalized user input accepted by the Agent input layer."""

    text: str
    modality: str = "text"
    context: dict[str, Any] = field(default_factory=dict)
    history: tuple[dict[str, Any], ...] = ()
    attachments: tuple[dict[str, Any], ...] = ()
    received_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_text(
        cls,
        text: str,
        context: dict[str, Any] | None = None,
        history: Iterable[dict[str, Any]] | None = None,
    ) -> "InputEnvelope":
        return cls(
            text=str(text or "").strip(),
            context=dict(context or {}),
            history=tuple(dict(item) for item in (history or [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "modality": self.modality,
            "context": self.context,
            "history_count": len(self.history),
            "attachments": list(self.attachments),
            "received_at": self.received_at,
        }


@dataclass(frozen=True)
class IntentDefinition:
    """Intent metadata used by the standard intent recognition layer."""

    name: str
    label: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    similar_intents: tuple[str, ...] = ()
    default_skill: str | None = None
    default_tool: str | None = None
    risk: str = "READ_ONLY"
    required_slots: tuple[str, ...] = ()
    source: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "aliases": list(self.aliases),
            "similar_intents": list(self.similar_intents),
            "default_skill": self.default_skill,
            "default_tool": self.default_tool,
            "risk": self.risk,
            "required_slots": list(self.required_slots),
            "source": self.source,
        }


@dataclass(frozen=True)
class SkillDefinition:
    """Skill metadata exposed by the Agent skill library."""

    name: str
    label: str
    intent: str
    risk: str = "READ_ONLY"
    default_tool: str | None = None
    required_slots: tuple[str, ...] = ()
    description: str = ""
    source: str = "builtin"
    installed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "intent": self.intent,
            "risk": self.risk,
            "default_tool": self.default_tool,
            "required_slots": list(self.required_slots),
            "description": self.description,
            "source": self.source,
            "installed": self.installed,
        }


@dataclass(frozen=True)
class ToolDefinition:
    """Tool metadata exposed by the Agent toolbox."""

    name: str
    label: str
    risk: str = "READ_ONLY"
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    source: str = "builtin"
    installed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "risk": self.risk,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "source": self.source,
            "installed": self.installed,
        }


@dataclass(frozen=True)
class RouteDecision:
    """Result of routing an intent to a skill and default tool."""

    intent: str
    skill: SkillDefinition | None
    tool: ToolDefinition | None
    risk: str = "READ_ONLY"
    required_slots: tuple[str, ...] = ()
    similar_intents: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "skill": self.skill.to_dict() if self.skill else None,
            "tool": self.tool.to_dict() if self.tool else None,
            "risk": self.risk,
            "required_slots": list(self.required_slots),
            "similar_intents": list(self.similar_intents),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExecutionStep:
    """Standard execution trace node contract for tools and skills."""

    node_id: str
    title: str
    kind: str
    status: str
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    reasoning: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "input": self.input,
            "output": self.output,
            "summary": self.summary,
            "reasoning": self.reasoning,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class IntentRegistry:
    """Registry for builtin and extension intents."""

    def __init__(self, intents: Iterable[IntentDefinition] | None = None):
        self._items: dict[str, IntentDefinition] = {}
        for intent in intents or []:
            self.register(intent)

    def register(self, intent: IntentDefinition) -> IntentDefinition:
        self._items[intent.name] = intent
        return intent

    def get(self, name: str | None) -> IntentDefinition | None:
        if not name:
            return None
        return self._items.get(name)

    def associate(self, name: str, *similar_intents: str) -> IntentDefinition | None:
        current = self.get(name)
        if not current:
            return None
        updated = IntentDefinition(
            **{
                **current.to_dict(),
                "aliases": tuple(current.aliases),
                "similar_intents": _dedupe([*current.similar_intents, *similar_intents]),
                "required_slots": tuple(current.required_slots),
            }
        )
        return self.register(updated)

    def all(self) -> list[IntentDefinition]:
        return [self._items[key] for key in sorted(self._items)]

    def to_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.all()]


class SkillRegistry:
    """Registry for builtin and third-party skills."""

    def __init__(self, skills: Iterable[SkillDefinition] | None = None):
        self._items: dict[str, SkillDefinition] = {}
        for skill in skills or []:
            self.register(skill)

    def register(self, skill: SkillDefinition) -> SkillDefinition:
        self._items[skill.name] = skill
        return skill

    def get(self, name: str | None) -> SkillDefinition | None:
        if not name:
            return None
        return self._items.get(name)

    def find_by_intent(self, intent: str | None) -> SkillDefinition | None:
        if not intent:
            return None
        for skill in self._items.values():
            if skill.intent == intent:
                return skill
        return None

    def all(self) -> list[SkillDefinition]:
        return [self._items[key] for key in sorted(self._items)]

    def to_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.all()]


class ToolRegistry:
    """Registry for builtin and third-party tools."""

    def __init__(self, tools: Iterable[ToolDefinition] | None = None):
        self._items: dict[str, ToolDefinition] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        self._items[tool.name] = tool
        return tool

    def get(self, name: str | None) -> ToolDefinition | None:
        if not name:
            return None
        return self._items.get(name)

    def all(self) -> list[ToolDefinition]:
        return [self._items[key] for key in sorted(self._items)]

    def to_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.all()]


class AgentCatalog:
    """Layered Agent catalog composed of intents, skills, and tools."""

    def __init__(
        self,
        intents: IntentRegistry | None = None,
        skills: SkillRegistry | None = None,
        tools: ToolRegistry | None = None,
    ):
        self.intents = intents or IntentRegistry()
        self.skills = skills or SkillRegistry()
        self.tools = tools or ToolRegistry()

    def register_intent(self, intent: IntentDefinition) -> IntentDefinition:
        return self.intents.register(intent)

    def register_skill(self, skill: SkillDefinition) -> SkillDefinition:
        return self.skills.register(skill)

    def register_tool(self, tool: ToolDefinition) -> ToolDefinition:
        return self.tools.register(tool)

    def route(self, intent_name: str | None) -> RouteDecision:
        intent = self.intents.get(intent_name) or self.intents.get("HELP")
        if not intent:
            return RouteDecision(
                intent=str(intent_name or "UNKNOWN"),
                skill=None,
                tool=None,
                reason="No registered fallback intent is available.",
            )
        skill = self.skills.get(intent.default_skill) or self.skills.find_by_intent(intent.name)
        tool = self.tools.get((skill.default_tool if skill else None) or intent.default_tool)
        return RouteDecision(
            intent=intent.name,
            skill=skill,
            tool=tool,
            risk=(skill.risk if skill else intent.risk),
            required_slots=(skill.required_slots if skill else intent.required_slots),
            similar_intents=intent.similar_intents,
            reason=(
                f"Intent {intent.name} is routed to "
                f"{skill.name if skill else 'no skill'}"
                f"{' and ' + tool.name if tool else ''}."
            ),
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "version": "agent-core-v1",
            "generated_at": _now_iso(),
            "layers": ["input", "intent", "skill", "toolbox", "execution", "output"],
            "intents": self.intents.to_dict(),
            "skills": self.skills.to_dict(),
            "tools": self.tools.to_dict(),
        }


def build_catalog_from_skill_catalog(
    skill_catalog: dict[str, dict[str, Any]],
    intent_relations: dict[str, Iterable[str]] | None = None,
) -> AgentCatalog:
    """Build standard registries from the existing product skill catalog."""

    intents = IntentRegistry()
    skills = SkillRegistry()
    tools = ToolRegistry()
    relations = intent_relations or {}

    for intent_name, definition in sorted(skill_catalog.items()):
        skill_name = str(definition.get("skill") or f"{intent_name.lower()}_skill")
        tool_name = definition.get("tool")
        required_slots = tuple(str(slot) for slot in definition.get("required_slots") or [])
        risk = str(definition.get("risk") or "READ_ONLY")
        label = str(definition.get("label") or intent_name)

        intents.register(
            IntentDefinition(
                name=intent_name,
                label=label,
                description=f"Intent for {label}.",
                similar_intents=_dedupe(relations.get(intent_name, ())),
                default_skill=skill_name,
                default_tool=str(tool_name) if tool_name else None,
                risk=risk,
                required_slots=required_slots,
            )
        )
        skills.register(
            SkillDefinition(
                name=skill_name,
                label=label,
                intent=intent_name,
                risk=risk,
                default_tool=str(tool_name) if tool_name else None,
                required_slots=required_slots,
                description=f"Skill bound to intent {intent_name}.",
            )
        )
        if tool_name:
            tools.register(
                ToolDefinition(
                    name=str(tool_name),
                    label=str(tool_name),
                    risk=risk,
                    description=f"Default tool for skill {skill_name}.",
                    input_schema={"required_slots": list(required_slots)},
                    output_schema={"type": "agent_tool_result"},
                )
            )

    return AgentCatalog(intents=intents, skills=skills, tools=tools)
