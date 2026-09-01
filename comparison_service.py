"""Fail-safe primitives for the OVD comparison-service P0/P1 boundary.

The module intentionally does not infer a SKU from a generic VLM answer.  It
normalizes a contracted OVD response and evaluates already-produced object
evidence against a slot rule.  If any prerequisite is missing, callers get a
review/system state instead of a normal/violation conclusion.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse


OVD_FAILURE_CODES = {
    "OVD_NOT_CONFIGURED",
    "OVD_ENDPOINT_REJECTED",
    "OVD_TIMEOUT",
    "OVD_HTTP_ERROR",
    "OVD_INVALID_RESPONSE",
    "OVD_INVALID_SCHEMA",
    "OVD_CIRCUIT_OPEN",
}


class OvdAdapterFailure(Exception):
    """A public, redacted OVD failure reason suitable for evidence records."""

    def __init__(self, code: str, message: str):
        self.code = code if code in OVD_FAILURE_CODES else "OVD_INVALID_RESPONSE"
        self.message = message
        super().__init__(self.code)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _score(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 1 else None


def _bbox(value: Any, image_width: int, image_height: int) -> list[float] | None:
    if isinstance(value, dict):
        value = [value.get("x1"), value.get("y1"), value.get("x2"), value.get("y2")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not (0 <= x1 < x2 <= image_width and 0 <= y1 < y2 <= image_height):
        return None
    return [x1, y1, x2, y2]


def _image_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Read dimensions from the supported image headers without decoding pixels.

    EAS returns boxes in source-image pixels but its OVD response does not echo
    the source dimensions.  The adapter must derive them locally before it can
    validate a box; otherwise an invalid coordinate could be confused with an
    empty detection result.
    """
    if len(image_bytes) >= 24 and image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(image_bytes[16:20], "big")
        height = int.from_bytes(image_bytes[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if len(image_bytes) >= 10 and image_bytes.startswith((b"GIF87a", b"GIF89a")):
        width = int.from_bytes(image_bytes[6:8], "little")
        height = int.from_bytes(image_bytes[8:10], "little")
        return (width, height) if width > 0 and height > 0 else None
    if len(image_bytes) >= 4 and image_bytes[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(image_bytes):
            if image_bytes[index] != 0xFF:
                index += 1
                continue
            while index < len(image_bytes) and image_bytes[index] == 0xFF:
                index += 1
            if index >= len(image_bytes):
                break
            marker = image_bytes[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(image_bytes):
                break
            segment_length = int.from_bytes(image_bytes[index:index + 2], "big")
            if segment_length < 2 or index + segment_length > len(image_bytes):
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            } and segment_length >= 7:
                height = int.from_bytes(image_bytes[index + 3:index + 5], "big")
                width = int.from_bytes(image_bytes[index + 5:index + 7], "big")
                return (width, height) if width > 0 and height > 0 else None
            index += segment_length
    return None


def normalize_eas_ovd_response(
    payload: Any,
    expected_prompts: list[str] | None,
    image_width: int,
    image_height: int,
    model_version: str,
) -> dict[str, Any]:
    """Normalize the documented Alibaba EAS OVD response into detection-v1.

    The vendor uses ``outputInfo`` and source-pixel ``[x, y, width, height]``
    boxes, whereas the internal contract is ``detections`` with ``xyxy`` boxes.
    Empty output remains a successful detector observation; an error response,
    malformed item, or out-of-image box remains a failure and can never prove
    that a target is absent.
    """
    outer = _as_dict(payload)
    raw_error_code = outer.get("errorCode")
    try:
        error_code = int(raw_error_code or 0)
    except (TypeError, ValueError):
        raise OvdAdapterFailure("OVD_INVALID_SCHEMA", "EAS OVD 响应的 errorCode 非法")
    if error_code != 0:
        # ``errorInfo`` may reveal vendor internals; retain only the fact of a
        # provider-side error in the public error record.
        raise OvdAdapterFailure("OVD_HTTP_ERROR", "EAS OVD 服务返回业务错误")
    request_id = _first_text(outer.get("requestID"), outer.get("request_id"))
    output_info = outer.get("outputInfo")
    if not request_id or not isinstance(output_info, list):
        raise OvdAdapterFailure("OVD_INVALID_SCHEMA", "EAS OVD 响应缺少 requestID 或 outputInfo")
    if not image_width or not image_height:
        raise OvdAdapterFailure("OVD_INVALID_RESPONSE", "无法读取待检测图片尺寸")

    prompt_set = {str(item).strip().casefold() for item in (expected_prompts or []) if str(item).strip()}
    normalized = []
    for index, raw_detection in enumerate(output_info):
        item = _as_dict(raw_detection)
        class_name = _first_text(item.get("label"))
        score = _score(item.get("score"))
        raw_box = item.get("box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", f"EAS OVD 第 {index + 1} 个检测对象缺少合法 box")
        try:
            x, y, width, height = [float(value) for value in raw_box]
        except (TypeError, ValueError):
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", f"EAS OVD 第 {index + 1} 个检测对象坐标非法")
        bbox = _bbox([x, y, x + width, y + height], image_width, image_height)
        if not class_name or score is None or bbox is None:
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", f"EAS OVD 第 {index + 1} 个检测对象字段或坐标非法")
        if prompt_set and class_name.casefold() not in prompt_set:
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", "EAS OVD 检测标签与本次受控提示词不一致")
        normalized.append(
            {
                "detection_id": f"eas_det_{index + 1}",
                "class_name": class_name,
                "prompt": class_name,
                "score": score,
                "bbox_xyxy": bbox,
                "mask": None,
            }
        )
    return {
        "request_id": request_id,
        "model_version": model_version or "eas_ovd",
        "image_width": image_width,
        "image_height": image_height,
        "detections": normalized,
    }


def normalize_ovd_response(payload: Any, expected_prompts: list[str] | None = None) -> dict[str, Any]:
    """Validate the vendor payload and return the internal ``detection[]`` shape.

    The source document did not include a response schema.  This is deliberately
    strict: a partial or coordinate-less result is rejected instead of becoming an
    empty detection list that could be mistaken for a missing SKU.
    """
    outer = _as_dict(payload)
    body = _as_dict(outer.get("data")) if isinstance(outer.get("data"), dict) else outer
    request_id = _first_text(body.get("request_id"), body.get("requestID"), outer.get("request_id"), outer.get("requestID"))
    model_version = _first_text(
        body.get("model_version"), body.get("modelVersion"), body.get("model"),
        outer.get("model_version"), outer.get("modelVersion"), outer.get("model"),
    )
    image_width = _positive_int(body.get("image_width") or body.get("imageWidth") or outer.get("image_width") or outer.get("imageWidth"))
    image_height = _positive_int(body.get("image_height") or body.get("imageHeight") or outer.get("image_height") or outer.get("imageHeight"))
    detections = body.get("detections") or body.get("results") or body.get("objects")
    if not request_id or not model_version or not image_width or not image_height or not isinstance(detections, list):
        raise OvdAdapterFailure("OVD_INVALID_SCHEMA", "OVD 响应缺少请求、模型、图像尺寸或检测数组契约字段")

    prompt_set = {str(item).strip().casefold() for item in (expected_prompts or []) if str(item).strip()}
    normalized = []
    for index, raw_detection in enumerate(detections):
        item = _as_dict(raw_detection)
        class_name = _first_text(item.get("class_name"), item.get("className"), item.get("label"), item.get("category"))
        score = _score(item.get("score") if "score" in item else item.get("confidence"))
        bbox = _bbox(item.get("bbox_xyxy") or item.get("bbox") or item.get("box"), image_width, image_height)
        if not class_name or score is None or bbox is None:
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", f"OVD 第 {index + 1} 个检测对象字段或坐标非法")
        prompt = _first_text(item.get("prompt"), item.get("text_prompt"), item.get("textPrompt"), class_name)
        if prompt_set and prompt.casefold() not in prompt_set and class_name.casefold() not in prompt_set:
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", "OVD 检测对象与本次受控提示词不一致")
        mask = item.get("mask")
        if mask is not None and not isinstance(mask, (str, list, dict)):
            raise OvdAdapterFailure("OVD_INVALID_SCHEMA", "OVD mask 字段类型非法")
        normalized.append(
            {
                "detection_id": _first_text(item.get("detection_id"), item.get("id"), f"det_{index + 1}"),
                "class_name": class_name,
                "prompt": prompt,
                "score": score,
                "bbox_xyxy": bbox,
                "mask": mask,
            }
        )
    return {
        "request_id": request_id,
        "model_version": model_version,
        "image_width": image_width,
        "image_height": image_height,
        "detections": normalized,
    }


def ovd_contract_report(payload: Any, expected_prompts: list[str] | None = None) -> dict[str, Any]:
    """Return a test report without returning raw vendor payloads or credentials."""
    try:
        normalized = normalize_ovd_response(payload, expected_prompts)
    except OvdAdapterFailure as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}
    return {
        "ok": True,
        "request_id": normalized["request_id"],
        "model_version": normalized["model_version"],
        "image_width": normalized["image_width"],
        "image_height": normalized["image_height"],
        "detection_count": len(normalized["detections"]),
        "coordinate_system": "pixel_xyxy",
    }


def eas_ovd_contract_report(
    payload: Any,
    expected_prompts: list[str] | None = None,
    image_width: Any = None,
    image_height: Any = None,
    model_version: Any = "pytrt_sam3",
) -> dict[str, Any]:
    """Validate an EAS response sample without accepting any credential or image."""
    try:
        normalized = normalize_eas_ovd_response(
            payload,
            expected_prompts,
            _positive_int(image_width) or 0,
            _positive_int(image_height) or 0,
            _first_text(model_version) or "pytrt_sam3",
        )
    except OvdAdapterFailure as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}
    return {
        "ok": True,
        "request_id": normalized["request_id"],
        "model_version": normalized["model_version"],
        "image_width": normalized["image_width"],
        "image_height": normalized["image_height"],
        "detection_count": len(normalized["detections"]),
        "coordinate_system": "pixel_xyxy",
    }


@dataclass(frozen=True)
class OvdAdapterConfig:
    endpoint: str
    authorization: str
    client_id: str
    allowed_hosts: frozenset[str]
    timeout_seconds: float = 8.0
    threshold: float = 0.4
    provider: str = "generic"
    model_version: str = "external_ovd"

    @classmethod
    def from_environment(cls) -> "OvdAdapterConfig":
        eas_token = str(os.environ.get("OVD_EAS_TOKEN") or "").strip()
        eas_model = str(os.environ.get("OVD_EAS_MODEL") or "pytrt_sam3").strip()
        eas_account_id = str(os.environ.get("OVD_EAS_ACCOUNT_ID") or "").strip()
        eas_region = str(os.environ.get("OVD_EAS_REGION") or "").strip()
        eas_host = (
            f"{eas_account_id}.{eas_region}.pai-eas.aliyuncs.com"
            if eas_account_id and eas_region
            else ""
        )
        eas_endpoint = str(os.environ.get("OVD_EAS_ENDPOINT") or "").strip()
        if eas_token and not eas_endpoint and eas_host and eas_model:
            eas_endpoint = f"https://{eas_host}/api/predict/{eas_model}/ovd"
        allowed_hosts = frozenset(
            item.strip().casefold()
            for item in str(os.environ.get("OVD_ALLOWED_HOSTS") or "").split(",")
            if item.strip()
        ) or (frozenset({eas_host.casefold()}) if eas_token and eas_host else frozenset())
        try:
            timeout_seconds = min(30.0, max(1.0, float(os.environ.get("OVD_TIMEOUT_SECONDS") or 8.0)))
        except ValueError:
            timeout_seconds = 8.0
        try:
            threshold = min(1.0, max(0.0, float(os.environ.get("OVD_THRESHOLD") or 0.4)))
        except ValueError:
            threshold = 0.4
        return cls(
            endpoint=eas_endpoint if eas_token else str(os.environ.get("OVD_BASE_URL") or "").strip(),
            authorization=eas_token if eas_token else str(os.environ.get("OVD_AUTHORIZATION") or "").strip(),
            client_id=str(os.environ.get("OVD_CLIENT_ID") or "wanxiang-comparison-service").strip(),
            allowed_hosts=allowed_hosts,
            timeout_seconds=timeout_seconds,
            threshold=threshold,
            provider="eas" if eas_token else "generic",
            model_version=eas_model if eas_token else str(os.environ.get("OVD_MODEL_VERSION") or "external_ovd").strip(),
        )


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_ovd_endpoint(config: OvdAdapterConfig, *, resolve_dns: bool = True) -> str:
    parsed = urlparse(config.endpoint)
    host = (parsed.hostname or "").casefold()
    if not config.endpoint or not config.authorization or not config.allowed_hosts:
        raise OvdAdapterFailure("OVD_NOT_CONFIGURED", "OVD 未完成环境级安全配置")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or host not in config.allowed_hosts:
        raise OvdAdapterFailure("OVD_ENDPOINT_REJECTED", "OVD 地址未通过 HTTPS 或白名单校验")
    if parsed.port not in (None, 443):
        raise OvdAdapterFailure("OVD_ENDPOINT_REJECTED", "OVD 地址端口不在允许范围")
    if not resolve_dns:
        return host
    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise OvdAdapterFailure("OVD_ENDPOINT_REJECTED", "无法解析 OVD 白名单地址") from exc
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise OvdAdapterFailure("OVD_ENDPOINT_REJECTED", "OVD 地址解析到非公网地址")
    return host


class SafeOvdAdapter:
    """Small server-only OVD adapter with strict payload and circuit safeguards."""

    def __init__(self, config: OvdAdapterConfig | None = None, transport: Callable[..., bytes] | None = None):
        self.config = config or OvdAdapterConfig.from_environment()
        self.transport = transport
        self._failure_count = 0
        self._open_until = 0.0

    @property
    def configured(self) -> bool:
        """Return only the local configuration state; no network call is made."""
        try:
            validate_ovd_endpoint(self.config, resolve_dns=False)
        except OvdAdapterFailure:
            return False
        return True

    def inspect_bytes(self, image_bytes: bytes, prompts: list[str], correlation_id: str | None = None) -> dict[str, Any]:
        if time.monotonic() < self._open_until:
            raise OvdAdapterFailure("OVD_CIRCUIT_OPEN", "OVD 熔断保护已开启，请稍后重试")
        validate_ovd_endpoint(self.config)
        if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
            raise OvdAdapterFailure("OVD_INVALID_RESPONSE", "待检测图片为空或超过 OVD 安全大小限制")
        safe_prompts = [str(item).strip()[:120] for item in prompts if str(item).strip()]
        if not safe_prompts:
            raise OvdAdapterFailure("OVD_INVALID_RESPONSE", "缺少受控 OVD 提示词")
        correlation_id = str(correlation_id or f"ovd_{uuid.uuid4().hex[:16]}")[:80]
        payload = {
            "inputParaJson": {
                "requestID": correlation_id,
                "clientID": self.config.client_id[:120],
                "textPrompts": safe_prompts,
                "threshold": self.config.threshold,
            },
            "imgInfoJson": {"imgData": base64.b64encode(image_bytes).decode("ascii"), "imgUrl": ""},
        }
        raw_request = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            response_bytes = self._send(raw_request)
            parsed = json.loads(response_bytes.decode("utf-8"))
            if self.config.provider == "eas":
                dimensions = _image_dimensions(image_bytes)
                if dimensions is None:
                    raise OvdAdapterFailure("OVD_INVALID_RESPONSE", "无法读取待检测图片尺寸")
                result = normalize_eas_ovd_response(
                    parsed,
                    safe_prompts,
                    dimensions[0],
                    dimensions[1],
                    self.config.model_version,
                )
            else:
                result = normalize_ovd_response(parsed, safe_prompts)
            self._failure_count = 0
            return result
        except OvdAdapterFailure:
            self._record_failure()
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._record_failure()
            raise OvdAdapterFailure("OVD_INVALID_RESPONSE", "OVD 返回不是有效 JSON") from exc

    def _send(self, raw_request: bytes) -> bytes:
        if self.transport:
            return self.transport(self.config.endpoint, raw_request, self.config.authorization, self.config.timeout_seconds)
        request = urlrequest.Request(
            self.config.endpoint,
            data=raw_request,
            headers={"Authorization": self.config.authorization, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with urlrequest.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    if response.status < 200 or response.status >= 300:
                        raise OvdAdapterFailure("OVD_HTTP_ERROR", "OVD 返回非成功状态")
                    data = response.read(2 * 1024 * 1024 + 1)
                    if len(data) > 2 * 1024 * 1024:
                        raise OvdAdapterFailure("OVD_INVALID_RESPONSE", "OVD 响应超过安全大小限制")
                    return data
            except OvdAdapterFailure:
                raise
            except (urlerror.HTTPError, urlerror.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.15)
        if isinstance(last_error, TimeoutError):
            raise OvdAdapterFailure("OVD_TIMEOUT", "OVD 调用超时") from last_error
        raise OvdAdapterFailure("OVD_HTTP_ERROR", "OVD 调用失败") from last_error

    def _record_failure(self):
        self._failure_count += 1
        if self._failure_count >= 3:
            self._open_until = time.monotonic() + 30.0


def _frame_is_valid(frame: dict[str, Any], slot: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    state = str(frame.get("state") or "").upper()
    if state == "SYSTEM_FAILED":
        return False, [str(frame.get("failure_code") or "OVD_FAILED")]
    try:
        quality = float(frame.get("quality_score") if frame.get("quality_score") is not None else 0)
        coverage = float(frame.get("roi_coverage") if frame.get("roi_coverage") is not None else 0)
        occlusion = float(frame.get("occlusion_ratio") if frame.get("occlusion_ratio") is not None else 1)
    except (TypeError, ValueError):
        return False, ["INVALID_QUALITY_METRICS"]
    quality_threshold = float(slot.get("quality_threshold") if slot.get("quality_threshold") is not None else 0.7)
    min_coverage = float(slot.get("min_roi_coverage") if slot.get("min_roi_coverage") is not None else 0.8)
    max_occlusion = float(slot.get("max_occlusion") if slot.get("max_occlusion") is not None else 0.2)
    if quality < quality_threshold:
        reasons.append("LOW_QUALITY")
    if coverage < min_coverage:
        reasons.append("LOW_COVERAGE")
    if occlusion > max_occlusion:
        reasons.append("OCCLUDED")
    if str(frame.get("camera_health") or "").upper() != "GREEN":
        reasons.append("CAMERA_NOT_GREEN")
    return not reasons, reasons


def evaluate_slot_evidence(slot: dict[str, Any], frames: list[dict[str, Any]], reference_skus: set[str], *, prerequisites_ok: bool = True) -> dict[str, Any]:
    """Apply the documented time-window hard rules to immutable frame evidence."""
    expected_skus = {str(item).strip().upper() for item in (slot.get("expected_skus") or []) if str(item).strip()}
    expected_count = max(1, int(slot.get("expected_count") or 1))
    min_valid_frames = max(1, int(slot.get("min_valid_frames") or 3))
    if not prerequisites_ok or not expected_skus or not expected_skus.issubset(reference_skus):
        return {"state": "REVIEW", "reason_codes": ["REFERENCE_OR_POLICY_MISSING"], "observed_count": 0, "valid_frame_count": 0}

    system_failed = [frame for frame in frames if str(frame.get("state") or "").upper() == "SYSTEM_FAILED"]
    if system_failed:
        return {"state": "SYSTEM_FAILED", "reason_codes": [str(system_failed[0].get("failure_code") or "OVD_FAILED")], "observed_count": 0, "valid_frame_count": 0}

    valid_frames: list[dict[str, Any]] = []
    invalid_reasons: list[str] = []
    for frame in frames:
        valid, reasons = _frame_is_valid(frame, slot)
        if valid:
            valid_frames.append(frame)
        else:
            invalid_reasons.extend(reasons)
    if len(valid_frames) < min_valid_frames:
        return {
            "state": "INCONCLUSIVE",
            "reason_codes": sorted(set(invalid_reasons + ["INSUFFICIENT_VALID_FRAMES"])),
            "observed_count": 0,
            "valid_frame_count": len(valid_frames),
        }

    max_allowed_count = 0
    seen_forbidden = False
    seen_unknown = False
    for frame in valid_frames:
        matched_keys = set()
        for index, obj in enumerate(frame.get("object_evidence") or []):
            if not isinstance(obj, dict):
                continue
            sku_id = str(obj.get("sku_id") or obj.get("identity_id") or "").strip().upper()
            state = str(obj.get("state") or "UNKNOWN").upper()
            if state == "MATCHED" and sku_id in expected_skus:
                matched_keys.add(str(obj.get("track_id") or f"{sku_id}#{index}"))
            elif state == "MATCHED" and sku_id and sku_id not in expected_skus:
                seen_forbidden = True
            elif state in {"UNKNOWN", "AMBIGUOUS", "NOT_VISIBLE"}:
                seen_unknown = True
        max_allowed_count = max(max_allowed_count, len(matched_keys))
    if seen_forbidden:
        return {"state": "SUSPECTED_VIOLATION", "reason_codes": ["FORBIDDEN_IDENTITY", "MULTI_FRAME_SUPPORT"], "observed_count": max_allowed_count, "valid_frame_count": len(valid_frames)}
    if max_allowed_count >= expected_count:
        return {"state": "COMPLIANT", "reason_codes": ["TEMPORAL_MATCH", "CALIBRATED_PASS"], "observed_count": max_allowed_count, "valid_frame_count": len(valid_frames)}
    if seen_unknown:
        return {"state": "REVIEW", "reason_codes": ["IDENTITY_AMBIGUOUS"], "observed_count": max_allowed_count, "valid_frame_count": len(valid_frames)}
    return {"state": "SUSPECTED_VIOLATION", "reason_codes": ["MISSING_EXPECTED", "COVERAGE_OK", "MULTI_FRAME_SUPPORT"], "observed_count": max_allowed_count, "valid_frame_count": len(valid_frames)}
