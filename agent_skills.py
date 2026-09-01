#!/usr/bin/env python3
"""Intent skills, slot filling, and inspection pipeline planning."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_core import AgentCatalog, ToolDefinition, build_catalog_from_skill_catalog
from visual_compliance import (
    VISUAL_COMPLIANCE_CAPABILITY_ID,
    VISUAL_COMPLIANCE_NAME,
    extract_visual_compliance_pack,
    is_visual_compliance_request,
)


CN_TZ = timezone(timedelta(hours=8))

SKILL_CATALOG = {
    "QUERY_ALARMS": {
        "skill": "history_alarm_query",
        "label": "历史预警查询",
        "risk": "READ_ONLY",
        "tool": "paas.alarm.query",
        "required_slots": [],
    },
    "ANALYZE_ALARMS": {
        "skill": "history_alarm_analysis",
        "label": "历史预警分析",
        "risk": "READ_ONLY",
        "tool": "paas.alarm.aggregate",
        "required_slots": [],
    },
    "QUERY_CAMERAS": {
        "skill": "camera_inventory_query",
        "label": "摄像头查询",
        "risk": "READ_ONLY",
        "tool": "paas.camera.page",
        "required_slots": [],
    },
    "QUERY_DEVICE_STATUS": {
        "skill": "device_health_query",
        "label": "设备状态查询",
        "risk": "READ_ONLY",
        "tool": "paas.device.health",
        "required_slots": [],
    },
    "VIEW_LIVE_STREAM": {
        "skill": "camera_live_view",
        "label": "摄像头直播",
        "risk": "TRANSIENT_SESSION",
        "tool": "paas.media.live.start",
        "required_slots": ["camera_id"],
    },
    "VIEW_PLAYBACK": {
        "skill": "camera_playback_view",
        "label": "录像回放",
        "risk": "TRANSIENT_SESSION",
        "tool": "paas.media.playback.start",
        "required_slots": ["camera_id", "playback_range"],
    },
    "CAPTURE_SNAPSHOT": {
        "skill": "camera_frame_capture",
        "label": "监控画面抓图",
        "risk": "TRANSIENT_SESSION",
        "tool": "paas.media.snapshot",
        "required_slots": ["camera_id", "capture_at"],
    },
    "ANALYZE_VISUAL": {
        "skill": "visual_scene_inspection",
        "label": "监控画面智能判断",
        "risk": "READ_ONLY",
        "tool": "vlm.image.inspect",
        "required_slots": [],
    },
    "CREATE_SCHEDULED_INSPECTION": {
        "skill": "scheduled_snapshot_inspection",
        "label": "周期快照 AI 巡检",
        "risk": "HIGH_WRITE",
        "tool": "scheduler.inspection.create",
        "required_slots": ["org_id", "camera_ids", "interval", "daily_window", "effective_time_range", "inspection_goal"],
    },
    "BATCH_SCHEDULED_INSPECTION_CREATE": {
        "skill": "multi_store_scheduled_inspection",
        "label": "多门店周期快照 AI 巡检",
        "risk": "HIGH_WRITE",
        "tool": "batch_inspection.create",
        "required_slots": ["org_scope", "camera_ids", "interval", "daily_window", "effective_time_range", "inspection_goal"],
    },
    "BATCH_INSPECTION_EXECUTE": {
        "skill": "multi_store_visual_inspection",
        "label": "多门店即时 AI 巡检",
        "risk": "HIGH_WRITE",
        "tool": "batch_inspection.execute",
        "required_slots": ["org_scope", "camera_ids", "inspection_goal"],
    },
    "VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE": {
        "skill": "visual_compliance_inspection",
        "label": "门店视觉合规巡检",
        "risk": "HIGH_WRITE",
        "tool": "paas.subscription.create",
        "required_slots": ["object_pack", "effective_time_range", "camera_ids", "schedule"],
    },
    "QUERY_SUBSCRIPTIONS": {
        "skill": "application_subscription_query",
        "label": "应用订阅查询",
        "risk": "READ_ONLY",
        "tool": "paas.capability.configured",
        "required_slots": [],
    },
    "QUERY_CAPABILITIES": {
        "skill": "application_subscription_query",
        "label": "能力配置查询",
        "risk": "READ_ONLY",
        "tool": "paas.capability.configured",
        "required_slots": [],
    },
    "CREATE_TASK": {
        "skill": "existing_capability_subscription",
        "label": "已有能力订阅",
        "risk": "HIGH_WRITE",
        "tool": "paas.subscription.create",
        "required_slots": ["effective_time_range", "camera_ids", "thresholds", "roi"],
    },
    "COMPOSE_CAPABILITY": {
        "skill": "composite_capability_pipeline",
        "label": "新能力编排",
        "risk": "DESIGN_ONLY",
        "tool": "pipeline.compose",
        "required_slots": ["goal"],
    },
    "OPEN_QA": {
        "skill": "open_question_answering",
        "label": "开放性问答",
        "risk": "READ_ONLY",
        "tool": None,
        "required_slots": [],
    },
    "FEEDBACK_ALARM": {
        "skill": "alarm_feedback",
        "label": "告警反馈",
        "risk": "HIGH_WRITE",
        "tool": "paas.alarm.feedback",
        "required_slots": ["alarm_id", "feedback_type"],
    },
    "HELP": {
        "skill": "agent_capability_help",
        "label": "能力帮助",
        "risk": "READ_ONLY",
        "tool": None,
        "required_slots": [],
    },
}

RUNTIME_TOOL_CATALOG = {
    "web.search": {
        "label": "公共网页检索",
        "risk": "READ_ONLY",
        "description": "仅在开放问答需要核验实时公共事实时检索可信网页，不接收租户、门店、会话历史或凭证数据。",
        "input_schema": {"required": ["query"], "optional": ["freshness", "locale", "max_results"]},
        "output_schema": {"type": "web_search_result", "fields": ["provider", "fetched_at", "citations"]},
    },
    "travel.recommendations.search": {
        "label": "旅行地点候选检索",
        "risk": "READ_ONLY",
        "description": "仅在明确的旅行攻略请求中，批量检索住宿和餐饮公开候选、地址线索与地图核验入口。",
        "input_schema": {"required": ["destination"], "optional": ["travel_year", "categories"]},
        "output_schema": {"type": "travel_recommendations", "fields": ["hotels", "restaurants", "source_urls", "map_urls"]},
    },
    "places.wikidata.lookup": {
        "label": "开放地点数据核验",
        "risk": "READ_ONLY",
        "description": "用 Wikidata 的地点实体、坐标和公开地址补齐旅行候选，并与网页推荐线索交叉排序。",
        "input_schema": {"required": ["destination"], "optional": ["categories", "radius_km"]},
        "output_schema": {"type": "open_place_candidates", "fields": ["name", "address", "coordinates", "source_url", "map_url"]},
    },
    "media.wikimedia.search": {
        "label": "开放授权旅行配图",
        "risk": "READ_ONLY",
        "description": "从 Wikimedia Commons 检索目的地图片，并保留作者、许可和素材页用于文档署名。",
        "input_schema": {"required": ["destination"], "optional": ["max_images"]},
        "output_schema": {"type": "licensed_media", "fields": ["thumbnail_url", "author", "license", "source_url"]},
    },
    "document.generate_pdf": {
        "label": "PDF 文档生成",
        "risk": "READ_ONLY",
        "description": "按用户明确要求，把开放问答结果与公开来源生成可下载 PDF；不读取巡检业务数据。",
        "input_schema": {"required": ["conversation_id", "content"], "optional": ["title", "citations"]},
        "output_schema": {"type": "generated_document", "fields": ["document_id", "filename", "mime_type", "size_bytes", "download_url"]},
    },
    "credential.redact": {
        "label": "凭证脱敏",
        "risk": "READ_ONLY",
        "description": "在聊天入库前移除 AppKey/AppSecret 等敏感字段。",
        "input_schema": {"required": ["raw_text"]},
    },
    "paas.auth.verify": {
        "label": "租户凭证验证",
        "risk": "HIGH_WRITE",
        "description": "验证 DeepVision 租户凭证并同步门店接入信息。",
        "input_schema": {"required": ["tenant_name", "tenant_code", "credential_ref"]},
    },
    "event.emit": {
        "label": "结果事件输出",
        "risk": "READ_ONLY",
        "description": "输出巡检结论、证据摘要和可追溯事件。",
        "input_schema": {"required": ["tenant_id", "org_id", "summary"]},
    },
    "evidence.archive": {
        "label": "证据归档",
        "risk": "READ_ONLY",
        "description": "归档本次巡检图片、异常标记和模型判断结果。",
        "input_schema": {"required": ["run_id", "evidence"]},
    },
    "knowledge.retrieve": {
        "label": "知识库召回",
        "risk": "READ_ONLY",
        "description": "按巡检目标召回 SOP、品牌规范、参考图片和门店平面图等知识。",
        "input_schema": {"required": ["tenant_id", "query"]},
    },
    "memory.retrieve": {
        "label": "长期记忆召回",
        "risk": "READ_ONLY",
        "description": "召回用户偏好、门店别名和业务判断口径，供 Agent 推理链路使用。",
        "input_schema": {"required": ["tenant_id", "user_id", "query"]},
    },
    "scheduler.run.persist": {
        "label": "周期巡检结果保存",
        "risk": "READ_ONLY",
        "description": "保存周期任务单次执行结果，供告警与证据页面追溯。",
        "input_schema": {"required": ["task_id", "run_id", "result_status"]},
    },
    "batch_inspection.create": {
        "label": "批量巡检任务创建",
        "risk": "HIGH_WRITE",
        "description": "按多门店范围创建批量父任务，并拆分生成每家门店的周期快照巡检子任务。",
        "input_schema": {"required": ["store_tasks", "schedule", "inspection_goal"]},
    },
    "batch_inspection.execute": {
        "label": "批量即时巡检执行",
        "risk": "HIGH_WRITE",
        "description": "按多门店范围创建批量父任务，立即抓取各门店在线摄像头快照，调用视觉模型分析并归档证据。",
        "input_schema": {"required": ["store_tasks", "inspection_goal"]},
    },
}

INTENTS = set(SKILL_CATALOG)

INTENT_RELATIONS = {
    "QUERY_ALARMS": ("ANALYZE_ALARMS", "FEEDBACK_ALARM"),
    "ANALYZE_ALARMS": ("QUERY_ALARMS",),
    "QUERY_CAMERAS": ("QUERY_DEVICE_STATUS", "CAPTURE_SNAPSHOT", "VIEW_LIVE_STREAM"),
    "QUERY_DEVICE_STATUS": ("QUERY_CAMERAS", "VIEW_LIVE_STREAM"),
    "VIEW_LIVE_STREAM": ("CAPTURE_SNAPSHOT", "VIEW_PLAYBACK", "ANALYZE_VISUAL"),
    "VIEW_PLAYBACK": ("CAPTURE_SNAPSHOT", "ANALYZE_VISUAL"),
    "CAPTURE_SNAPSHOT": ("VIEW_LIVE_STREAM", "VIEW_PLAYBACK", "ANALYZE_VISUAL"),
    "ANALYZE_VISUAL": ("CAPTURE_SNAPSHOT", "CREATE_SCHEDULED_INSPECTION", "VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE"),
    "CREATE_SCHEDULED_INSPECTION": ("ANALYZE_VISUAL", "CAPTURE_SNAPSHOT", "BATCH_SCHEDULED_INSPECTION_CREATE", "BATCH_INSPECTION_EXECUTE"),
    "BATCH_SCHEDULED_INSPECTION_CREATE": ("BATCH_INSPECTION_EXECUTE", "CREATE_SCHEDULED_INSPECTION", "ANALYZE_VISUAL", "CAPTURE_SNAPSHOT"),
    "BATCH_INSPECTION_EXECUTE": ("BATCH_SCHEDULED_INSPECTION_CREATE", "ANALYZE_VISUAL", "CAPTURE_SNAPSHOT"),
    "VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE": ("CREATE_SCHEDULED_INSPECTION", "ANALYZE_VISUAL"),
    "QUERY_SUBSCRIPTIONS": ("QUERY_CAPABILITIES",),
    "QUERY_CAPABILITIES": ("QUERY_SUBSCRIPTIONS",),
    "CREATE_TASK": ("COMPOSE_CAPABILITY", "VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE"),
    "COMPOSE_CAPABILITY": ("CREATE_TASK", "ANALYZE_VISUAL"),
    "OPEN_QA": ("HELP",),
    "FEEDBACK_ALARM": ("QUERY_ALARMS",),
    "HELP": ("QUERY_ALARMS", "ANALYZE_VISUAL", "CREATE_SCHEDULED_INSPECTION", "OPEN_QA"),
}

CAPABILITY_DEFAULT_THRESHOLDS = {
    "off_duty": {"duration_seconds": 300, "confidence": 0.80},
    "cloth_detect": {"confidence": 0.80},
    "play_phone": {"duration_seconds": 10, "confidence": 0.80},
    "person_smoke": {"confidence": 0.82},
    "crowd": {"person_count": 5, "duration_seconds": 30, "confidence": 0.78},
    "cross_line": {"confidence": 0.80},
    "sleep_duty": {"duration_seconds": 180, "confidence": 0.80},
    VISUAL_COMPLIANCE_CAPABILITY_ID: {
        "confidence": 0.80,
        "low_confidence_to_pending": True,
        "require_marked_anomaly_image": True,
    },
}


def skill_descriptor(intent: str) -> dict:
    definition = SKILL_CATALOG.get(intent) or SKILL_CATALOG["HELP"]
    return {"intent": intent, **definition}


def public_skill_catalog() -> list[dict]:
    return [skill_descriptor(intent) for intent in sorted(SKILL_CATALOG)]


def standard_agent_catalog() -> AgentCatalog:
    catalog = build_catalog_from_skill_catalog(SKILL_CATALOG, INTENT_RELATIONS)
    for name, definition in RUNTIME_TOOL_CATALOG.items():
        catalog.register_tool(
            ToolDefinition(
                name=name,
                label=str(definition.get("label") or name),
                risk=str(definition.get("risk") or "READ_ONLY"),
                description=str(definition.get("description") or ""),
                input_schema=definition.get("input_schema") or {},
                output_schema=definition.get("output_schema") or {"type": "agent_tool_result"},
            )
        )
    return catalog


def public_agent_catalog() -> dict:
    return standard_agent_catalog().to_manifest()


def infer_intent(text: str, known_capability: bool) -> str:
    if any(word in text for word in ("误报", "真警", "忽略", "反馈")):
        return "FEEDBACK_ALARM"
    scheduled_pattern = re.search(r"每\s*(?:隔\s*)?(?:\d+|[一二三四五六七八九十两半]{1,3})\s*(?:h|H|小时|分钟|min)", text)
    fixed_daily_pattern = re.search(
        r"(?:每天|每日)\s*(?:凌晨|上午|下午|早上|中午|晚上)?\s*\d{1,2}\s*(?::\s*\d{1,2}|[点时]\s*(?:\d{1,2}\s*分?|半)?)",
        text,
    )
    if (scheduled_pattern or fixed_daily_pattern) and any(word in text for word in ("巡检", "看下", "看看", "看一下", "检查", "分析", "截取", "抓取", "轮询")):
        return "CREATE_SCHEDULED_INSPECTION"
    visual_context = ("画面", "图片", "快照", "监控", "镜头", "图中", "这些", "地面", "门口", "场景", "视频", "视频里", "视频中", "门店内", "展厅内", "售后", "售后区", "售后区域", "售后服务区", "服务区", "服务区域", "维修区", "维修区域")
    inspection_targets = ("垃圾", "杂物", "异常", "风险", "烟火", "摔倒", "抽烟", "玩手机", "占道", "聚集", "离岗", "睡岗", "员工", "工作人员", "顾客", "人员", "在岗", "值守", "接待", "服务", "排队", "Logo", "logo", "海报", "立牌", "展架", "座椅", "广告", "车标", "其他品牌", "竞品")
    judgment_actions = ("有没有", "有无", "是否", "判断", "识别", "分析", "检查", "确认")
    soft_judgment_actions = ("看看", "看下", "注意", "查下")
    # Object-search predicates are open-vocabulary: the target may be a sofa
    # today and a backpack, fire extinguisher or unknown product tomorrow.  Do
    # not maintain a noun allow-list.  Instead distinguish "find an object in
    # the pictures" from the media-only request "find/open a camera picture".
    visual_search = re.search(
        r"(?:找|寻找|查找|搜寻|定位|识别出?|检测到?)"
        r"\s*(?:一下|出|到)?\s*(?:一个|一名|一位|一辆|一件|所有|全部)?\s*"
        r"(?P<target>[^\s，。！？,!?]{1,80})",
        text,
    )
    visual_search_target = str(visual_search.group("target") if visual_search else "")
    media_only_search = bool(
        visual_search_target
        and re.fullmatch(
            r".{0,24}(?:当前|现在)?(?:门店|店内|现场)?"
            r"(?:监控|摄像头|镜头|视频)?(?:画面|图像|图片|快照|截图|视频)",
            visual_search_target,
        )
    )
    has_open_visual_search = bool(
        visual_search
        and not media_only_search
        and any(word in text for word in visual_context)
    )
    has_visual_quantity_or_attribute_question = bool(
        any(word in text for word in visual_context)
        and (
            re.search(r"(?:几|多少)(?:个|名|位|辆|件|把|张|台)?", text)
            or re.search(r"(?:什么|哪种|何种)(?:颜色|款式|类型|状态)", text)
        )
    )
    task_request = any(word in text for word in ("创建", "订阅", "启用", "上线", "配置", "开通", "巡检"))
    if is_visual_compliance_request(text) and task_request:
        return "VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE"
    if not task_request and (
        (
            (any(word in text for word in visual_context) or any(word in text for word in inspection_targets))
            and any(word in text for word in judgment_actions)
        )
        or (any(word in text for word in inspection_targets) and any(word in text for word in soft_judgment_actions))
        or has_open_visual_search
        or has_visual_quantity_or_attribute_question
    ):
        return "ANALYZE_VISUAL"
    if any(word in text for word in ("录像", "回放", "历史视频", "历史画面")):
        return "VIEW_PLAYBACK"
    view_actions = ("看下", "看看", "查看", "打开", "调取", "获取", "给我看", "播放", "能拍到", "拍到")
    frame_terms = ("画面", "图像", "图片", "快照", "截图")
    live_terms = ("直播", "实时画面", "实时视频", "现场画面", "现场视频")
    video_terms = ("监控视频", "摄像头视频", "镜头视频", "视频画面", "视频")
    if any(word in text for word in live_terms):
        return "VIEW_LIVE_STREAM"
    if any(word in text for word in view_actions) and any(word in text for word in frame_terms):
        return "CAPTURE_SNAPSHOT"
    if any(word in text for word in ("截图", "抓图", "快照", "画面图像", "画面图片", "监控画面")):
        return "CAPTURE_SNAPSHOT"
    if any(word in text for word in view_actions) and any(word in text for word in video_terms):
        return "VIEW_LIVE_STREAM"
    if (
        any(word in text for word in ("应用", "能力", "功能", "算法"))
        and any(word in text for word in ("查看", "查询", "哪些", "什么", "已有", "已经", "订阅了", "上线了"))
    ):
        return "QUERY_SUBSCRIPTIONS"
    if any(word in text for word in ("服务器状态", "设备状态", "设备健康", "在线状态", "离线状态")):
        return "QUERY_DEVICE_STATUS"
    if any(word in text for word in ("创建", "订阅", "启用", "上线", "配置", "开通", "巡检")):
        return "CREATE_TASK" if known_capability else "COMPOSE_CAPABILITY"
    if any(word in text for word in ("统计", "排行", "趋势", "最多", "分析", "汇总", "分布", "Top", "TOP")):
        return "ANALYZE_ALARMS"
    if any(word in text for word in ("摄像头", "镜头", "设备", "在线", "离线")):
        return "QUERY_CAMERAS"
    if any(word in text for word in ("告警", "预警", "事件", "证据", "离岗", "工服", "手机", "抽烟", "占道", "聚集", "跨线", "睡岗")):
        return "QUERY_ALARMS"
    return "HELP"


def conversation_text(text: str, history: list[dict]) -> str:
    prior = [str(item.get("content") or "") for item in history if item.get("sender") == "user"]
    return "\n".join([*prior[-6:], text]).strip()


def parse_explicit_datetime(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(CN_TZ)
    iso = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2})?(?:[:点时](\d{1,2}))?", text)
    if iso:
        year, month, day = (int(iso.group(index)) for index in range(1, 4))
        hour = int(iso.group(4) or 0)
        minute = int(iso.group(5) or 0)
        try:
            return datetime(year, month, day, hour, minute, tzinfo=CN_TZ)
        except ValueError:
            return None

    relative = re.search(r"(昨天|今天|前天)?\s*(上午|下午|晚上|中午|凌晨)?\s*(\d{1,2})\s*[点时:](\d{1,2})?", text)
    if not relative:
        return now if any(word in text for word in ("现在", "当前", "实时", "此刻", "刚刚")) else None
    day_word, period, hour_text, minute_text = relative.groups()
    target_date = now.date() - timedelta(days={"昨天": 1, "前天": 2}.get(day_word, 0))
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return datetime.combine(target_date, datetime.min.time(), CN_TZ).replace(hour=hour, minute=minute)


def parse_playback_range(text: str, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(CN_TZ)
    explicit_dates = list(re.finditer(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s*\d{1,2}[:点时]\d{1,2}", text))
    if len(explicit_dates) >= 2:
        start = parse_explicit_datetime(explicit_dates[0].group(0), now)
        end = parse_explicit_datetime(explicit_dates[1].group(0), now)
    else:
        clock_matches = list(re.finditer(r"(?:昨天|今天|前天)?\s*(?:上午|下午|晚上|中午|凌晨)?\s*\d{1,2}\s*[点时:]\s*\d{0,2}", text))
        if not clock_matches:
            return None
        first_text = clock_matches[0].group(0)
        start = parse_explicit_datetime(first_text, now)
        if len(clock_matches) >= 2:
            second_text = clock_matches[1].group(0)
            if not any(word in second_text for word in ("昨天", "今天", "前天")):
                day_prefix = next((word for word in ("昨天", "今天", "前天") if word in first_text), "今天")
                second_text = day_prefix + second_text
            end = parse_explicit_datetime(second_text, now)
        else:
            end = start + timedelta(minutes=10) if start else None
    if not start or not end or end <= start or end - start > timedelta(hours=12):
        return None
    return {
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "start_ms": int(start.timestamp() * 1000),
        "end_ms": int(end.timestamp() * 1000),
        "label": f"{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%H:%M')}",
    }


def parse_effective_range(text: str, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(CN_TZ)
    if any(word in text for word in ("长期生效", "永久生效", "持续生效")):
        return {"start": now.date().isoformat(), "end": None, "label": "立即开始，长期生效"}
    dates = re.findall(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text)
    if len(dates) >= 2:
        normalized = [item.replace("/", "-") for item in dates[:2]]
        try:
            start = datetime.fromisoformat(normalized[0]).date()
            end = datetime.fromisoformat(normalized[1]).date()
        except ValueError:
            return None
        if end < start:
            return None
        return {"start": start.isoformat(), "end": end.isoformat(), "label": f"{start.isoformat()} 至 {end.isoformat()}"}
    if "下周" in text:
        start = now.date() + timedelta(days=7 - now.date().weekday())
        end = start + timedelta(days=6)
        return {"start": start.isoformat(), "end": end.isoformat(), "label": "下周"}
    return None


def parse_thresholds(text: str, capability_code: str | None) -> dict | None:
    if any(word in text for word in ("推荐阈值", "默认阈值", "使用默认")):
        return dict(CAPABILITY_DEFAULT_THRESHOLDS.get(capability_code or "") or {"confidence": 0.80})
    values: dict[str, Any] = {}
    if match := re.search(r"(?:超过|持续|大于)\s*(\d+(?:\.\d+)?)\s*分钟", text):
        values["duration_seconds"] = int(float(match.group(1)) * 60)
    elif match := re.search(r"(?:超过|持续|大于)\s*(\d+)\s*秒", text):
        values["duration_seconds"] = int(match.group(1))
    if match := re.search(r"置信度\s*(?:超过|大于|不低于|为)?\s*(\d{1,3})\s*%", text):
        values["confidence"] = min(int(match.group(1)), 100) / 100
    if match := re.search(r"(?:人数|人员)\s*(?:超过|大于|不少于)\s*(\d+)", text):
        values["person_count"] = int(match.group(1))
    return values or None


def parse_roi(text: str) -> dict | None:
    if any(word in text for word in ("全画面", "全屏", "整个画面", "不限定区域")):
        return {"mode": "FULL_FRAME", "label": "全画面", "polygon": None, "calibration_required": False}
    if match := re.search(r"([\u4e00-\u9fa5A-Za-z0-9_-]{1,16}(?:区域|区域内|范围内))", text):
        label = match.group(1).removesuffix("内")
        return {"mode": "NAMED_REGION", "label": label, "polygon": None, "calibration_required": True}
    return None


def resolve_cameras(text: str, cameras: list[dict]) -> tuple[list[dict], str | None]:
    if any(word in text for word in ("所有摄像头", "全部摄像头", "所有镜头", "全部镜头")):
        return cameras, None
    matches = [
        camera for camera in cameras
        if camera.get("name") and str(camera["name"]).lower() in text.lower()
    ]
    if not matches:
        return [], "camera_id"
    unique = {item["camera_id"]: item for item in matches}
    return list(unique.values()), None


def _node(node_id: str, kind: str, name: str, runtime: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "runtime": runtime,
        "config": config or {},
    }


def compose_pipeline(goal: str, thresholds: dict | None = None, roi: dict | None = None) -> dict:
    goal_hash = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:10]
    nodes = [
        _node("source", "SOURCE", "摄像头受控拉流", "DeepVision PaaS", {"protocol": "managed"}),
        _node("decode", "DECODE", "视频解码与抽帧", "media.decode", {"strategy": "keyframe_aware", "sample_fps": 2}),
        _node("roi", "PREPROCESS", "标定区域裁剪", "vision.roi", roi or {"mode": "PENDING"}),
    ]

    lower_goal = goal.lower()
    if any(word in goal for word in ("人", "顾客", "员工", "导购", "无人", "离岗", "接待")):
        nodes.append(_node("person", "SMALL_MODEL", "人员检测与跟踪", "atom.person_track", {"model": "person_detector+tracker"}))
    if any(word in goal for word in ("烟", "火", "手机", "工服", "安全帽", "货架", "车辆")):
        nodes.append(_node("object", "SMALL_MODEL", "目标与属性识别", "atom.object_attribute", {"goal": goal[:120]}))
    if is_visual_compliance_request(goal):
        pack = extract_visual_compliance_pack(goal)
        nodes.extend(
            [
                _node("object_pack", "TOOL", "对象包检索与规则展开", "visual.object_pack", {"object_pack": pack}),
                _node("open_vocab", "SMALL_MODEL", "开放词汇对象候选", "atom.open_vocabulary_detect", {"objects": pack["target_objects"] + pack["forbidden_objects"]}),
                _node("logo_ocr", "TOOL", "Logo/OCR/品牌文本识别", "vision.logo_ocr", {"authorized_brands": pack["authorized_brands"]}),
                _node("reference_compare", "TOOL", "参考素材相似度比对", "vision.reference_compare", {"required_assets": pack["reference_assets_required"]}),
                _node("compliance_rule", "DECISION", "视觉合规规则引擎", "rule.visual_compliance", {"rules": pack["rules"]}),
            ]
        )
    if any(word in goal for word in ("无人接待", "长时间", "超过", "持续", "先后", "之后", "期间")):
        nodes.append(_node("temporal", "RULE", "时序与状态聚合", "rule.temporal", thresholds or {"window_seconds": 180}))
    if any(word in lower_goal for word in ("是否", "判断", "行为", "接待", "异常")) or len(nodes) <= 4:
        nodes.append(
            _node(
                "vlm",
                "LARGE_MODEL",
                "视觉大模型复核",
                "vlm.reasoner",
                {"input": ["关键帧", "目标轨迹", "场景上下文"], "output": "结构化判定与理由"},
            )
        )
    nodes.extend(
        [
            _node("decision", "DECISION", "置信度融合与去重", "rule.decision", thresholds or {"confidence": 0.80}),
            _node("output", "OUTPUT", "识别结果与证据输出", "event.emit", {"include": ["result", "confidence", "evidence", "reason"]}),
        ]
    )
    pipeline_name = f"{goal[:24]} Pipeline" if goal.rstrip().endswith("识别") else f"{goal[:24]}识别 Pipeline"
    return {
        "pipeline_id": f"pipeline_{goal_hash}",
        "name": pipeline_name,
        "goal": goal,
        "status": "DRAFT",
        "sop": ["需求输入", "摄像头拉流分析", "模型能力 Pipeline 编排", "输出识别结果"],
        "nodes": nodes,
        "edges": [{"from": nodes[index]["id"], "to": nodes[index + 1]["id"]} for index in range(len(nodes) - 1)],
        "validation_gates": ["视频源可达", "解码策略压测", "原子能力离线评测", "VLM 成本与超时", "1:1 回放验收", "人工审批后发布"],
        "execution_ready": False,
        "blocked_by": ["解码服务未接入", "Pipeline 发布/回执接口未接入"],
    }


def build_capability_plan(
    text: str,
    history: list[dict],
    fields: list[dict],
    cameras: list[dict],
    capability: dict | None,
) -> dict:
    full_text = conversation_text(text, history)
    is_visual_compliance = bool(capability and capability.get("capability_id") == VISUAL_COMPLIANCE_CAPABILITY_ID)
    camera_matches, _ = resolve_cameras(full_text, cameras)
    if is_visual_compliance and not camera_matches:
        camera_matches = cameras
    effective_range = parse_effective_range(full_text)
    thresholds = parse_thresholds(full_text, capability.get("capability_id") if capability else None)
    roi = parse_roi(full_text)
    visual_compliance = extract_visual_compliance_pack(full_text) if is_visual_compliance else None
    if is_visual_compliance:
        thresholds = {
            **CAPABILITY_DEFAULT_THRESHOLDS[VISUAL_COMPLIANCE_CAPABILITY_ID],
            **(thresholds or {}),
            "visual_compliance": visual_compliance,
        }
        roi = roi or {"mode": "FULL_FRAME", "label": "全画面，按对象包规则聚焦推荐点位", "polygon": None, "calibration_required": False}
    missing_slots = []
    if not effective_range:
        missing_slots.append("effective_time_range")
    if not camera_matches:
        missing_slots.append("camera_ids")
    if not thresholds:
        missing_slots.append("thresholds")
    if not roi:
        missing_slots.append("roi")

    if missing_slots:
        status = "NEED_CLARIFICATION"
    elif roi and roi.get("calibration_required") and not roi.get("polygon"):
        status = "NEED_CALIBRATION"
        missing_slots.append("roi_geometry")
    else:
        status = "NEED_INTEGRATION"

    capability_name = capability.get("name") if capability else "待编排能力"
    if is_visual_compliance:
        capability_name = VISUAL_COMPLIANCE_NAME
    field_names = [item["name"] for item in fields]
    return {
        "plan_id": f"online_plan_{hashlib.sha256(full_text.encode('utf-8')).hexdigest()[:12]}",
        "intent": "CREATE_TASK",
        "summary": f"为{'、'.join(field_names)}上线{capability_name}",
        "status": status,
        "risk_level": "HIGH_WRITE",
        "confirm_required": True,
        "idempotency_key": hashlib.sha256((full_text + "|" + "|".join(field_names)).encode("utf-8")).hexdigest(),
        "slots": {
            "org_scope": {"resolved_ids": [item["org_id"] for item in fields], "store_count": len(fields)},
            "capability": capability or {},
            "camera_scope": {
                "resolved_ids": [item["camera_id"] for item in camera_matches],
                "resolved_names": [item["name"] for item in camera_matches],
            },
            "effective_time_range": effective_range,
            "time_range": {"raw": effective_range["label"]} if effective_range else None,
            "thresholds": thresholds,
            "roi": roi,
            "visual_compliance": visual_compliance,
            "missing_slots": missing_slots,
        },
        "validation_result": {
            "warnings": [
                "当前文档未提供有效的线上订阅创建接口，槽位完整后仍需等待接口契约。"
            ] if status == "NEED_INTEGRATION" else [],
        },
        "execution": {
            "tool": "paas.subscription.create",
            "contract_status": "MISSING",
            "executed": False,
        },
    }


def next_slot_question(plan: dict) -> str:
    missing = plan.get("slots", {}).get("missing_slots") or []
    questions = {
        "effective_time_range": "这项巡检从哪天开始、到哪天结束？也可以回复“长期生效”。",
        "camera_ids": "请指定要使用的监控镜头，也可以回复“所有摄像头”。",
        "thresholds": "请设置判断阈值，例如“持续 5 分钟、置信度 80%”，或回复“使用推荐阈值”。",
        "roi": "请说明识别区域，例如“全画面”或“收银台区域”。",
        "roi_geometry": "该区域还需要在摄像头画面上完成多边形标定后才能发布。",
    }
    return questions.get(missing[0], "请继续补充这项巡检任务的执行条件。") if missing else ""
