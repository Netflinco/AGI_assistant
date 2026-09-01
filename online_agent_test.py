#!/usr/bin/env python3
"""Contract tests for the online agent without external network access."""

from __future__ import annotations

import json
import time
import online_agent as online_agent_module
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from agent_skills import infer_intent
from online_agent import CN_TZ, DeepVisionPaaSClient, IntentAnalyzer, OnlineAgentError, OnlineInspectionAgent, OpenQuestionResponder, VisualReasoner
from web_search import WebSearchClient


class FakePaaSClient:
    tenant_code = "oppo"

    def organization_tree(self):
        return {
            "tenantCode": "oppo",
            "poiId": "oppo",
            "name": "OPPO",
            "poiType": "GeneralPOI",
            "children": [
                {"tenantCode": "oppo", "poiId": "store-a", "name": "广州门店", "poiType": "FieldPOI", "children": []},
                {"tenantCode": "oppo", "poiId": "store-b", "name": "深圳门店", "poiType": "FieldPOI", "children": []},
            ],
        }

    def cameras(self, poi_id):
        return {
            "total": 1,
            "items": [
                {
                    "fieldId": poi_id,
                    "fieldName": "测试门店",
                    "sensorId": f"camera-{poi_id}",
                    "sensorName": "入口摄像头",
                    "deviceStatus": "online" if poi_id == "store-a" else "offline",
                    "snapshotUrl": "https://signed.example/snapshot.jpg",
                    "userName": "must-not-leak",
                    "password": "must-not-leak",
                    "ipAddress": "10.0.0.1",
                }
            ],
        }

    def configured_capabilities(self, poi_id):
        del poi_id
        return [{"type": "off_duty", "name": "离岗检测"}, {"type": "play_phone", "name": "玩手机检测"}]

    def alarms(self, poi_id, begin_time, end_time, alarm_type=None, camera_id=None, page_index=1, page_size=50):
        del begin_time, end_time, camera_id
        total = 23
        start = (page_index - 1) * page_size
        stop = min(start + page_size, total)
        base_time = datetime(2026, 6, 29, 10, 0, tzinfo=CN_TZ)
        items = []
        for index in range(start, stop):
            items.append(
                {
                    "poiId": poi_id,
                    "alarmId": f"alarm-{poi_id}-{index}",
                    "alarmType": alarm_type or "off_duty",
                    "cameraId": f"camera-{poi_id}",
                    "tick": int((base_time - timedelta(minutes=index)).timestamp() * 1000),
                    "snapshotUrl": "https://signed.example/alarm.jpg",
                    "rects": [{"left": 10, "top": 20, "width": 30, "height": 40}],
                    "extend": json.dumps({"confidence": 0.91, "camera_config": "must-not-leak"}),
                    "llmResult": None,
                }
            )
        return {"success": True, "totalCount": total, "data": items}

    def alarm_detail(self, poi_id, alarm_id):
        result = self.alarms(poi_id, "", "")
        item = result["data"][0]
        item["alarmId"] = alarm_id
        return item

    def start_live_stream(self, poi_id, camera_id, mode=1):
        del poi_id, camera_id, mode
        return {
            "videoToken": "private-video-token",
            "streamId": "live-stream-id",
            "pullStreamUrls": [
                {"type": "flv", "url": "https://media.example/live.flv"},
                {"type": "m3u8", "url": "https://media.example/live.m3u8"},
            ],
        }

    def stop_live_stream(self, poi_id, camera_id, video_token, stream_id):
        assert poi_id and camera_id and video_token and stream_id
        return {"streamId": stream_id}

    def start_playback(self, poi_id, camera_id, start_ms, end_ms):
        assert poi_id and camera_id and start_ms < end_ms
        return {
            "videoToken": "private-playback-token",
            "streamId": "playback-stream-id",
            "pullStreamUrls": [{"type": "m3u8", "url": "https://media.example/playback.m3u8"}],
        }

    def stop_playback(self, poi_id, camera_id, video_token, stream_id):
        assert poi_id and camera_id and video_token and stream_id
        return {"streamId": stream_id}

    def take_snapshot(self, poi_id, camera_id):
        assert poi_id and camera_id
        return {"snapshotUrl": "https://media.example/snapshot.jpg", "snapshotPath": "/snapshot.jpg"}


class NoInventoryPaaSClient(FakePaaSClient):
    def organization_tree(self):
        raise AssertionError("open questions must not access the DeepVision organization inventory")


class DeploymentConfigurationRejectedPaaSClient(FakePaaSClient):
    """Simulates the vendor error observed on configured-capabilities only."""

    def __init__(self):
        self.capability_calls = 0
        self.snapshot_calls = 0

    def configured_capabilities(self, poi_id):
        del poi_id
        self.capability_calls += 1
        raise OnlineAgentError(
            "UPSTREAM_REJECTED",
            "DeepVision 拒绝了本次查询",
            {
                "vendor_code": 400,
                "vendor_message": "内部错误：请联系技术人员配置该产品对应部署形态",
            },
        )

    def take_snapshot(self, poi_id, camera_id):
        self.snapshot_calls += 1
        return super().take_snapshot(poi_id, camera_id)


class FakeVisualReasoner:
    configured = True
    max_images = 8

    def __init__(self):
        self.calls = []

    def analyze(self, question, images):
        self.calls.append({"question": question, "images": images})
        return {
            "status": "NEGATIVE",
            "conclusion": "相关监控画面中未发现明确垃圾，地贴和正常堆放货物已排除。",
            "confidence": 0.91,
            "selected_camera_names": [images[0]["camera_name"]],
            "observations": ["地面通道清晰可见，未见散落废弃物。"],
            "exclusions": ["地贴", "正常堆放货物"],
            "question": question,
            "image_count": len(images),
            "model": "fake-vlm",
            "source": "vlm_online",
        }

    def select_camera(self, question, images):
        self.calls.append({"selection_question": question, "images": images})
        return {"image": images[0], "relevance": 0.96, "reason": "入口区域可见", "model": "fake-vlm"}


class StoreWideObjectSearchPaaSClient(FakePaaSClient):
    def __init__(self, failed_camera_id=None):
        self.failed_camera_id = failed_camera_id
        self.snapshot_camera_ids = []

    def organization_tree(self):
        return {
            "tenantCode": "oppo",
            "poiId": "oppo",
            "name": "OPPO",
            "poiType": "GeneralPOI",
            "children": [
                {
                    "tenantCode": "oppo",
                    "poiId": "store-a",
                    "name": "东莞店",
                    "poiType": "FieldPOI",
                    "children": [],
                }
            ],
        }

    def cameras(self, poi_id):
        assert poi_id == "store-a"
        return {
            "total": 17,
            "items": [
                {
                    "fieldId": poi_id,
                    "fieldName": "东莞店",
                    "sensorId": f"dongguan-camera-{index}",
                    "sensorName": f"展厅{index}",
                    "deviceStatus": "online",
                    "snapshotUrl": f"https://signed.example/dongguan-{index}.jpg",
                }
                for index in range(1, 18)
            ],
        }

    def take_snapshot(self, poi_id, camera_id):
        assert poi_id == "store-a"
        self.snapshot_camera_ids.append(camera_id)
        if camera_id == self.failed_camera_id:
            raise OnlineAgentError("UPSTREAM_UNAVAILABLE", "抓图失败")
        return {
            "snapshotUrl": f"https://media.example/{camera_id}.jpg",
            "snapshotPath": f"/{camera_id}.jpg",
        }


class MisroutedObjectSearchAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "CAPTURE_SNAPSHOT",
            "confidence": 0.95,
            "poi_names": ["东莞店"],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": ["东莞店当前镜头"],
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "explanation": "返回当前镜头并查找黑色沙发。",
            "engine": "llm",
        }


class HallucinatedValidCameraAnalyzer(MisroutedObjectSearchAnalyzer):
    def analyze(self, text, context, orgs, capabilities, history):
        result = super().analyze(text, context, orgs, capabilities, history)
        result.update(
            {
                "intent": "ANALYZE_VISUAL",
                "confidence": 1.0,
                # This camera really exists, but the current utterance did not
                # name it.  It simulates the 2026-08-31 production badcase in
                # which the LLM copied a prior camera from conversation history.
                "camera_names": ["展厅3"],
                "explanation": "",
            }
        )
        return result


class StoreWideObjectSearchReasoner:
    configured = True
    max_images = 4

    def __init__(self, always_negative=False):
        self.always_negative = always_negative
        self.calls = []

    def analyze(self, question, images):
        self.calls.append({"question": question, "images": images})
        matched = None if self.always_negative else next(
            (item for item in images if item["camera_name"] == "展厅17"),
            None,
        )
        return {
            "status": "POSITIVE" if matched else "NEGATIVE",
            "target_observed": bool(matched),
            "business_policy": "OBSERVATION_ONLY",
            "evidence_type": "DIRECT_VISUAL" if matched else "ABSENCE",
            "conclusion": "展厅17发现黑色沙发。" if matched else "本批画面未发现黑色沙发。",
            "confidence": 0.94,
            "selected_camera_names": [matched["camera_name"]] if matched else [],
            "target_evidence": [
                {
                    "camera_name": matched["camera_name"],
                    "subject": "黑色沙发",
                    "target": "黑色沙发",
                    "attributes": {"对象类别": "沙发", "颜色": "黑色"},
                    "constraint_results": [
                        {"constraint": "对象类别", "expected": "沙发", "observed": "沙发", "status": "MATCH"},
                        {"constraint": "颜色", "expected": "黑色", "observed": "黑色", "status": "MATCH"},
                    ],
                    "matches_query": True,
                    "location": "画面中央",
                    "bbox_1000": [320, 260, 760, 820],
                    "confidence": 0.94,
                }
            ] if matched else [],
            "absence_evidence": {
                "coverage": "FULL",
                "inspected_subject_count": len(images),
                "reason": "本批所有画面均已完整检查。",
            } if not matched else {},
            "observations": [],
            "exclusions": [],
            "question": question,
            "image_count": len(images),
            "model": "fake-store-wide-vlm",
            "source": "vlm_online",
        }

    def select_camera(self, question, images):
        raise AssertionError(f"store-wide object search must not select one camera: {question}, {len(images)}")


class LowRelevanceVisualReasoner(FakeVisualReasoner):
    def select_camera(self, question, images):
        self.calls.append({"selection_question": question, "images": images})
        return {"image": images[0], "relevance": 0.0, "reason": "未能匹配用户描述的位置", "model": "fake-vlm"}


class FakeFloorPaaSClient(FakePaaSClient):
    def __init__(self):
        self.snapshot_camera_ids = []

    def cameras(self, poi_id):
        names = [
            "(JK-1) jk-B001-入口方向",
            "(JK-2) jk-B001-垃圾桶区域",
            "(JK-3) jk-B001-扶梯方向",
            "(JK-4) jk-B002-停车区",
        ]
        return {
            "total": len(names),
            "items": [
                {
                    "fieldId": poi_id,
                    "fieldName": "测试门店",
                    "sensorId": f"floor-camera-{index}",
                    "sensorName": name,
                    "deviceStatus": "online",
                    "snapshotUrl": f"https://signed.example/{index}.jpg",
                }
                for index, name in enumerate(names, start=1)
            ],
        }

    def take_snapshot(self, poi_id, camera_id):
        assert poi_id and camera_id
        self.snapshot_camera_ids.append(camera_id)
        return {"snapshotUrl": f"https://media.example/{camera_id}.jpg", "snapshotPath": f"/{camera_id}.jpg"}


class FakeFloorVisualReasoner(FakeVisualReasoner):
    max_images = 2

    def analyze(self, question, images):
        self.calls.append({"question": question, "images": images})
        anomaly_names = [item["camera_name"] for item in images if "垃圾桶" in item["camera_name"]]
        positive = bool(anomaly_names)
        return {
            "status": "POSITIVE" if positive else "NEGATIVE",
            "conclusion": "发现垃圾桶溢满。" if positive else "本批画面未发现垃圾桶溢满。",
            "confidence": 0.94,
            "target_observed": positive,
            "business_policy": "PROHIBITED_CONDITION",
            "selected_camera_names": anomaly_names or [images[0]["camera_name"]],
            "anomaly_camera_names": anomaly_names,
            "observations": ["B1 层点位画面已完成检查。"],
            "exclusions": [],
            "question": question,
            "image_count": len(images),
            "model": "fake-floor-vlm",
            "source": "vlm_online",
        }


class FakePointPaaSClient(FakePaaSClient):
    def cameras(self, poi_id):
        names = [
            "50#JK-196#-BF-三月兽后面通道",
            "57#JK-43#-BF-三月兽门口朝向杂物室方向",
            "JK-2#-BF-汪保来朝西门",
            "216#JK-297#-F1-保安岗亭",
        ]
        return {
            "total": len(names),
            "items": [
                {
                    "fieldId": poi_id,
                    "fieldName": "测试门店",
                    "sensorId": f"point-camera-{index}",
                    "sensorName": name,
                    "deviceStatus": "online",
                    "snapshotUrl": f"https://signed.example/point-{index}.jpg",
                }
                for index, name in enumerate(names, start=1)
            ],
        }


class FakeSupermarketPaaSClient(FakePaaSClient):
    def __init__(self):
        self.snapshot_camera_ids = []

    def cameras(self, poi_id):
        names = [
            "jk-JK-305#-BF-永辉超市门口朝向超市内",
            "jk-JK-306#-BF-永辉超市门口朝向外通道",
            "jk-JK-401#-BF-停车场出口",
        ]
        return {
            "total": len(names),
            "items": [
                {
                    "fieldId": poi_id,
                    "fieldName": "测试门店",
                    "sensorId": f"supermarket-camera-{index}",
                    "sensorName": name,
                    "deviceStatus": "online",
                    "snapshotUrl": f"https://signed.example/supermarket-{index}.jpg",
                }
                for index, name in enumerate(names, start=1)
            ],
        }

    def take_snapshot(self, poi_id, camera_id):
        assert poi_id and camera_id
        self.snapshot_camera_ids.append(camera_id)
        return {
            "snapshotUrl": f"https://media.example/{camera_id}.jpg",
            "snapshotPath": f"/{camera_id}.jpg",
        }


class FakeAfterSalesPaaSClient(FakePaaSClient):
    """A store whose camera inventory omits functional-area labels."""

    def __init__(self):
        self.snapshot_camera_ids = []

    def organization_tree(self):
        return {
            "tenantCode": "oppo",
            "poiId": "oppo",
            "name": "OPPO",
            "poiType": "GeneralPOI",
            "children": [
                {
                    "tenantCode": "oppo",
                    "poiId": "store-a",
                    "name": "广州天河区天河城店",
                    "poiType": "FieldPOI",
                    "children": [],
                }
            ],
        }

    def cameras(self, poi_id):
        names = [f"展厅{index}" for index in range(1, 8)]
        return {
            "total": len(names),
            "items": [
                {
                    "fieldId": poi_id,
                    "fieldName": "广州天河区天河城店",
                    "sensorId": f"after-sales-camera-{index}",
                    "sensorName": name,
                    "pointLabel": "广州天河区天河城店",
                    "deviceStatus": "online",
                    "snapshotUrl": f"https://signed.example/after-sales-{index}.jpg",
                }
                for index, name in enumerate(names, start=1)
            ],
        }

    def take_snapshot(self, poi_id, camera_id):
        assert poi_id and camera_id
        self.snapshot_camera_ids.append(camera_id)
        return {
            "snapshotUrl": f"https://media.example/{camera_id}.jpg",
            "snapshotPath": f"/{camera_id}.jpg",
        }


class FakeAfterSalesVisualReasoner(FakeVisualReasoner):
    def select_camera(self, question, images):
        self.calls.append({"selection_question": question, "images": images})
        selected = next(item for item in images if item["camera_name"] == "展厅5")
        return {
            "image": selected,
            "relevance": 0.93,
            "reason": "画面中可见售后接待台和维修等待区域。",
            "model": "fake-after-sales-vlm",
        }

    def analyze(self, question, images):
        self.calls.append({"question": question, "images": images})
        assert [item["camera_name"] for item in images] == ["展厅5"]
        return {
            "status": "POSITIVE",
            "target_observed": True,
            "business_policy": "OBSERVATION_ONLY",
            "conclusion": "售后区域画面中可见工作人员。",
            "confidence": 0.94,
            "selected_camera_names": ["展厅5"],
            "target_evidence": [
                {
                    "subject": "工作人员",
                    "target": "工作人员",
                    "attributes": {"对象类别": "工作人员"},
                    "constraint_results": [
                        {"constraint": "对象类别", "expected": "工作人员", "observed": "工作人员", "status": "MATCH"},
                    ],
                    "matches_query": True,
                    "location": "售后接待台附近",
                    "bbox_1000": [420, 180, 560, 880],
                    "confidence": 0.94,
                }
            ],
            "observations": ["售后接待台附近可见一名工作人员。"],
            "exclusions": [],
            "question": question,
            "image_count": len(images),
            "model": "fake-after-sales-vlm",
            "source": "vlm_online",
        }


class GenericSupermarketAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "ANALYZE_VISUAL",
            "confidence": 0.95,
            "poi_names": ["超市门口"],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": [],
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "engine": "llm",
        }


class PointAsPoiAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "ANALYZE_VISUAL",
            "confidence": 0.95,
            "poi_names": ["三月兽"],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": [],
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "engine": "llm",
        }


class PointSlotMissingAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "ANALYZE_VISUAL",
            "confidence": 0.9,
            "poi_names": [],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": [],
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "engine": "llm",
        }


class FloorAsPoiAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "ANALYZE_VISUAL",
            "confidence": 0.96,
            "poi_names": ["B1层"],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": [],
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "engine": "llm",
        }


class IncompletePlaybackAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "VIEW_PLAYBACK",
            "confidence": 0.93,
            "poi_names": [],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": [],
            "playback_range": {"start": "2026-06-29T10:00:00+08:00", "end": "2026-06-29T10:10:00+08:00"},
            "limit": 50,
            "engine": "llm",
        }


class SlotDroppingAnalyzer:
    def analyze(self, text, context, orgs, capabilities, history):
        del text, context, orgs, capabilities, history
        return {
            "intent": "COMPOSE_CAPABILITY",
            "confidence": 0.75,
            "poi_names": [],
            "alarm_types": [],
            "camera_status": None,
            "camera_names": [],
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "engine": "llm",
        }


def assert_no_secrets(value):
    serialized = json.dumps(value, ensure_ascii=False)
    for forbidden in ("must-not-leak", "password", "ipAddress", "camera_config"):
        assert forbidden not in serialized, serialized


def assert_http_401_refreshes_token():
    client = DeepVisionPaaSClient("app-key", "app-secret", "oppo", "https://example.invalid")
    client._token = "expired-token"
    client._token_expires_at = time.monotonic() + 3600
    calls = []

    def fake_raw_post(path, body):
        calls.append((path, body.get("token")))
        if path == "/user/center/v1/login/client":
            return {"success": True, "data": {"token": "fresh-token", "expireIn": 7200}}
        if body.get("token") == "expired-token":
            raise OnlineAgentError(
                "UPSTREAM_HTTP_ERROR",
                "DeepVision 在线服务暂时不可用",
                {"http_status": 401, "vendor_code": 401},
            )
        return {"success": True, "data": {"ok": True}}

    client._raw_post = fake_raw_post
    result = client._post("/test", {})
    assert result["data"]["ok"] is True
    assert client._token == "fresh-token"
    assert calls == [
        ("/test", "expired-token"),
        ("/user/center/v1/login/client", None),
        ("/test", "fresh-token"),
    ]


def main():
    assert_http_401_refreshes_token()
    assert infer_intent("我想看下门口的监控画面", False) == "CAPTURE_SNAPSHOT"
    assert infer_intent("看下店门口的摄像头画面", False) == "CAPTURE_SNAPSHOT"
    assert infer_intent("查看当前在线摄像头", False) == "QUERY_CAMERAS"
    assert infer_intent("看看门口监控画面有没有垃圾", False) == "ANALYZE_VISUAL"
    assert infer_intent("帮我看看视频里有没有员工在接待顾客", False) == "ANALYZE_VISUAL"
    assert infer_intent("帮我看下天河城店的售后区域有没有工作人员", False) == "ANALYZE_VISUAL"
    assert infer_intent("帮我看下东莞店当前镜头画面，找一个黑色的沙发", False) == "ANALYZE_VISUAL"
    assert infer_intent("看下当前监控画面，找一个复古邮差包", False) == "ANALYZE_VISUAL"
    assert infer_intent("看下当前镜头画面里有几把椅子", False) == "ANALYZE_VISUAL"
    assert infer_intent("帮我找东莞店当前监控画面", False) == "CAPTURE_SNAPSHOT"
    assert infer_intent("能拍到大门那路视频", False) == "VIEW_LIVE_STREAM"
    assert infer_intent("给我看下门口的监控视频", False) == "VIEW_LIVE_STREAM"
    assert infer_intent("查看昨天10点到10点10分门口录像", False) == "VIEW_PLAYBACK"
    assert infer_intent("查看昨天10点门口的监控画面图像", False) == "CAPTURE_SNAPSHOT"
    assert infer_intent("查看昨天的离岗预警", False) == "QUERY_ALARMS"
    assert infer_intent("近7天当前门店告警统计排行 Top10", False) == "ANALYZE_ALARMS"
    assert infer_intent("查看当前门店上线了哪些应用", False) == "QUERY_SUBSCRIPTIONS"
    assert infer_intent("查看门店服务器和摄像头在线状态", False) == "QUERY_DEVICE_STATUS"
    assert infer_intent("给入口摄像头上线离岗巡检", True) == "CREATE_TASK"
    assert infer_intent("上线顾客进店后无人接待巡检", False) == "COMPOSE_CAPABILITY"
    guarded_intent = IntentAnalyzer()._validate(
        {"intent": "QUERY_CAMERAS", "confidence": 0.6},
        "给我看下门口的监控视频",
        False,
    )
    assert guarded_intent["intent"] == "VIEW_LIVE_STREAM"
    guarded_object_search = IntentAnalyzer()._validate(
        {
            "intent": "CAPTURE_SNAPSHOT",
            "confidence": 0.95,
            "poi_names": ["东莞店"],
            "camera_names": ["东莞店当前镜头"],
        },
        "帮我看下东莞店当前镜头画面，找一个黑色的沙发",
        False,
    )
    assert guarded_object_search["intent"] == "ANALYZE_VISUAL"
    assert VisualReasoner.visual_query_spec(
        "帮我看下东莞店当前镜头画面，找一个黑色的沙发"
    )["requires_ovd_candidate_detection"] is True
    guarded_stats = IntentAnalyzer()._validate(
        {"intent": "QUERY_ALARMS", "confidence": 0.92},
        "近7天当前门店告警统计排行 Top10",
        False,
    )
    assert guarded_stats["intent"] == "ANALYZE_ALARMS"
    normalized = VisualReasoner()._normalize_result(
        "看看地面有没有垃圾",
        [{"camera_name": "入口摄像头", "snapshot_url": "https://signed.example/snapshot.jpg"}],
        {
            "status": "POSITIVE",
            "conclusion": "地面干净，无垃圾。",
            "confidence": 0.95,
            "selected_camera_names": ["入口摄像头"],
            "observations": [],
            "exclusions": ["地贴"],
        },
        0,
    )
    assert normalized["status"] == "NEGATIVE"
    assert normalized["business_policy"] == "PROHIBITED_CONDITION"
    assert normalized["anomaly_camera_names"] == []
    contradicted_clean = VisualReasoner()._normalize_result(
        "检查地面是否存在垃圾或污渍",
        [{"camera_name": "展厅1", "snapshot_url": "https://signed.example/clean.jpg"}],
        {
            "target_observed": True,
            "business_policy": "PROHIBITED_CONDITION",
            "status": "POSITIVE",
            "conclusion": "地面无散落垃圾或污渍，符合清洁标准。",
            "confidence": 0.98,
            "selected_camera_names": ["展厅1"],
            "anomaly_camera_names": ["展厅1"],
        },
        0,
    )
    assert contradicted_clean["status"] == "NEGATIVE"
    assert contradicted_clean["target_observed"] is False
    assert contradicted_clean["anomaly_camera_names"] == []
    assert "未观察到禁止出现的目标" in contradicted_clean["business_reason"]
    stain_detected = VisualReasoner()._normalize_result(
        "检查地面是否存在污渍",
        [{"camera_name": "门口摄像头", "snapshot_url": "https://signed.example/stain.jpg"}],
        {
            "target_observed": True,
            "business_policy": "OBSERVATION_ONLY",
            "status": "NEGATIVE",
            "conclusion": "画面中发现地面污渍。",
            "confidence": 0.9,
            "selected_camera_names": ["门口摄像头"],
            "observations": ["地面有明显深色脏污。"],
            "exclusions": ["阴影", "反光"],
        },
        0,
    )
    assert stain_detected["status"] == "POSITIVE"
    assert stain_detected["business_policy"] == "PROHIBITED_CONDITION"
    insufficient_stain = VisualReasoner()._normalize_result(
        "检查地面是否存在污渍",
        [{"camera_name": "门口摄像头", "snapshot_url": "https://signed.example/stain.jpg"}],
        {
            "target_observed": True,
            "evidence_type": "INSUFFICIENT",
            "status": "POSITIVE",
            "conclusion": "未发现地面有污渍或垃圾。",
            "confidence": 0.99,
            "selected_camera_names": ["门口摄像头"],
            "anomaly_camera_names": ["门口摄像头"],
        },
        0,
    )
    assert insufficient_stain["status"] == "UNCERTAIN"
    assert insufficient_stain["anomaly_camera_names"] == []
    assert "证据不足" in insufficient_stain["conclusion"]
    reception_missing = VisualReasoner()._normalize_result(
        "帮我看看视频里有没有员工在接待顾客",
        [{"camera_name": "入口摄像头", "snapshot_url": "https://signed.example/snapshot.jpg"}],
        {
            "target_observed": False,
            "subject_present": True,
            "status": "NEGATIVE",
            "conclusion": "视频中未发现员工在接待顾客。",
            "confidence": 0.98,
            "selected_camera_names": ["入口摄像头"],
            "observations": [],
            "exclusions": [],
        },
        0,
    )
    assert reception_missing["status"] == "POSITIVE"
    assert reception_missing["business_policy"] == "REQUIRED_BEHAVIOR"
    assert reception_missing["target_observed"] is False
    assert reception_missing["anomaly_camera_names"] == ["入口摄像头"]
    assert "判定为异常" in reception_missing["conclusion"]
    water_missing = VisualReasoner()._normalize_result(
        "看看员工有没有给顾客倒水",
        [{"camera_name": "服务区摄像头", "snapshot_url": "https://signed.example/snapshot.jpg"}],
        {
            "business_policy": "REQUIRED_BEHAVIOR",
            "subject_present": True,
            "target_observed": False,
            "status": "NEGATIVE",
            "conclusion": "画面中有顾客在场，但未观察到员工给顾客倒水。",
            "confidence": 0.92,
            "selected_camera_names": ["服务区摄像头"],
            "observations": ["顾客位于服务台前"],
            "exclusions": [],
        },
        0,
    )
    assert water_missing["status"] == "POSITIVE"
    assert water_missing["business_policy"] == "REQUIRED_BEHAVIOR"
    assert water_missing["subject_present"] is True
    assert water_missing["applicability"] == "APPLICABLE"
    assert water_missing["anomaly_camera_names"] == ["服务区摄像头"]
    water_unknown = VisualReasoner().apply_business_policy(
        "看看员工有没有给顾客倒水",
        {
            "business_policy": "REQUIRED_BEHAVIOR",
            "subject_present": None,
            "target_observed": False,
            "status": "NEGATIVE",
            "conclusion": "未观察到倒水行为，无法确认顾客是否在场。",
        },
    )
    assert water_unknown["status"] == "UNCERTAIN"
    assert water_unknown["applicability"] == "UNKNOWN"
    assert "不能判定为正常" in water_unknown["business_reason"]
    water_not_applicable = VisualReasoner().apply_business_policy(
        "看看员工有没有给顾客倒水",
        {
            "business_policy": "REQUIRED_BEHAVIOR",
            "subject_present": False,
            "target_observed": False,
            "status": "POSITIVE",
            "conclusion": "画面中没有顾客，也未观察到倒水行为。",
        },
    )
    assert water_not_applicable["status"] == "NEGATIVE"
    assert water_not_applicable["applicability"] == "NOT_APPLICABLE"
    water_cup_outcome = VisualReasoner().apply_business_policy(
        "看看员工有没有给顾客倒水",
        {
            "business_policy": "REQUIRED_BEHAVIOR",
            "subject_present": True,
            "target_observed": False,
            "evidence_type": "SERVICE_OUTCOME",
            "status": "POSITIVE",
            "conclusion": "顾客手中可见水杯，未捕捉到瞬时倒水动作。",
        },
    )
    assert water_cup_outcome["target_observed"] is True
    assert water_cup_outcome["status"] == "NEGATIVE"
    assert water_cup_outcome["evidence_type"] == "SERVICE_OUTCOME"

    observed_object = VisualReasoner().apply_business_policy(
        "画面中是否有红色背包、透明水瓶、灰色椅子和木桌？",
        {
            # Simulates an incorrect policy label induced by the shared
            # inspection prompt. The query itself is still a fact question.
            "business_policy": "REQUIRED_BEHAVIOR",
            "target_observed": True,
            "evidence_type": "DIRECT_VISUAL",
            "status": "NEGATIVE",
            "conclusion": "画面中存在红色背包、透明水瓶、灰色椅子和木桌。",
            "target_evidence": [
                {
                    "subject": "红色背包",
                    "target": "背包",
                    "attributes": {"对象类别": "背包", "颜色": "红色"},
                    "constraint_results": [
                        {"constraint": "对象类别", "expected": "背包", "observed": "背包", "status": "MATCH"},
                        {"constraint": "颜色", "expected": "红色", "observed": "红色", "status": "MATCH"},
                    ],
                    "matches_query": True,
                    "location": "画面左下角",
                    "confidence": 0.92,
                }
            ],
        },
    )
    assert observed_object["business_policy"] == "OBSERVATION_ONLY"
    assert observed_object["target_observed"] is True
    assert observed_object["status"] == "POSITIVE"

    web_search_requests = []

    def public_search_fetcher(req, timeout):
        del timeout
        web_search_requests.append(req)
        if req.full_url == "https://api.tavily.com/usage":
            return json.dumps({"key": {"usage": 100, "limit": 1000}}).encode("utf-8")
        return json.dumps(
            {
                "request_id": "search-contract-1",
                "results": [
                    {
                        "title": "公开来源",
                        "url": "https://example.gov.cn/weather?tracking=remove",
                        "content": "公开来源摘要。",
                        "published_date": "2026-08-03",
                    }
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    public_search = WebSearchClient(
        {"provider": "tavily", "api_key": "test-public-search-key"},
        fetcher=public_search_fetcher,
    )
    open_analyzer = IntentAnalyzer()
    fixed_open_now = datetime(2026, 8, 4, 16, 36, 52, tzinfo=CN_TZ)
    open_responder = OpenQuestionResponder(open_analyzer, public_search, clock=lambda: fixed_open_now)
    open_agent = OnlineInspectionAgent(
        NoInventoryPaaSClient(),
        open_analyzer,
        open_responder=open_responder,
    )
    weather = open_agent.handle_message("今天的天气如何？", {"org_id": "store-a"}, [])
    assert weather["intent"] == "OPEN_QA"
    assert weather["agent"]["mode"] == "OPEN_QA"
    assert weather["agent"]["tool_calls"] == ["web.search"]
    assert weather["agent"]["decision"]["allowed_tools"] == ["web.search"]
    assert weather["agent"]["decision"]["evidence_state"] == "WEB_SEARCHED"
    assert weather["agent"]["data_source"] == "public_web"
    assert weather["web_search"]["citations"][0]["url"] == "https://example.gov.cn/weather"
    assert weather["web_search"]["account_usage"]["remaining_credits"] == 900
    assert weather["web_search"]["freshness"] == "day"
    assert weather["web_search"]["topic"] == "general"
    assert weather["web_search"]["temporal_context"] == {
        "reference_time": "2026-08-04T16:36:52+08:00",
        "target_date": "2026-08-04",
        "timezone": "Asia/Shanghai",
        "scope": "weather",
        "query_rewrite": "WEATHER_CANONICAL",
        "weather_location": None,
    }
    weather_payload = json.loads(web_search_requests[0].data.decode("utf-8"))
    assert "2026年8月4日" in weather_payload["query"]
    assert weather_payload["query"] == "2026年8月4日 天气 实况 预报"
    assert weather_payload["topic"] == "general"
    assert weather_payload["time_range"] == "day"
    model_messages = open_responder._model_messages(
        "今天的天气如何？",
        weather["web_search"]["citations"],
        weather["web_search"]["temporal_context"],
    )
    assert "当前北京时间为 2026-08-04 16:36:52" in model_messages[0]["content"]
    assert "目标日期是 2026-08-04" in model_messages[0]["content"]
    assert "当前北京时间 2026-08-04T16:36:52+08:00" in model_messages[1]["content"]
    assert OpenQuestionResponder._has_conflicting_current_date(
        "当前日期为2025-11-17，以下预报可能过时。",
        weather["web_search"]["temporal_context"],
    ) is True
    tomorrow_query, tomorrow_context = open_responder._rewrite_search_query(
        "明天杭州天气如何？",
        OpenQuestionResponder.classify("明天杭州天气如何？"),
    )
    assert "2026年8月5日" in tomorrow_query
    assert tomorrow_context["target_date"] == "2026-08-05"
    incident_query, incident_context = open_responder._rewrite_search_query(
        "今天的杭州天气怎么养",
        OpenQuestionResponder.classify("今天的杭州天气怎么养"),
    )
    assert incident_query == "2026年8月4日 杭州天气 实况 预报"
    assert incident_context["query_rewrite"] == "WEATHER_CANONICAL"
    assert incident_context["weather_location"] == "杭州"
    assert "怎么养" not in incident_query
    # The rewrite is strictly limited to public weather. Store-inspection text
    # remains outside OPEN_QA and keeps the existing visual-intent route.
    assert OpenQuestionResponder.classify("帮我看下门店镜头下是否存在地面垃圾") is None
    assert infer_intent("帮我看下门店镜头下是否存在地面垃圾", False) == "ANALYZE_VISUAL"
    assert OpenQuestionResponder.classify("帮我看下天河城店的售后区域有没有工作人员") is None
    assert len(web_search_requests) == 2
    assert OpenQuestionResponder._has_conflicting_current_date(
        "当前日期为2026-08-04，以下为当天信息。",
        weather["web_search"]["temporal_context"],
    ) is False
    guard_analyzer = IntentAnalyzer({"api_key": "temporal-guard-test", "model": "test-model"})
    guard_responder = OpenQuestionResponder(guard_analyzer, public_search, clock=lambda: fixed_open_now)
    guard_responder._call_model = lambda *_args, **_kwargs: "当前日期为2025-11-17，杭州天气晴到多云。"
    guarded_weather = guard_responder.respond("今天杭州天气如何？")
    assert guarded_weather["state"] == "NO_RELIABLE_SOURCE"
    assert guarded_weather["engine"] == "web_search_temporal_guard"
    assert "2025-11-17" not in guarded_weather["content"]
    assert "2026-08-04" in guarded_weather["content"]
    open_writing = open_agent.handle_message("帮我写一段新品欢迎语", {}, [])
    assert open_writing["intent"] == "OPEN_QA"
    assert open_writing["agent"]["tool_calls"] == []
    open_fact = open_agent.handle_message("美国总统是谁", {}, [])
    assert open_fact["intent"] == "OPEN_QA"
    assert open_fact["agent"]["decision"]["evidence_state"] == "WEB_SEARCHED"
    assert open_fact["agent"]["tool_calls"] == ["web.search"]
    company_profile = open_agent.handle_message("深象智能是一家什么样的公司", {}, [])
    assert company_profile["agent"]["decision"]["evidence_state"] == "WEB_SEARCHED"
    assert company_profile["agent"]["tool_calls"] == ["web.search"]
    assert company_profile["web_search"]["status"] == "SUCCEEDED"
    company_payload = json.loads(web_search_requests[-2].data.decode("utf-8"))
    assert company_payload["query"] == "深象智能是一家什么样的公司"
    assert company_payload["topic"] == "general"
    assert "time_range" not in company_payload
    legal_entity_route = OpenQuestionResponder.classify("那浙江深象智能科技有限公司呢？")
    assert legal_entity_route["state"] == "WEB_SEARCH_REQUIRED"
    assert legal_entity_route["capability"] == "PUBLIC_ENTITY_PROFILE"
    historical_fact = open_agent.handle_message("美国第一任总统是谁", {}, [])
    assert historical_fact["intent"] == "OPEN_QA"
    assert historical_fact["agent"]["decision"]["response_strategy"] == "GENERAL_ANSWER"

    vague_travel = OpenQuestionResponder.classify("你能帮我制定旅行计划么？")
    assert vague_travel["state"] == "OPEN_QA", "能力咨询不应消耗检索额度"
    detailed_travel_text = "想去新加坡，差不多6天左右，计划国庆时候出行，帮我做一份攻略吧，记得生成一份PDF文档"
    detailed_travel = OpenQuestionResponder.classify(detailed_travel_text)
    assert detailed_travel["state"] == "WEB_SEARCH_REQUIRED"
    assert detailed_travel["capability"] == "TRAVEL_PLANNING"
    assert detailed_travel["requested_output_format"] == "PDF"
    travel_requests = []

    class EmptyTravelMedia:
        def search(self, _destination, limit=3, search_label=None):
            del limit, search_label
            return []

    class EmptyTravelPlaces:
        def resolve_destination(self, _destination):
            return {}

        def recommendations(self, _destination, _destination_info, _citation_groups, limit=4):
            del limit
            return {"hotels": [], "restaurants": []}

    def travel_search_fetcher(req, _timeout):
        travel_requests.append(req)
        if req.full_url == "https://api.tavily.com/usage":
            return json.dumps({"key": {"usage": 104, "limit": 1000}}).encode("utf-8")
        query = json.loads(req.data.decode("utf-8"))["query"]
        if "recommended hotels" in query:
            return json.dumps(
                {
                    "request_id": "travel-hotels-1",
                    "usage": {"credits": 1},
                    "results": [
                        {
                            "title": "Hotel Mi Rochor Singapore | Official Site",
                            "url": "https://hotel.example.sg/",
                            "content": "Singapore hotel. Address: 89 Short Street, Singapore 188216.",
                            "score": 0.91,
                        }
                    ],
                }
            ).encode("utf-8")
        if "recommended restaurants" in query:
            return json.dumps(
                {
                    "request_id": "travel-restaurants-1",
                    "usage": {"credits": 1},
                    "results": [
                        {
                            "title": "National Kitchen Restaurant Singapore",
                            "url": "https://restaurant.example.sg/",
                            "content": "Local restaurant. Address: 1 St Andrew's Road, Singapore 178957.",
                            "score": 0.9,
                        }
                    ],
                }
            ).encode("utf-8")
        return json.dumps(
            {
                "request_id": "travel-search-1",
                "usage": {"credits": 1},
                "results": [
                    {
                        "title": "新加坡官方入境旅行信息",
                        "url": "https://www.ica.gov.sg/enter-transit-depart/entering-singapore",
                        "content": "Travellers should review current entry requirements before departure.",
                        "published_date": "2026-07-20",
                        "score": 0.93,
                    }
                ],
            }
        ).encode("utf-8")

    travel_responder = OpenQuestionResponder(
        IntentAnalyzer(),
        WebSearchClient({"provider": "tavily", "api_key": "travel-test-key"}, fetcher=travel_search_fetcher),
        clock=lambda: datetime(2026, 8, 5, 11, 0, 0, tzinfo=CN_TZ),
        travel_media_client=EmptyTravelMedia(),
        travel_places_client=EmptyTravelPlaces(),
    )
    travel_result = travel_responder.respond(detailed_travel_text)
    assert travel_result["state"] == "WEB_SEARCHED"
    assert travel_result["requested_output_format"] == "PDF"
    assert travel_result["web_search"]["temporal_context"]["scope"] == "travel"
    assert travel_result["web_search"]["temporal_context"]["travel_year"] == 2026
    assert "新加坡" in travel_result["content"]
    travel_payload = json.loads(travel_requests[0].data.decode("utf-8"))
    assert travel_payload["query"] == (
        "新加坡 2026 China National Day 6 day travel guide "
        "official tourism itinerary attractions neighborhoods public transport"
    )
    assert "include_domains" not in travel_payload
    assert "time_range" not in travel_payload
    assert travel_payload["search_depth"] == "basic"
    assert len([req for req in travel_requests if req.full_url.endswith("/search")]) == 3
    assert travel_result["web_search"]["usage"]["credits"] == 3
    assert travel_result["travel_guide"]["hotels"][0]["address_verified"] is True
    assert travel_result["travel_guide"]["restaurants"][0]["map_url"].startswith("https://www.google.com/maps/")
    assert "住宿候选" in travel_result["content"] and "餐饮候选" in travel_result["content"]
    tokyo_route = OpenQuestionResponder.classify("想去东京玩5天，暑假出行，帮我做旅行攻略")
    assert tokyo_route["state"] == "WEB_SEARCH_REQUIRED"
    tokyo_query, tokyo_context = travel_responder._rewrite_search_query(
        "想去东京玩5天，暑假出行，帮我做旅行攻略",
        tokyo_route,
    )
    assert tokyo_query.startswith("东京 2026 5 day travel guide")
    assert tokyo_context["destination"] == "东京"
    spain_text = "帮我制定一版本详细的西班牙8天旅游攻略，预计1月出发；"
    spain_details = OpenQuestionResponder._travel_plan_details(spain_text)
    assert spain_details == {"destination": "西班牙", "days": 8, "month": 1}
    spain_route = OpenQuestionResponder.classify(spain_text)
    spain_query, spain_context = travel_responder._rewrite_search_query(spain_text, spain_route)
    assert spain_query == (
        "西班牙 2027 January 8 day travel guide "
        "official tourism itinerary attractions neighborhoods public transport"
    )
    assert "出境旅行" not in spain_query
    assert spain_context["destination"] == "西班牙"
    assert spain_context["travel_year"] == 2027
    assert spain_context["travel_month"] == 1
    spaced_pdf_route = OpenQuestionResponder.classify("整理一个p d f", force_open=True)
    assert spaced_pdf_route["requested_output_format"] == "PDF"
    filtered_rome_sources = OpenQuestionResponder._filter_travel_citations(
        "想去罗马玩3天",
        [
            {
                "title": "China Entry & Immigration Guide for Foreign Visitors",
                "snippet": "Entry ports across China for trips to China.",
                "domain": "hellochinatrip.com",
            },
            {
                "title": "Rome official tourism and Italy entry information",
                "snippet": "Travel information for visitors to Rome, Italy.",
                "domain": "turismoroma.it",
            },
        ],
        ["罗马", "Rome", "Italy"],
    )
    assert [item["domain"] for item in filtered_rome_sources] == ["turismoroma.it"]
    assert OpenQuestionResponder._filter_travel_citations(
        spain_text,
        [
            {
                "title": "China Entry & Immigration Guide for Foreign Visitors",
                "snippet": "Entry ports and itineraries across China.",
                "domain": "hellochinatrip.com",
            },
            {
                "title": "Spain official tourism guide",
                "snippet": "Public transport and attractions for an eight-day trip in Spain.",
                "domain": "spain.info",
            },
        ],
        ["西班牙", "Spain"],
    ) == [
        {
            "title": "Spain official tourism guide",
            "snippet": "Public transport and attractions for an eight-day trip in Spain.",
            "domain": "spain.info",
        }
    ]
    assert OpenQuestionResponder._filter_travel_citations(
        "帮我制定8天旅游攻略",
        [{"title": "China travel guide", "snippet": "China itinerary", "domain": "example.com"}],
    ) == []
    assert OpenQuestionResponder._is_travel_source_refusal(
        "由于现有资料无法支持该目的地，我无法基于这些来源为您提供具体行程。"
    )
    assert not OpenQuestionResponder._is_travel_source_refusal(
        "第1天抵达马德里，第2天参观博物馆区，第3天前往巴塞罗那。"
    )
    tokyo_fallback = OpenQuestionResponder._travel_fallback_answer("想去东京玩5天，暑假出行", [])
    assert "东京" in tokyo_fallback and "第 5 天" in tokyo_fallback
    spain_fallback = OpenQuestionResponder._travel_fallback_answer(spain_text, [])
    assert "西班牙、8 天、1月出行" in spain_fallback

    no_source_requests = []

    def no_source_travel_fetcher(req, _timeout):
        no_source_requests.append(req)
        if req.full_url == "https://api.tavily.com/usage":
            return json.dumps({"key": {"usage": 103, "limit": 1000}}).encode("utf-8")
        return json.dumps({"request_id": "travel-no-source", "results": []}).encode("utf-8")

    no_source_travel = OpenQuestionResponder(
        IntentAnalyzer({"api_key": "travel-model-key", "model": "travel-model"}),
        WebSearchClient({"provider": "tavily", "api_key": "travel-search-key"}, fetcher=no_source_travel_fetcher),
        clock=lambda: datetime(2026, 8, 5, 11, 0, 0, tzinfo=CN_TZ),
        travel_media_client=EmptyTravelMedia(),
        travel_places_client=EmptyTravelPlaces(),
    )
    no_source_model_calls = []

    def answer_no_source_travel(text, citations=None, temporal_context=None, history=None):
        no_source_model_calls.append((text, citations, temporal_context, history))
        return """巴黎第1天游览塞纳河沿岸，第2天安排博物馆片区，第3天探索历史街区，第4天机动返程。

**预算参考**
- 住宿：¥1000/晚
---
**PDF文档说明**
目前我无法直接生成或发送PDF文件，请复制粘贴到Word并导出为PDF。
---
卢浮宫（免费，需预约）可作为备选。"""

    no_source_travel._call_model = answer_no_source_travel
    paris_no_source = no_source_travel.respond("想去巴黎玩4天，国庆出行，请生成旅行攻略和PDF文档")
    assert paris_no_source["state"] == "NO_RELIABLE_SOURCE"
    assert paris_no_source["engine"] == "travel_model_without_sources"
    assert paris_no_source["requested_output_format"] == "PDF"
    assert "塞纳河" in paris_no_source["content"]
    assert "当前未获得可核验的公开来源" in paris_no_source["content"]
    assert "**预算与预订**" in paris_no_source["content"]
    assert "¥1000" not in paris_no_source["content"]
    assert "无法直接生成" not in paris_no_source["content"]
    assert "PDF文档说明" not in paris_no_source["content"]
    assert "免费" not in paris_no_source["content"]
    assert "需预约" not in paris_no_source["content"]
    assert no_source_model_calls[0][1] == []
    assert no_source_model_calls[0][2]["scope"] == "travel"
    assert len([req for req in no_source_requests if req.full_url.endswith("/search")]) == 3

    dirty_sourced_travel = """### **第1天：抵达马德里**
- **签证**：中国公民需提前申请申根签证。
- **下午**：参观普拉多博物馆（需提前预约，冬季10:00-19:00）。
- **晚餐推荐：Fake Restaurant**
> 当地气温3-8°C，酒店折扣约15%。
### **预算参考**
- 住宿：€100/晚
---
### **第8天：返程**
- 根据航班时间前往机场，预留值机与退税时间。"""
    clean_sourced_travel = OpenQuestionResponder._sanitize_unverified_travel_answer(
        dirty_sourced_travel,
        has_sources=True,
    )
    assert "第1天" in clean_sourced_travel and "第8天" in clean_sourced_travel
    assert "已获得部分目的地公开来源" in clean_sourced_travel
    assert "申根" not in clean_sourced_travel
    assert "10:00" not in clean_sourced_travel
    assert "Fake Restaurant" not in clean_sourced_travel
    assert "15%" not in clean_sourced_travel
    assert "€100" not in clean_sourced_travel
    assert "在当日游览片区选择本地餐厅" in clean_sourced_travel

    safe_history = [
        {"sender": "user", "content": "你能帮我制定旅行计划么？"},
        {
            "sender": "assistant",
            "content": "请告诉我目的地、时间和天数。",
            "linked_object": {"source": "open_qa", "agent": {"mode": "OPEN_QA"}},
        },
    ]
    contextual_messages = travel_responder._model_messages(detailed_travel_text, history=safe_history)
    assert [item["role"] for item in contextual_messages] == ["system", "user", "assistant", "user"]
    assert contextual_messages[1]["content"] == "你能帮我制定旅行计划么？"
    inspection_history = [
        {"sender": "user", "content": "检查门店摄像头"},
        {
            "sender": "assistant",
            "content": "已完成门店巡检。",
            "linked_object": {"source": "deepvision_online", "agent": {"mode": "INSPECTION"}},
        },
    ]
    isolated_messages = travel_responder._model_messages("帮我写旅行建议", history=inspection_history)
    assert len(isolated_messages) == 2
    assert "检查门店摄像头" not in json.dumps(isolated_messages, ensure_ascii=False)
    assert "已完成门店巡检" not in json.dumps(isolated_messages, ensure_ascii=False)

    configured_fallback = OpenQuestionResponder(
        IntentAnalyzer({"api_key": "configured-test-key", "model": "test-model"})
    )

    def fail_open_model(*_args, **_kwargs):
        raise OnlineAgentError("LLM_UNAVAILABLE", "timeout", {"attempts": 2})

    configured_fallback._call_model = fail_open_model
    failed_open_answer = configured_fallback.respond("帮我写一段活动欢迎语")
    assert failed_open_answer["engine"] == "open_qa_model_fallback"
    assert failed_open_answer["model_failure"]["attempts"] == 2
    assert "未配置" not in failed_open_answer["content"]

    retry_responder = OpenQuestionResponder(IntentAnalyzer({"api_key": "retry-key", "model": "retry-model"}))
    retry_calls = []

    class RetryResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "重试后成功"}}]}).encode("utf-8")

    original_urlopen = online_agent_module.request.urlopen

    def retry_urlopen(_req, timeout):
        retry_calls.append(timeout)
        if len(retry_calls) == 1:
            raise online_agent_module.error.URLError("simulated connection reset")
        return RetryResponse()

    try:
        online_agent_module.request.urlopen = retry_urlopen
        assert retry_responder._call_model("测试重试") == "重试后成功"
    finally:
        online_agent_module.request.urlopen = original_urlopen
    assert retry_calls == [28, 32]

    travel_timeout_calls = []

    def travel_timeout_urlopen(req, timeout):
        travel_timeout_calls.append((json.loads(req.data.decode("utf-8")), timeout))
        return RetryResponse()

    try:
        online_agent_module.request.urlopen = travel_timeout_urlopen
        assert retry_responder._call_model(
            "想去罗马玩3天",
            citations=[],
            temporal_context={"scope": "travel", "travel_year": 2026, "reference_time": "2026-08-05T12:00:00+08:00"},
        ) == "重试后成功"
    finally:
        online_agent_module.request.urlopen = original_urlopen
    assert travel_timeout_calls[0][0]["max_tokens"] == 1480
    assert travel_timeout_calls[0][1] == 55

    empty_search = WebSearchClient(
        {"provider": "tavily", "api_key": "test-empty-search-key"},
        fetcher=lambda req, _timeout: (
            json.dumps({"key": {"usage": 101, "limit": 1000}}).encode("utf-8")
            if req.full_url == "https://api.tavily.com/usage"
            else json.dumps({"request_id": "empty-search-1", "results": []}).encode("utf-8")
        ),
    )
    empty_company = OpenQuestionResponder(open_analyzer, empty_search).respond("那浙江深象智能科技有限公司呢？")
    assert empty_company["state"] == "NO_RELIABLE_SOURCE"
    assert empty_company["tool_call"] == "web.search"
    assert empty_company["web_search"]["status"] == "NO_RESULTS"
    assert empty_company["web_search"]["citations"] == []

    def failing_search_fetcher(_req, _timeout):
        raise OSError("simulated provider outage")

    failed_company = OpenQuestionResponder(
        open_analyzer,
        WebSearchClient({"provider": "tavily", "api_key": "test-failed-search-key"}, fetcher=failing_search_fetcher),
    ).respond("那浙江深象智能科技有限公司呢？")
    assert failed_company["state"] == "CAPABILITY_UNAVAILABLE"
    assert failed_company["tool_call"] == "web.search:failed"
    assert failed_company["web_search"]["status"] == "FAILED"
    assert failed_company["web_search"]["error_code"] == "WEB_SEARCH_UNAVAILABLE"
    forced_open = open_agent.handle_message(
        "查看广州门店摄像头",
        {"org_id": "store-a", "mode_override": "OPEN_QA"},
        [],
    )
    assert forced_open["intent"] == "OPEN_QA"
    assert forced_open["agent"]["decision"]["mode_selection"] == "OPEN_QA"
    assert forced_open["agent"]["tool_calls"] == []
    policy_blocked = open_agent.handle_message("今天门店天气如何", {"mode_override": "OPEN_QA"}, [])
    assert policy_blocked["agent"]["decision"]["evidence_state"] == "POLICY_BLOCKED"
    assert policy_blocked["agent"]["tool_calls"] == ["web.search:blocked"]

    unconfigured_open = OnlineInspectionAgent(NoInventoryPaaSClient(), IntentAnalyzer())
    unavailable = unconfigured_open.handle_message("今天的天气如何？", {}, [])
    assert unavailable["agent"]["tool_calls"] == ["web.search:unavailable"]
    assert unavailable["agent"]["decision"]["evidence_state"] == "CAPABILITY_UNAVAILABLE"

    agent = OnlineInspectionAgent(FakePaaSClient(), IntentAnalyzer())
    bootstrap = agent.bootstrap({"user_id": "u_admin", "name": "demo", "role": "tenant_admin"})
    assert bootstrap["integration"]["mode"] == "deepvision_online"
    assert bootstrap["integration"]["read_only"] is True
    assert len([item for item in bootstrap["orgs"] if item["org_type"] == "store"]) == 2
    assert len(bootstrap["cameras"]) == 2
    assert_no_secrets(bootstrap)

    coverage_client = StoreWideObjectSearchPaaSClient()
    coverage_reasoner = StoreWideObjectSearchReasoner()
    coverage_result = OnlineInspectionAgent(
        coverage_client,
        MisroutedObjectSearchAnalyzer(),
        coverage_reasoner,
    ).handle_message(
        "帮我看下东莞店当前镜头画面，找一个黑色的沙发",
        {"org_id": "store-a"},
        [],
    )
    assert coverage_result["intent"] == "ANALYZE_VISUAL"
    assert coverage_result["agent"]["analysis"]["intent_guard"] == {
        "from": "CAPTURE_SNAPSHOT",
        "to": "ANALYZE_VISUAL",
        "reason": "VISUAL_PREDICATE_REQUIRES_REASONING",
    }
    assert coverage_result["agent"]["analysis"]["camera_names"] == []
    assert coverage_result["agent"]["tool_calls"] == [
        "paas.camera.page",
        "paas.media.snapshot",
        "vlm.image.inspect",
    ]
    assert len(coverage_client.snapshot_camera_ids) == 17
    assert len(coverage_reasoner.calls) == 5
    assert sum(len(call["images"]) for call in coverage_reasoner.calls) == 17
    assert coverage_result["visual_result"]["status"] == "POSITIVE"
    assert coverage_result["visual_result"]["image_count"] == 17
    assert coverage_result["visual_result"]["batch_count"] == 5
    assert coverage_result["visual_result"]["visual_scope"]["coverage_status"] == "FULL"
    assert coverage_result["visual_result"]["visual_scope"]["eligible_camera_count"] == 17
    assert "17 路可用镜头执行全量检索" in coverage_result["assistant_content"]

    production_badcase_client = StoreWideObjectSearchPaaSClient()
    production_badcase_result = OnlineInspectionAgent(
        production_badcase_client,
        HallucinatedValidCameraAnalyzer(),
        StoreWideObjectSearchReasoner(),
    ).handle_message(
        "帮我看下东莞店当前镜头画面，找一个黑色的沙发",
        {"org_id": "store-a"},
        [
            {"sender": "user", "content": "上一轮查看展厅3"},
            {
                "sender": "assistant",
                "content": "这是展厅3的画面。",
                "linked_object": {
                    "visual_context": {
                        "images": [
                            {
                                "camera_id": "dongguan-camera-3",
                                "camera_name": "展厅3",
                                "org_id": "store-a",
                            }
                        ]
                    }
                },
            },
        ],
    )
    assert production_badcase_result["intent"] == "ANALYZE_VISUAL"
    assert production_badcase_result["agent"]["analysis"]["camera_names"] == []
    assert len(production_badcase_client.snapshot_camera_ids) == 17
    assert production_badcase_result["visual_result"]["image_count"] == 17
    assert production_badcase_result["visual_result"]["visual_scope"]["coverage_status"] == "FULL"

    grounded_camera_client = StoreWideObjectSearchPaaSClient()
    grounded_camera_result = OnlineInspectionAgent(
        grounded_camera_client,
        HallucinatedValidCameraAnalyzer(),
        StoreWideObjectSearchReasoner(always_negative=True),
    ).handle_message(
        "帮我看下东莞店展厅3当前画面，找一个黑色的沙发",
        {"org_id": "store-a"},
        [],
    )
    assert grounded_camera_result["agent"]["analysis"]["camera_names"] == ["展厅3"]
    assert grounded_camera_client.snapshot_camera_ids == ["dongguan-camera-3"]
    assert grounded_camera_result["visual_result"]["image_count"] == 1

    multi_camera_client = StoreWideObjectSearchPaaSClient()
    multi_camera_result = OnlineInspectionAgent(
        multi_camera_client,
        HallucinatedValidCameraAnalyzer(),
        StoreWideObjectSearchReasoner(always_negative=True),
    ).handle_message(
        "帮我检查东莞店展厅10和展厅11当前画面中是否有黑色的沙发",
        {"org_id": "store-a"},
        [],
    )
    assert multi_camera_client.snapshot_camera_ids == ["dongguan-camera-10", "dongguan-camera-11"]
    assert multi_camera_result["visual_result"]["image_count"] == 2
    assert multi_camera_result["visual_result"]["visual_scope"]["matched_camera_names"] == ["展厅10", "展厅11"]

    full_negative_result = OnlineInspectionAgent(
        StoreWideObjectSearchPaaSClient(),
        MisroutedObjectSearchAnalyzer(),
        StoreWideObjectSearchReasoner(always_negative=True),
    ).handle_message(
        "帮我看下东莞店当前镜头画面，找一个黑色的沙发",
        {"org_id": "store-a"},
        [],
    )
    assert full_negative_result["visual_result"]["status"] == "NEGATIVE"
    assert full_negative_result["visual_result"]["absence_evidence"] == {
        "coverage": "FULL",
        "inspected_subject_count": 17,
        "reason": "全部 5 个分批均完成全画面排除核验。",
    }

    partial_client = StoreWideObjectSearchPaaSClient(failed_camera_id="dongguan-camera-17")
    partial_result = OnlineInspectionAgent(
        partial_client,
        MisroutedObjectSearchAnalyzer(),
        StoreWideObjectSearchReasoner(always_negative=True),
    ).handle_message(
        "帮我看下东莞店当前镜头画面，找一个黑色的沙发",
        {"org_id": "store-a"},
        [],
    )
    assert partial_result["visual_result"]["status"] == "UNCERTAIN"
    assert partial_result["visual_result"]["visual_scope"]["coverage_status"] == "PARTIAL"
    assert partial_result["visual_result"]["visual_scope"]["eligible_camera_count"] == 17
    assert partial_result["visual_result"]["visual_scope"]["captured_camera_count"] == 16
    assert "不能得出全范围“未发现”" in partial_result["visual_result"]["conclusion"]

    inspection_mode_lock = agent.handle_message("今天的天气如何？", {"org_id": "store-a", "mode_override": "INSPECTION"}, [])
    assert inspection_mode_lock["intent"] != "OPEN_QA"
    assert "web.search" not in (inspection_mode_lock.get("agent") or {}).get("tool_calls", [])

    camera_result = agent.handle_message("查看广州门店在线摄像头", {"org_id": "store-a"}, [])
    assert camera_result["intent"] == "QUERY_CAMERAS"
    assert len(camera_result["cameras"]) == 1
    assert camera_result["agent"]["tool_calls"] == ["paas.camera.page"]
    assert_no_secrets(camera_result)

    device_status = agent.handle_message("查看广州门店摄像头和服务器状态", {"org_id": "store-a"}, [])
    assert device_status["intent"] == "QUERY_DEVICE_STATUS"
    assert device_status["device_status"]["summary"] == {
        "camera_total": 1,
        "camera_online": 1,
        "camera_offline": 0,
    }
    assert device_status["device_status"]["servers"][0]["status"] == "UNKNOWN"

    live = agent.handle_message("查看广州门店入口摄像头直播", {"org_id": "store-a"}, [])
    assert live["intent"] == "VIEW_LIVE_STREAM"
    assert live["media"]["kind"] == "LIVE"
    assert live["media"]["stream_type"] == "flv"
    assert live["media"]["playback_url"].startswith("/api/media/sessions/")
    assert "media.example" not in live["media"]["playback_url"]
    media_query = parse_qs(urlparse(live["media"]["playback_url"]).query)
    assert media_query["tenant_code"] == ["oppo"]
    proxy_source = agent.media_stream_source(
        live["media"]["session_id"],
        media_query["access_token"][0],
    )
    assert proxy_source["stream_type"] == "flv"
    assert live["media"]["can_stop"] is True
    assert "video_token" not in live["media"]
    stopped = agent.stop_media_session(live["media"]["session_id"])
    assert stopped["status"] == "STOPPED"

    live_rejected = agent.handle_message("查看广州门店入口摄像头直播", {"org_id": "store-a"}, [])

    def reject_stop(*args):
        del args
        raise OnlineAgentError("UPSTREAM_REJECTED", "DeepVision 拒绝了本次查询", {"vendor_code": 4})

    original_stop_live = agent.client.stop_live_stream
    agent.client.stop_live_stream = reject_stop
    released = agent.stop_media_session(live_rejected["media"]["session_id"])
    agent.client.stop_live_stream = original_stop_live
    assert released["status"] == "RELEASED_LOCAL"
    assert released["upstream"] is False

    playback = agent.handle_message("查看昨天 10点到10点10分广州门店入口摄像头录像", {"org_id": "store-a"}, [])
    assert playback["intent"] == "VIEW_PLAYBACK"
    assert playback["media"]["kind"] == "PLAYBACK"
    agent.stop_media_session(playback["media"]["session_id"])

    snapshot = agent.handle_message("获取广州门店入口摄像头现在的画面图像", {"org_id": "store-a"}, [])
    assert snapshot["intent"] == "CAPTURE_SNAPSHOT"
    assert snapshot["media"]["kind"] == "IMAGE"
    assert snapshot["media"]["snapshot_url"].endswith("snapshot.jpg")
    assert snapshot["_visual_context"]["images"][0]["camera_name"] == "入口摄像头"

    blocked_visual = agent.handle_message("看看广州门店门口地面有没有垃圾", {"org_id": "store-a"}, [])
    assert blocked_visual["intent"] == "ANALYZE_VISUAL"
    assert blocked_visual["visual_result"]["status"] == "BLOCKED"
    assert blocked_visual["agent"]["blocked_reason"] == "VLM_NOT_CONFIGURED"
    assert "choices" not in blocked_visual

    fake_visual = FakeVisualReasoner()
    visual_agent = OnlineInspectionAgent(FakePaaSClient(), IntentAnalyzer(), fake_visual)
    view_only = visual_agent.handle_message("看下店门口的摄像头画面", {"org_id": "store-a"}, [])
    assert view_only["intent"] == "CAPTURE_SNAPSHOT"
    assert view_only["media"]["kind"] == "IMAGE"
    assert view_only["agent"]["tool_calls"] == ["paas.camera.page", "paas.media.snapshot", "vlm.camera.select"]
    assert "visual_result" not in view_only
    assert "choices" not in view_only

    live_by_scene = visual_agent.handle_message("给我看下门口的监控视频", {"org_id": "store-a"}, [])
    assert live_by_scene["intent"] == "VIEW_LIVE_STREAM"
    assert live_by_scene["media"]["kind"] == "LIVE"
    assert live_by_scene["agent"]["tool_calls"] == [
        "paas.camera.page",
        "paas.media.snapshot",
        "vlm.camera.select",
        "paas.media.live.start",
    ]
    assert live_by_scene["_visual_context"]["images"][0]["kind"] == "LIVE_CONTEXT"
    live_follow_up = visual_agent.handle_message(
        "帮我看看视频里有没有员工在接待顾客",
        {"org_id": "store-a"},
        [
            {
                "sender": "assistant",
                "content": "已创建临时直播会话。",
                "linked_object": {"visual_context": live_by_scene["_visual_context"]},
            }
        ],
    )
    assert live_follow_up["intent"] == "ANALYZE_VISUAL"
    assert live_follow_up["visual_result"]["image_count"] == 1
    assert live_follow_up["media"]["camera_name"] == live_by_scene["media"]["camera_name"]
    assert live_follow_up["agent"]["tool_calls"] == [
        "conversation.live_context",
        "paas.media.snapshot",
        "vlm.image.inspect",
    ]
    visual_agent.stop_media_session(live_by_scene["media"]["session_id"])

    exact_camera_client = FakeSupermarketPaaSClient()
    exact_camera_agent = OnlineInspectionAgent(exact_camera_client, IntentAnalyzer(), LowRelevanceVisualReasoner())
    exact_camera = exact_camera_agent.handle_message(
        "获取 jk-JK-305#-BF-永辉超市门口朝向超市内 当前监控画面",
        {"org_id": "store-a"},
        [],
    )
    assert exact_camera["intent"] == "CAPTURE_SNAPSHOT"
    assert exact_camera["media"]["camera_name"] == "jk-JK-305#-BF-永辉超市门口朝向超市内"
    assert exact_camera_client.snapshot_camera_ids == ["supermarket-camera-1"]
    assert exact_camera["agent"]["tool_calls"] == ["paas.media.snapshot"]

    missing_explicit_camera = exact_camera_agent.handle_message(
        "获取 jk-JK-305#-BF-周真真门口朝向永辉超市门口 当前监控画面",
        {"org_id": "store-a"},
        [],
    )
    assert missing_explicit_camera["agent"]["status"] == "WAITING_CONFIRM"
    assert missing_explicit_camera["agent"]["blocked_reason"] == "EXPLICIT_CAMERA_NOT_FOUND"
    assert "media" not in missing_explicit_camera
    assert exact_camera_client.snapshot_camera_ids == ["supermarket-camera-1"]

    low_relevance_media = exact_camera_agent.handle_message("看下不存在区域的摄像头画面", {"org_id": "store-a"}, [])
    assert low_relevance_media["agent"]["status"] == "WAITING_CONFIRM"
    assert low_relevance_media["agent"]["blocked_reason"] == "CAMERA_SELECTION_RELEVANCE_TOO_LOW"
    assert "media" not in low_relevance_media

    playback_by_scene = visual_agent.handle_message(
        "查看昨天10点到10点10分门口录像",
        {"org_id": "store-a"},
        [],
    )
    assert playback_by_scene["intent"] == "VIEW_PLAYBACK"
    assert playback_by_scene["media"]["kind"] == "PLAYBACK"
    assert playback_by_scene["agent"]["tool_calls"][-1] == "paas.media.playback.start"
    visual_agent.stop_media_session(playback_by_scene["media"]["session_id"])

    repaired_playback = OnlineInspectionAgent(
        FakePaaSClient(),
        IncompletePlaybackAnalyzer(),
        FakeVisualReasoner(),
    ).handle_message("查看昨天10点到10点10分门口录像", {"org_id": "store-a"}, [])
    assert repaired_playback["intent"] == "VIEW_PLAYBACK"
    assert repaired_playback["media"]["kind"] == "PLAYBACK"

    automatic_visual = visual_agent.handle_message(
        "我想查看门口监控的快照，看看地上有没有垃圾",
        {"org_id": "oppo"},
        [],
    )
    assert automatic_visual["intent"] == "ANALYZE_VISUAL"
    assert automatic_visual["visual_result"]["status"] == "NEGATIVE"
    assert automatic_visual["visual_result"]["image_count"] == 1
    assert automatic_visual["agent"]["tool_calls"] == [
        "paas.camera.page",
        "camera.location.resolve",
        "paas.media.snapshot",
        "vlm.camera.select",
        "vlm.image.inspect",
    ]
    assert "choices" not in automatic_visual

    deployment_client = DeploymentConfigurationRejectedPaaSClient()
    deployment_agent = OnlineInspectionAgent(deployment_client, IntentAnalyzer(), FakeVisualReasoner())
    deployment_visual = deployment_agent.handle_message(
        "帮我看下门店镜头下是否存在垃圾",
        {"org_id": "store-a"},
        [],
    )
    assert deployment_visual["intent"] == "ANALYZE_VISUAL"
    assert deployment_visual["visual_result"]["status"] == "NEGATIVE"
    assert deployment_client.capability_calls == 0
    assert deployment_client.snapshot_calls == 1

    deployment_subscriptions = deployment_agent.handle_message(
        "查看广州门店已经配置了哪些巡检能力",
        {"org_id": "store-a"},
        [],
    )
    assert deployment_subscriptions["intent"] == "QUERY_SUBSCRIPTIONS"
    assert deployment_subscriptions["agent"]["status"] == "BLOCKED"
    assert deployment_subscriptions["partial_errors"]
    assert "部署形态" in deployment_subscriptions["assistant_content"]
    assert deployment_client.capability_calls == 1

    deployment_task = deployment_agent.handle_message(
        "给广州门店入口摄像头上线离岗巡检",
        {"org_id": "store-a"},
        [],
    )
    assert deployment_task["intent"] == "CREATE_TASK"
    assert deployment_task["agent"]["status"] == "BLOCKED"
    assert "没有将它误编排为新能力" in deployment_task["assistant_content"]
    assert deployment_client.capability_calls == 2

    follow_up = visual_agent.handle_message(
        "帮我看下这些地面上有没有垃圾，注意规避地贴和堆放的货物",
        {"org_id": "store-a"},
        [
            {
                "sender": "assistant",
                "content": "已获取当前监控画面。",
                "linked_object": {"visual_context": snapshot["_visual_context"]},
            }
        ],
    )
    assert follow_up["intent"] == "ANALYZE_VISUAL"
    assert follow_up["visual_result"]["status"] == "NEGATIVE"
    assert follow_up["visual_result"]["image_count"] == 1
    assert len(fake_visual.calls) == 7

    floor_client = FakeFloorPaaSClient()
    floor_visual = FakeFloorVisualReasoner()
    floor_agent = OnlineInspectionAgent(floor_client, IntentAnalyzer(), floor_visual)
    floor_result = floor_agent.handle_message(
        "帮我看下B1层摄像头画面中是否存在垃圾桶溢满的情况",
        {"org_id": "store-a"},
        [],
    )
    assert floor_result["intent"] == "ANALYZE_VISUAL"
    assert floor_result["visual_result"]["visual_scope"]["label"] == "B1层"
    assert floor_result["visual_result"]["visual_scope"]["matched_camera_count"] == 3
    assert floor_result["visual_result"]["image_count"] == 3
    assert len(floor_result["media_gallery"]) == 3
    assert len(floor_visual.calls) == 2
    assert all("B001" in item["camera_name"] for item in floor_result["media_gallery"])
    assert all("floor-camera-4" not in call["images"][0]["snapshot_url"] for call in floor_visual.calls)
    assert [item["camera_name"] for item in floor_result["media_gallery"] if item["is_anomalous"]] == [
        "(JK-2) jk-B001-垃圾桶区域"
    ]
    assert "已根据点位名称识别 B1层 摄像头 3 路" in floor_result["assistant_content"]
    bf_floor_matches = OnlineInspectionAgent._filter_cameras_by_floor(
        [
            {"name": "JK-298#-BF-永辉门口", "point_label": "测试门店"},
            {"name": "JK-20#-F1-入口", "point_label": "测试门店"},
            {"name": "JK-21#-B2F-停车区", "point_label": "测试门店"},
        ],
        {"floor_code": "B1"},
    )
    assert [item["name"] for item in bf_floor_matches] == ["JK-298#-BF-永辉门口"]

    misrouted_client = FakeFloorPaaSClient()
    misrouted_visual = FakeFloorVisualReasoner()
    misrouted_floor = OnlineInspectionAgent(misrouted_client, FloorAsPoiAnalyzer(), misrouted_visual).handle_message(
        "帮我看下B1层摄像头画面中是否存在垃圾桶溢满的情况",
        {"org_id": "store-a"},
        [
            {
                "sender": "assistant",
                "content": "已展示其他楼层画面。",
                "linked_object": {
                    "visual_context": {
                        "images": [
                            {
                                "kind": "IMAGE",
                                "camera_id": "floor-camera-4",
                                "camera_name": "(JK-4) jk-B002-停车区",
                                "org_id": "store-a",
                                "org_name": "广州门店",
                                "snapshot_url": "https://media.example/old-b2.jpg",
                            }
                        ]
                    }
                },
            }
        ],
    )
    assert misrouted_floor["intent"] == "ANALYZE_VISUAL"
    assert misrouted_floor["agent"]["tenant_code"] == "oppo"
    assert "camera.floor.resolve" in misrouted_floor["agent"]["tool_calls"]
    assert misrouted_floor["visual_result"]["visual_scope"]["matched_camera_count"] == 3
    assert all("B001" in item["camera_name"] for item in misrouted_floor["media_gallery"])
    assert all("B002" not in item["camera_name"] for item in misrouted_floor["media_gallery"])

    point_visual = FakeVisualReasoner()
    point_agent = OnlineInspectionAgent(FakePointPaaSClient(), PointAsPoiAnalyzer(), point_visual)
    point_result = point_agent.handle_message(
        "帮我看下三月兽店门口的地面有没有污渍垃圾",
        {"org_id": "store-a"},
        [],
    )
    assert "授权组织中找到" not in point_result["assistant_content"]
    assert "camera.location.resolve" in point_result["agent"]["tool_calls"]
    assert point_result["visual_result"]["visual_scope"]["matched_camera_count"] == 2
    assert all("三月兽" in item["camera_name"] for item in point_result["media_gallery"])
    point_history = [
        {"sender": "user", "content": "帮我看下三月兽店门口的地面有没有污渍垃圾"},
        {
            "sender": "assistant",
            "content": point_result["assistant_content"],
            "linked_object": {"visual_context": point_result["_visual_context"]},
        },
    ]
    follow_point = point_agent.handle_message("那汪保来呢？", {"org_id": "store-a"}, point_history)
    assert follow_point["visual_result"]["visual_scope"]["matched_camera_count"] == 1
    assert [item["camera_name"] for item in follow_point["media_gallery"]] == ["JK-2#-BF-汪保来朝西门"]
    assert "地面是否存在污渍或垃圾或杂物" in point_visual.calls[-1]["question"]
    assert "服务对象" not in follow_point["assistant_content"]
    slotless_point = OnlineInspectionAgent(
        FakePointPaaSClient(),
        PointSlotMissingAnalyzer(),
        FakeVisualReasoner(),
    ).handle_message(
        "帮我看下三月兽店门口的地面有没有污渍垃圾",
        {"org_id": "store-a"},
        [],
    )
    assert slotless_point["visual_result"]["visual_scope"]["matched_camera_count"] == 2
    assert all("三月兽" in item["camera_name"] for item in slotless_point["media_gallery"])

    supermarket_client = FakeSupermarketPaaSClient()
    supermarket_visual = FakeVisualReasoner()
    supermarket_agent = OnlineInspectionAgent(
        supermarket_client,
        GenericSupermarketAnalyzer(),
        supermarket_visual,
    )
    unmatched_supermarket = supermarket_agent.handle_message(
        "帮我看下盒马超市门口有没有排队情况",
        {"org_id": "store-a"},
        [],
    )
    assert unmatched_supermarket["agent"]["status"] == "WAITING_CONFIRM"
    assert unmatched_supermarket["agent"]["blocked_reason"] == "CAMERA_LOCATION_CONFIRM_REQUIRED"
    assert unmatched_supermarket["choices"]["kind"] == "CAMERA_LOCATION_DISAMBIGUATION"
    assert unmatched_supermarket["choices"]["requested"] == "盒马超市门口"
    assert [item["label"] for item in unmatched_supermarket["choices"]["locations"]] == ["永辉超市门口"]
    location_choice = unmatched_supermarket["choices"]["locations"][0]
    assert location_choice["rewritten_question"] == "帮我看下永辉超市门口有没有排队情况"
    assert "盒马" not in location_choice["prompt"]
    assert "永辉超市门口" in location_choice["prompt"]
    assert supermarket_client.snapshot_camera_ids == []
    assert supermarket_visual.calls == []

    confirmed_supermarket = supermarket_agent.handle_message(
        "确认使用“永辉超市门口”继续检索",
        {"org_id": "store-a"},
        [
            {"sender": "user", "content": "帮我看下盒马超市门口有没有排队情况"},
            {
                "sender": "assistant",
                "content": unmatched_supermarket["assistant_content"],
                "linked_object": {"artifact": {"choices": unmatched_supermarket["choices"]}},
            },
        ],
    )
    assert confirmed_supermarket["visual_result"]["visual_scope"]["label"] == "永辉超市门口"
    assert confirmed_supermarket["visual_result"]["visual_scope"]["matched_camera_count"] == 2
    assert confirmed_supermarket["visual_result"]["image_count"] == 2
    assert len(supermarket_client.snapshot_camera_ids) == 2
    assert supermarket_visual.calls[-1]["question"] == "帮我看下永辉超市门口有没有排队情况"
    assert "盒马" not in supermarket_visual.calls[-1]["question"]
    assert confirmed_supermarket["visual_result"]["question"] == "帮我看下永辉超市门口有没有排队情况"
    assert confirmed_supermarket["visual_result"]["visual_scope"]["rewritten_question"] == (
        "帮我看下永辉超市门口有没有排队情况"
    )
    assert "盒马" not in confirmed_supermarket["assistant_content"]

    after_sales_client = FakeAfterSalesPaaSClient()
    after_sales_visual = FakeAfterSalesVisualReasoner()
    after_sales_result = OnlineInspectionAgent(
        after_sales_client,
        IntentAnalyzer(),
        after_sales_visual,
    ).handle_message(
        "帮我看下天河城店的售后区域有没有工作人员",
        {"org_id": "store-a"},
        [],
    )
    assert after_sales_result["intent"] == "ANALYZE_VISUAL"
    assert after_sales_result["agent"]["status"] == "SUCCEEDED"
    assert after_sales_result["agent"]["tool_calls"] == [
        "paas.camera.page",
        "camera.location.resolve",
        "paas.media.snapshot",
        "vlm.camera.select",
        "vlm.image.inspect",
    ]
    after_sales_scope = after_sales_result["visual_result"]["visual_scope"]
    assert after_sales_scope["label"] == "售后区域"
    assert after_sales_scope["matching_basis"] == "候选快照的 VLM 语义点位匹配"
    assert after_sales_scope["candidate_camera_count"] == 7
    assert after_sales_scope["captured_camera_count"] == 7
    assert after_sales_scope["matched_camera_names"] == ["展厅5"]
    assert after_sales_result["visual_result"]["business_policy"] == "OBSERVATION_ONLY"
    assert after_sales_result["visual_result"]["target_observed"] is True
    assert [item["camera_name"] for item in after_sales_result["media_gallery"]] == ["展厅5"]
    assert len(after_sales_client.snapshot_camera_ids) == 7
    assert "售后区域画面中可见工作人员" in after_sales_result["assistant_content"]

    entrance_coverage_client = FakeAfterSalesPaaSClient()
    entrance_coverage_visual = LowRelevanceVisualReasoner()
    entrance_coverage_result = OnlineInspectionAgent(
        entrance_coverage_client,
        IntentAnalyzer(),
        entrance_coverage_visual,
    ).handle_message(
        "再帮我看店门口有没有员工未在岗",
        {"org_id": "store-a"},
        [],
    )
    assert OnlineInspectionAgent._requested_camera_location("再帮我看店门口有没有员工未在岗") == "店门口"
    assert entrance_coverage_result["intent"] == "ANALYZE_VISUAL"
    assert entrance_coverage_result["agent"]["status"] == "SUCCEEDED"
    assert entrance_coverage_result["agent"]["blocked_reason"] is None
    assert entrance_coverage_result["agent"]["tool_calls"] == [
        "paas.camera.page",
        "camera.location.resolve",
        "paas.media.snapshot",
        "vlm.camera.select",
    ]
    assert entrance_coverage_result["visual_result"]["status"] == "NOT_COVERED"
    assert entrance_coverage_result["visual_result"]["source"] == "camera_coverage_check"
    assert entrance_coverage_result["visual_result"]["image_count"] == 0
    assert entrance_coverage_result["visual_result"]["visual_scope"]["coverage_status"] == "NOT_COVERED"
    assert len(entrance_coverage_client.snapshot_camera_ids) == 7
    assert all("question" not in call for call in entrance_coverage_visual.calls)
    assert "店门口没有可用于巡检的摄像头覆盖" in entrance_coverage_result["assistant_content"]

    call_count = len(floor_visual.calls)
    missing_floor = floor_agent.handle_message(
        "帮我判断B3层摄像头画面中有没有垃圾",
        {"org_id": "store-a"},
        [],
    )
    assert missing_floor["visual_result"]["status"] == "BLOCKED"
    assert missing_floor["agent"]["blocked_reason"] == "FLOOR_CAMERA_NOT_FOUND"
    assert len(floor_visual.calls) == call_count

    historical_frame = agent.handle_message("获取昨天 10点广州门店入口摄像头的画面图像", {"org_id": "store-a"}, [])
    assert historical_frame["intent"] == "CAPTURE_SNAPSHOT"
    assert historical_frame["pipeline"]["required_tool"] == "media.frame.extract"

    applications = agent.handle_message("查看广州门店已经订阅上线了哪些应用", {"org_id": "store-a"}, [])
    assert applications["intent"] == "QUERY_SUBSCRIPTIONS"
    assert {"off_duty", "play_phone", "visual_compliance_inspection"}.issubset(
        {item["capability_id"] for item in applications["applications"]}
    )

    alarm_result = agent.handle_message("查看广州门店近7天离岗告警", {"org_id": "store-a"}, [])
    assert alarm_result["intent"] == "QUERY_ALARMS"
    assert alarm_result["result"]["summary"]["total"] == 23
    assert alarm_result["result"]["events"][0]["confidence"] == 0.91
    assert alarm_result["result"]["pagination"]["page_size"] == 50
    assert_no_secrets(alarm_result)

    analytics = agent.handle_message("分析近7天所有门店告警最多的门店 Top10", {"org_id": "store-a"}, [])
    assert analytics["intent"] == "ANALYZE_ALARMS"
    assert analytics["analytics"]["metrics"]["event_total"] == 46

    page_two = agent.paginated_events("store-a", None, "近7天离岗告警", 2, 10)
    assert page_two["pagination"] == {
        "page": 2,
        "page_size": 10,
        "total": 23,
        "total_pages": 3,
        "has_previous": True,
        "has_next": True,
        "range_start": 11,
        "range_end": 20,
        "page_size_options": [10, 20, 50, 100],
    }
    assert len(page_two["events"]) == 10
    assert page_two["events"][0]["event_id"] == "alarm-store-a-10"

    last_page = agent.paginated_events("store-a", None, "近7天离岗告警", 3, 10)
    assert len(last_page["events"]) == 3
    assert last_page["pagination"]["has_next"] is False

    all_stores = agent.paginated_events("oppo", None, "近7天告警", 2, 20)
    assert all_stores["pagination"]["total"] == 46
    assert all_stores["pagination"]["total_pages"] == 3
    assert len(all_stores["events"]) == 20

    try:
        agent.paginated_events("store-a", None, "近7天告警", 1, 25)
        raise AssertionError("unsupported page size should fail")
    except OnlineAgentError as exc:
        assert exc.code == "BAD_REQUEST"

    capabilities = agent.handle_message("查看广州门店已经配置了哪些巡检能力", {"org_id": "store-a"}, [])
    assert capabilities["intent"] == "QUERY_SUBSCRIPTIONS"
    assert capabilities["agent"]["tool_calls"] == ["paas.capability.configured"]

    incomplete = agent.handle_message("给广州门店入口摄像头上线离岗巡检", {"org_id": "store-a"}, [])
    assert incomplete["intent"] == "CREATE_TASK"
    assert incomplete["plan"]["status"] == "NEED_CLARIFICATION"
    assert set(incomplete["plan"]["slots"]["missing_slots"]) == {"effective_time_range", "thresholds", "roi"}

    completed_slots = agent.handle_message(
        "2026-07-01 到 2026-07-31 生效，使用推荐阈值，全画面",
        {"org_id": "store-a"},
        [
            {"sender": "user", "content": "查看广州门店已经订阅上线了哪些应用"},
            {"sender": "assistant", "content": "已读取门店当前上线的应用订阅。", "linked_object": {"agent": {"intent": "QUERY_SUBSCRIPTIONS"}}},
            {"sender": "user", "content": "给广州门店入口摄像头上线离岗巡检"},
            {
                "sender": "assistant",
                "content": "已识别为已有能力订阅。",
                "linked_object": {"agent": {"intent": "CREATE_TASK"}, "plan": {"status": "NEED_CLARIFICATION"}},
            },
        ],
    )
    assert completed_slots["intent"] == "CREATE_TASK"
    assert completed_slots["plan"]["status"] == "NEED_INTEGRATION"
    assert completed_slots["plan"]["slots"]["missing_slots"] == []
    assert completed_slots["plan"]["execution"]["executed"] is False

    inherited_capability = OnlineInspectionAgent(FakePaaSClient(), SlotDroppingAnalyzer()).handle_message(
        "2026-07-01 到 2026-07-31 生效，使用推荐阈值，全画面",
        {"org_id": "store-a"},
        [
            {"sender": "user", "content": "给广州门店入口摄像头上线离岗巡检"},
            {
                "sender": "assistant",
                "content": "已识别为已有能力订阅。",
                "linked_object": {
                    "agent": {"intent": "CREATE_TASK"},
                    "plan": {
                        "status": "NEED_CLARIFICATION",
                        "slots": {"capability": {"capability_id": "off_duty", "name": "离岗检测"}},
                    },
                },
            },
        ],
    )
    assert inherited_capability["intent"] == "CREATE_TASK"
    assert inherited_capability["plan"]["status"] == "NEED_INTEGRATION"
    assert inherited_capability["plan"]["slots"]["capability"]["capability_id"] == "off_duty"

    composite = agent.handle_message("上线顾客进店后 3 分钟内无人接待识别", {"org_id": "store-a"}, [])
    assert composite["intent"] == "COMPOSE_CAPABILITY"
    assert composite["pipeline"]["status"] == "DRAFT"
    node_kinds = {item["kind"] for item in composite["pipeline"]["nodes"]}
    assert {"SOURCE", "DECODE", "SMALL_MODEL", "LARGE_MODEL", "OUTPUT"}.issubset(node_kinds)

    detail = agent.event_detail("alarm-store-a-0")
    assert detail["event_id"] == "alarm-store-a-0"
    assert detail["source"] == "deepvision_online"
    assert_no_secrets(detail)
    print("PASS online agent tests: token refresh, skill routing, media, slots, pipeline, pagination, DTOs, redaction, analytics")


if __name__ == "__main__":
    main()
