#!/usr/bin/env python3
"""Visual compliance object-pack helpers.

The helpers keep the new visual-compliance domain parameterized so tenant
specific rules do not leak into the core chat and scheduling flows.
"""

from __future__ import annotations

import re


VISUAL_COMPLIANCE_CAPABILITY_ID = "visual_compliance_inspection"
VISUAL_COMPLIANCE_EVENT_TYPE = "VISUAL_COMPLIANCE"
VISUAL_COMPLIANCE_NAME = "门店视觉合规巡检"

VISUAL_COMPLIANCE_ALIASES = (
    "视觉合规",
    "门店合规",
    "物料合规",
    "合规巡检",
    "品牌露出",
    "竞品露出",
    "竞品Logo",
    "其他品牌Logo",
    "宣传海报",
    "其他品牌海报",
    "统一座椅",
    "立牌",
    "展架",
    "电视广告",
    "其他品牌汽车",
    "其他品牌车",
    "车标",
)

VISUAL_COMPLIANCE_TERMS = tuple(sorted(set(VISUAL_COMPLIANCE_ALIASES + (
    "Logo",
    "logo",
    "LOGO",
    "标准物料",
    "指定物体",
    "指定物料",
    "统一风格",
    "异品牌",
    "非本品牌",
    "广告图片",
    "广告画面",
))))

PHONE_COMPETITOR_BRANDS = ("vivo", "华为", "小米", "荣耀", "苹果", "三星", "一加", "realme", "iQOO")
CAR_COMPETITOR_BRANDS = ("问界", "理想", "蔚来", "小鹏", "特斯拉", "比亚迪", "宝马", "奔驰", "奥迪")


def is_visual_compliance_request(text: str) -> bool:
    return any(term in text for term in VISUAL_COMPLIANCE_TERMS)


def _scene_from_text(text: str, tenant_code: str = "", tenant_name: str = "") -> str:
    scope = f"{tenant_code} {tenant_name} {text}".lower()
    if any(term in scope for term in ("oppo", "手机", "logo", "海报", "宣传")):
        return "mobile_store"
    if any(term in scope for term in ("极狐", "arcfox", "汽车", "展厅", "车标", "车辆")):
        return "auto_showroom"
    return "retail_store"


def _authorized_brand(scene: str, tenant_code: str = "", tenant_name: str = "") -> str:
    scope = f"{tenant_code} {tenant_name}".lower()
    if "oppo" in scope:
        return "OPPO"
    if "arcfox" in scope or "极狐" in tenant_name:
        return "极狐"
    if scene == "mobile_store":
        return tenant_name or tenant_code or "本品牌"
    if scene == "auto_showroom":
        return tenant_name or tenant_code or "本品牌"
    return tenant_name or tenant_code or "本品牌"


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def extract_visual_compliance_pack(text: str, tenant_code: str = "", tenant_name: str = "") -> dict:
    scene = _scene_from_text(text, tenant_code, tenant_name)
    authorized_brand = _authorized_brand(scene, tenant_code, tenant_name)
    target_objects: list[str] = []
    forbidden_objects: list[str] = []
    object_types: list[str] = []
    rules: list[dict] = []
    reference_assets_required: list[str] = []

    wants_other_brand = _has_any(text, ("其他品牌", "竞品", "异品牌", "非本品牌", "别的品牌"))
    wants_logo = _has_any(text, ("Logo", "logo", "LOGO", "标识", "品牌露出"))
    wants_poster = _has_any(text, ("海报", "宣传", "物料"))
    wants_chair = _has_any(text, ("座椅", "椅子", "沙发"))
    wants_stand = _has_any(text, ("立牌", "展架", "易拉宝", "台卡"))
    wants_tv_ad = _has_any(text, ("电视", "大屏", "广告", "播放"))
    wants_other_car = _has_any(text, ("其他品牌汽车", "其他品牌车", "竞品车", "汽车", "车辆", "车标"))

    if scene == "mobile_store" and (wants_other_brand or wants_logo or wants_poster):
        forbidden_objects.extend(["其他品牌Logo", "竞品宣传海报"])
        object_types.extend(["brand_logo", "poster"])
        rules.append(
            {
                "rule_id": "forbid_competitor_brand_material",
                "rule_type": "FORBIDDEN_OBJECT_APPEAR",
                "check_mode": "forbidden",
                "object_type": "brand_logo_or_poster",
                "objects": ["其他品牌Logo", "竞品宣传海报"],
                "authorized_brands": [authorized_brand],
                "forbidden_brand_scope": list(PHONE_COMPETITOR_BRANDS),
                "threshold": 0.80,
                "evidence_policy": {"require_bbox": True, "low_confidence_to_pending": True},
            }
        )

    if wants_chair:
        target_objects.append("统一风格座椅")
        object_types.append("furniture")
        rules.append(
            {
                "rule_id": "require_standard_chair",
                "rule_type": "REQUIRED_OBJECT_PRESENT",
                "check_mode": "required",
                "object_type": "chair",
                "objects": ["统一风格座椅"],
                "threshold": 0.75,
                "evidence_policy": {"require_panorama": True, "low_confidence_to_pending": True},
            }
        )
    if wants_stand:
        target_objects.append("统一立牌展架")
        object_types.append("display_stand")
        rules.append(
            {
                "rule_id": "require_standard_display_stand",
                "rule_type": "REQUIRED_OBJECT_PRESENT",
                "check_mode": "required",
                "object_type": "display_stand",
                "objects": ["统一立牌展架"],
                "threshold": 0.75,
                "evidence_policy": {"require_panorama": True, "low_confidence_to_pending": True},
            }
        )
    if wants_tv_ad:
        target_objects.append("电视或大屏指定广告画面")
        object_types.append("screen_content")
        reference_assets_required.append("指定广告图片或视频关键帧")
        rules.append(
            {
                "rule_id": "require_specified_screen_ad",
                "rule_type": "CONTENT_MATCH",
                "check_mode": "content_match",
                "object_type": "screen",
                "objects": ["指定广告画面"],
                "threshold": 0.80,
                "evidence_policy": {"require_reference_compare": True, "low_confidence_to_pending": True},
            }
        )
    if scene == "auto_showroom" and (wants_other_brand or wants_other_car):
        forbidden_objects.append("其他品牌汽车或车标")
        object_types.extend(["vehicle_brand", "car_logo"])
        rules.append(
            {
                "rule_id": "forbid_other_brand_vehicle",
                "rule_type": "FORBIDDEN_OBJECT_APPEAR",
                "check_mode": "forbidden",
                "object_type": "vehicle_brand",
                "objects": ["其他品牌汽车", "其他品牌车标"],
                "authorized_brands": [authorized_brand],
                "forbidden_brand_scope": list(CAR_COMPETITOR_BRANDS),
                "threshold": 0.80,
                "evidence_policy": {"require_bbox": True, "low_confidence_to_pending": True},
            }
        )

    if not rules:
        forbidden_objects.append("不符合门店视觉合规要求的对象")
        object_types.append("generic_visual_compliance")
        rules.append(
            {
                "rule_id": "generic_visual_compliance",
                "rule_type": "VISUAL_COMPLIANCE_CHECK",
                "check_mode": "mixed",
                "object_type": "generic",
                "objects": ["用户描述的视觉合规目标"],
                "threshold": 0.80,
                "evidence_policy": {"low_confidence_to_pending": True},
            }
        )

    scene_label = {"mobile_store": "手机门店", "auto_showroom": "汽车展厅", "retail_store": "连锁门店"}[scene]
    pack_name = f"{authorized_brand}{scene_label}视觉合规对象包"
    camera_point_preference = []
    if wants_logo or wants_poster:
        camera_point_preference.extend(["门店全景", "墙面", "柜台", "海报区", "入口"])
    if wants_chair:
        camera_point_preference.extend(["接待区", "休息区", "展厅全景"])
    if wants_stand:
        camera_point_preference.extend(["门口", "展厅入口", "主通道"])
    if wants_tv_ad:
        camera_point_preference.extend(["电视", "大屏", "屏幕区域"])
    if wants_other_car:
        camera_point_preference.extend(["展厅全景", "车辆展示区"])
    if not camera_point_preference:
        camera_point_preference = ["门店全景", "主通道", "入口"]

    return {
        "object_pack_id": f"pack_{re.sub(r'[^a-zA-Z0-9]+', '_', authorized_brand.lower()).strip('_') or 'tenant'}_visual_compliance",
        "name": pack_name,
        "version": "draft-1",
        "scene": scene,
        "scene_label": scene_label,
        "authorized_brands": [authorized_brand],
        "target_objects": target_objects,
        "forbidden_objects": forbidden_objects,
        "object_types": sorted(set(object_types)),
        "rules": rules,
        "reference_assets_required": reference_assets_required,
        "camera_point_preference": sorted(set(camera_point_preference), key=camera_point_preference.index),
        "object_pack_update_policy": "APPROVAL_REQUIRED",
        "evidence_policy": {
            "require_original_snapshot": True,
            "require_marked_anomaly_image": True,
            "low_confidence_to_pending": True,
        },
    }


def visual_compliance_goal(text: str, tenant_code: str = "", tenant_name: str = "") -> str:
    pack = extract_visual_compliance_pack(text, tenant_code, tenant_name)
    target = "、".join(pack["target_objects"]) if pack["target_objects"] else "无必须出现对象"
    forbidden = "、".join(pack["forbidden_objects"]) if pack["forbidden_objects"] else "无禁止出现对象"
    return (
        f"按照{pack['name']}检查门店视觉合规：必须出现对象为{target}；"
        f"禁止出现对象为{forbidden}。只依据画面可见证据判断，低置信输出待确认。"
    )


def visual_compliance_prompt_clause(question: str) -> str:
    if not is_visual_compliance_request(question):
        return ""
    return """
视觉合规巡检规则：
1. 只判断用户或对象包中列出的目标，禁止凭门店名、摄像头名、常识或品牌背景补事实。
2. “其他品牌Logo、竞品宣传海报、其他品牌汽车/车标”属于禁止出现对象：画面清晰可见即 status=POSITIVE；确实未见为 NEGATIVE；遮挡、过远、反光、文字不可读为 UNCERTAIN。
3. “统一座椅、立牌展架、指定广告画面”属于必须出现或内容匹配对象：摄像头覆盖区域且明显缺失/不符时 status=POSITIVE；已清晰满足为 NEGATIVE；未覆盖区域或缺少参考素材时输出 UNCERTAIN，不要说未发现异常。
4. Logo/海报判断必须基于可读文字、清晰标志或明确画面内容；普通颜色、装饰、广告位轮廓不能作为品牌证据。
5. observations 写明命中的规则和可见证据；exclusions 写明被排除的正常物体、地贴、反光、背景装饰或无法确认的内容。
"""
