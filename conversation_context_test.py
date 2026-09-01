#!/usr/bin/env python3
"""Node-level and end-to-end regression for cross-store continuation V2."""

from __future__ import annotations

import json
import tempfile
import types
from pathlib import Path

import server
from conversation_context import VISUAL_DOMAIN, decide_continuation
from online_agent import OnlineInspectionAgent


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"context-regression-image" + b"\xff\xd9"


class FakeImageResponse:
    headers = {"Content-Type": "image/jpeg"}

    def read(self, _limit):
        return JPEG_BYTES

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ContextClient:
    tenant_code = "tenant_jihu"

    def __init__(self):
        self.organization_calls = 0
        self.camera_calls = []
        self.snapshot_calls = []

    def organization_tree(self):
        self.organization_calls += 1
        return {
            "poiId": "tenant_jihu",
            "name": "吉护集团",
            "poiType": "GeneralPOI",
            "children": [
                {"poiId": "ctx-store-a", "name": "A店", "poiType": "FieldPOI", "children": []},
                {"poiId": "ctx-store-b", "name": "B店", "poiType": "FieldPOI", "children": []},
            ],
        }

    def cameras(self, org_id):
        self.camera_calls.append(org_id)
        suffix = "a" if org_id == "ctx-store-a" else "b"
        return {
            "items": [
                {
                    "sensorId": f"camera-{suffix}",
                    "sensorName": f"{org_id}-展厅",
                    "fieldName": org_id,
                    "deviceStatus": "online",
                    "snapshotUrl": f"https://snapshot.test/{org_id}.jpg",
                }
            ]
        }

    def take_snapshot(self, org_id, camera_id):
        self.snapshot_calls.append((org_id, camera_id))
        return {"snapshotUrl": f"https://snapshot.test/{org_id}/{camera_id}/{len(self.snapshot_calls)}.jpg"}


class ContextAnalyzer:
    configured = False

    def analyze(self, text, _context, _orgs, _capabilities, _history):
        poi_names = []
        if "B店" in text:
            poi_names = ["B店"]
        elif "A店" in text:
            poi_names = ["A店"]
        return {
            "intent": "ANALYZE_VISUAL",
            "confidence": 0.96,
            "poi_names": poi_names,
            "alarm_types": [],
            "camera_names": [],
            "camera_status": None,
            "desired_capability": None,
            "capture_at": None,
            "playback_range": None,
            "thresholds": {},
            "roi": None,
            "limit": 50,
            "explanation": "context regression analyzer",
            "engine": "fake-structured-llm",
            "warning": None,
        }


class ContextVisualReasoner:
    configured = True
    max_images = 8
    max_candidate_images = 8

    def __init__(self):
        self.calls = []

    def analyze(self, question, images):
        self.calls.append(
            {
                "question": question,
                "org_ids": [item.get("org_id") for item in images],
                "camera_ids": [item.get("camera_id") for item in images],
                "urls": [item.get("snapshot_url") for item in images],
            }
        )
        camera_names = [item.get("camera_name") for item in images]
        return {
            "status": "POSITIVE",
            "conclusion": "已按本轮动态条件完成视觉判断。",
            "confidence": 0.93,
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": True,
            "evidence_type": "DIRECT_VISUAL",
            "selected_camera_names": camera_names,
            "anomaly_camera_names": [],
            "observations": ["测试目标可见。"],
            "exclusions": [],
            "model": "fake-context-vlm",
        }


def check_pure_resolver():
    active = {
        "context_id": "ctx-active",
        "version": 4,
        "state": "ACTIVE",
        "domain": VISUAL_DOMAIN,
        "effective_query": "检查 B 店画面中是否有红色沙发",
        "task_scope": {"org_ids": ["ctx-store-b"], "org_names": ["B店"]},
        "evidence_refs": [{"evidence_id": "os-1", "org_id": "ctx-store-b", "camera_id": "camera-b"}],
        "scope_history": [],
    }
    cases = {
        "灰色的呢": ("CONTINUE", "KEEP_SCOPE", "REFRESH_SAME_SCOPE"),
        "在帮我找一个灰色的沙发": ("CONTINUE", "KEEP_SCOPE", "REFRESH_SAME_SCOPE"),
        "这张图里有几个": ("CONTINUE", "KEEP_SCOPE", "REUSE_SAME_FRAME"),
        "现在还有吗": ("CONTINUE", "KEEP_SCOPE", "REFRESH_SAME_SCOPE"),
        "当前门店呢": ("CONTINUE", "RETURN_PAGE_SCOPE", "RECAPTURE_RESOLVED_SCOPE"),
        "其他门店也看一下": ("CONTINUE", "EXPAND_SCOPE", "RECAPTURE_RESOLVED_SCOPE"),
        "只看失败的两家": ("CONTINUE", "NARROW_SCOPE", "RECAPTURE_RESOLVED_SCOPE"),
        "这两家对比一下": ("CONTINUE", "COMPARE_SCOPE", "RECAPTURE_RESOLVED_SCOPE"),
        "新问题，不要沿用": ("NEW_TASK", "KEEP_SCOPE", "NONE"),
        "再帮我做一个PPT": ("NEW_TASK", "KEEP_SCOPE", "NONE"),
        "今天天气怎么样": ("NEW_TASK", "KEEP_SCOPE", "NONE"),
    }
    for text, expected in cases.items():
        decision = decide_continuation(text, active, "ctx-store-a")
        assert (decision["decision"], decision["scope_operation"], decision["evidence_mode"]) == expected, (text, decision)
    assert "本轮用户补充：灰色的呢" in decide_continuation("灰色的呢", active, "ctx-store-a")["effective_query"]
    assert decide_continuation("灰色的呢", active, "ctx-store-a", "OPEN_QA")["decision"] == "NEW_TASK"
    ambiguous = decide_continuation("这家呢", active, "ctx-store-a")
    assert (ambiguous["decision"], ambiguous["scope_operation"], ambiguous["reason_code"]) == (
        "CLARIFY",
        "CLARIFY_SCOPE",
        "AMBIGUOUS_PAGE_OR_TASK_SCOPE",
    )


def check_end_to_end_nodes():
    original_db = server.DB_PATH
    original_evidence_dir = server.ONLINE_SNAPSHOT_EVIDENCE_DIR
    original_online_agent = server.online_agent_for_tenant
    original_urlopen = server.urlrequest.urlopen
    tmp = tempfile.TemporaryDirectory()
    try:
        server.DB_PATH = Path(tmp.name) / "context.db"
        server.ONLINE_SNAPSHOT_EVIDENCE_DIR = Path(tmp.name) / "online-evidence"
        server.init_db(reset=True)
        client = ContextClient()
        reasoner = ContextVisualReasoner()
        agent = OnlineInspectionAgent(client, ContextAnalyzer(), reasoner)
        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: agent
        server.urlrequest.urlopen = lambda _request, timeout=20: FakeImageResponse()

        with server.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO orgs VALUES (?,?,?,?,?)",
                ("ctx-store-a", "tenant_jihu", None, "A店", "store"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO orgs VALUES (?,?,?,?,?)",
                ("ctx-store-b", "tenant_jihu", None, "B店", "store"),
            )
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            conversation = server.create_conversation(conn, user, "跨门店上下文节点回归", org_id="ctx-store-a")
            handler = types.SimpleNamespace()

            first = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "检查 B店 画面中是否有红色沙发", "context": {"org_id": "ctx-store-a"}},
            )
            assert first["intent"] == "ANALYZE_VISUAL"
            assert client.snapshot_calls == [("ctx-store-b", "camera-b")]
            assert first["conversation_context"]["version"] == 1
            assert first["conversation_context"]["page_scope"]["org_id"] == "ctx-store-a"
            assert first["conversation_context"]["task_scope"]["org_ids"] == ["ctx-store-b"]
            first_message = first["messages"][0]
            artifact = first_message["linked_object"]["artifact"]
            assert artifact["conversationScope"]["task_scope"]["org_names"] == ["B店"]
            trace_ids = [item["node_id"] for item in first["agent"]["trace"]["nodes"]]
            for expected_node in ("tool_1", "tool_2", "tool_3"):
                assert expected_node in trace_ids
            tool_calls = first["agent"]["tool_calls"]
            assert tool_calls[:3] == ["conversation.context.resolve", "permission.scope.check", "scope.resolve"]
            assert "paas.media.snapshot" in tool_calls and "vlm.image.inspect" in tool_calls
            assert all(item.startswith("/api/online-snapshot-evidence/") for item in [media["snapshot_url"] for media in artifact["mediaGallery"]])

            before_ambiguous = len(client.snapshot_calls)
            active_before_ambiguous = server.load_active_conversation_context(conn, user, conversation["conversation_id"])
            ambiguous = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "这家呢", "context": {"org_id": "ctx-store-a"}},
            )
            assert ambiguous["intent"] == "CLARIFY_VISUAL_SCOPE"
            assert ambiguous["agent"]["status"] == "NEED_CLARIFICATION"
            assert ambiguous["agent"]["tool_calls"] == ["conversation.context.resolve", "scope.clarify"]
            assert len(client.snapshot_calls) == before_ambiguous
            active_after_ambiguous = server.load_active_conversation_context(conn, user, conversation["conversation_id"])
            assert active_after_ambiguous["context_id"] == active_before_ambiguous["context_id"]

            second = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "灰色的呢", "context": {"org_id": "ctx-store-a"}},
            )
            assert second["intent"] == "ANALYZE_VISUAL"
            assert client.snapshot_calls[-1] == ("ctx-store-b", "camera-b")
            assert second["conversation_context"]["version"] == 2
            assert second["conversation_context"]["task_scope"]["org_ids"] == ["ctx-store-b"]
            assert second["conversation_context"]["evidence_mode"] == "REFRESH_SAME_SCOPE"
            assert "本轮用户补充：灰色的呢" in reasoner.calls[-1]["question"]

            before_same_frame = len(client.snapshot_calls)
            same_frame = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "这张图里有几个", "context": {"org_id": "ctx-store-a"}},
            )
            assert len(client.snapshot_calls) == before_same_frame
            assert same_frame["conversation_context"]["evidence_mode"] == "REUSE_SAME_FRAME"
            assert all(url.startswith("data:image/") for url in reasoner.calls[-1]["urls"])
            same_artifact = same_frame["messages"][0]["linked_object"]["artifact"]
            assert all(not item["snapshot_url"].startswith("data:") for item in same_artifact["mediaGallery"])
            assert "evidence.resolve" in same_frame["agent"]["tool_calls"]

            page_store = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "当前门店呢", "context": {"org_id": "ctx-store-a"}},
            )
            assert client.snapshot_calls[-1] == ("ctx-store-a", "camera-a")
            assert page_store["conversation_context"]["task_scope"]["org_ids"] == ["ctx-store-a"]
            assert page_store["conversation_context"]["evidence_mode"] == "RECAPTURE_RESOLVED_SCOPE"

            # "Other stores" expands from the active A-store task and is
            # intercepted by the batch path before the single-store agent.
            batch = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "其他门店也看一下", "context": {"org_id": "ctx-store-a"}},
            )
            assert batch["intent"] == "BATCH_INSPECTION_EXECUTE"
            assert batch["plan"]["slots"]["org_scope"]["resolved_ids"] == ["ctx-store-b"]
            assert batch["conversation_context"]["task_scope"]["org_ids"] == ["ctx-store-b"]

            previous_store = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "上一家呢", "context": {"org_id": "ctx-store-a"}},
            )
            assert previous_store["conversation_context"]["task_scope"]["org_ids"] == ["ctx-store-a"]
            assert client.snapshot_calls[-1] == ("ctx-store-a", "camera-a")

            compared = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "这两家对比一下", "context": {"org_id": "ctx-store-a"}},
            )
            assert set(compared["conversation_context"]["task_scope"]["org_ids"]) == {"ctx-store-a", "ctx-store-b"}, compared
            assert compared["intent"] == "BATCH_INSPECTION_EXECUTE"
            assert set(compared["plan"]["slots"]["org_scope"]["resolved_ids"]) == {"ctx-store-a", "ctx-store-b"}

            before_weather = len(client.snapshot_calls)
            active_before_weather = server.load_active_conversation_context(conn, user, conversation["conversation_id"])
            weather = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                conversation["conversation_id"],
                {"content": "今天天气怎么样", "context": {"org_id": "ctx-store-a"}},
            )
            assert weather["intent"] == "OPEN_QA", weather
            assert len(client.snapshot_calls) == before_weather
            active_after_weather = server.load_active_conversation_context(conn, user, conversation["conversation_id"])
            assert active_after_weather["context_id"] == active_before_weather["context_id"]

            restricted = {**user, "allowed_org_ids": json.dumps(["ctx-store-a"])}
            denied_conversation = server.create_conversation(conn, restricted, "单店权限门禁", org_id="ctx-store-a")
            before_denied = len(client.snapshot_calls)
            denied = server.AppHandler.api_send_message(
                handler,
                conn,
                restricted,
                denied_conversation["conversation_id"],
                {"content": "检查 B店 画面中是否有沙发", "context": {"org_id": "ctx-store-a"}},
            )
            assert len(client.snapshot_calls) == before_denied
            assert "不在当前用户授权范围" in denied["messages"][0]["content"]

            # Conversations created before the context-revision rollout still
            # contain trustworthy visual messages and archived metadata.  A
            # high-confidence follow-up must lazily recover task semantics,
            # force a fresh capture when stale, and never fall into shopping
            # advice/OpenQA merely because the new context table is empty.
            legacy_conversation = server.create_conversation(conn, user, "旧会话懒迁移", org_id="ctx-store-a")
            legacy_user_message = server.add_message(
                conn,
                legacy_conversation["conversation_id"],
                "user",
                "帮我看下当前门店画面中是否有穿红衣服的人",
            )
            legacy_assistant_message = server.add_message(
                conn,
                legacy_conversation["conversation_id"],
                "assistant",
                "已完成当前画面分析。",
                None,
                {
                    "source": "deepvision_online",
                    "agent": {"intent": "ANALYZE_VISUAL", "tool_calls": ["paas.media.snapshot", "vlm.image.inspect"]},
                    "visual_context": {
                        "images": [
                            {
                                "kind": "IMAGE",
                                "camera_id": "camera-a",
                                "camera_name": "A店-展厅",
                                "org_id": "ctx-store-a",
                                "org_name": "A店",
                                "captured_at": "2026-08-27T11:00:00+08:00",
                            }
                        ]
                    },
                    "artifact": {
                        "visualResult": {
                            "question": "帮我看下当前门店画面中是否有穿红衣服的人",
                            "status": "UNCERTAIN",
                        }
                    },
                },
            )
            conn.execute(
                "UPDATE messages SET created_at='2026-08-27T11:00:00+08:00' WHERE message_id IN (?,?)",
                (legacy_user_message["message_id"], legacy_assistant_message["message_id"]),
            )
            assert server.load_active_conversation_context(conn, user, legacy_conversation["conversation_id"]) is None
            before_legacy_followup = len(client.snapshot_calls)
            legacy_followup = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                legacy_conversation["conversation_id"],
                {"content": "在帮我找一个灰色的沙发", "context": {"org_id": "ctx-store-a"}},
            )
            assert legacy_followup["intent"] == "ANALYZE_VISUAL", legacy_followup
            assert len(client.snapshot_calls) > before_legacy_followup
            assert client.snapshot_calls[-1] == ("ctx-store-a", "camera-a")
            assert legacy_followup["conversation_context"]["version"] == 1
            assert legacy_followup["conversation_context"]["reason_code"] == "RECOVERED_VISUAL_CONTEXT"
            assert legacy_followup["agent"]["tool_calls"][0] == "conversation.context.recover"
            assert "再帮我找一个灰色的沙发" in reasoner.calls[-1]["question"]

            conn.execute(
                "UPDATE conversation_contexts SET expires_at='2026-08-27T11:01:00+08:00' WHERE context_id=?",
                (legacy_followup["conversation_context"]["context_id"],),
            )
            expired_revision_followup = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                legacy_conversation["conversation_id"],
                {"content": "再帮我找一个蓝色的沙发", "context": {"org_id": "ctx-store-a"}},
            )
            assert expired_revision_followup["intent"] == "ANALYZE_VISUAL"
            assert expired_revision_followup["conversation_context"]["version"] == 2
            assert expired_revision_followup["conversation_context"]["evidence_mode"] == "RECAPTURE_RESOLVED_SCOPE"
            assert expired_revision_followup["agent"]["tool_calls"][0] == "conversation.context.recover"

            legacy_cross_domain = server.create_conversation(conn, user, "旧视觉会话跨域隔离", org_id="ctx-store-a")
            cross_user = server.add_message(conn, legacy_cross_domain["conversation_id"], "user", "检查门店画面")
            cross_assistant = server.add_message(
                conn,
                legacy_cross_domain["conversation_id"],
                "assistant",
                "已完成。",
                None,
                {
                    "source": "deepvision_online",
                    "agent": {"intent": "ANALYZE_VISUAL"},
                    "visual_context": {"images": [{"org_id": "ctx-store-a", "org_name": "A店", "camera_id": "camera-a"}]},
                    "artifact": {"visualResult": {"question": "检查门店画面", "status": "POSITIVE"}},
                },
            )
            conn.execute(
                "UPDATE messages SET created_at='2026-08-27T11:00:00+08:00' WHERE message_id IN (?,?)",
                (cross_user["message_id"], cross_assistant["message_id"]),
            )
            cross_domain_answer = server.AppHandler.api_send_message(
                handler,
                conn,
                user,
                legacy_cross_domain["conversation_id"],
                {"content": "今天天气怎么样", "context": {"org_id": "ctx-store-a"}},
            )
            assert cross_domain_answer["intent"] == "OPEN_QA"
            assert server.load_active_conversation_context(conn, user, legacy_cross_domain["conversation_id"]) is None

            # Two concurrent writers that read the same revision: only the
            # first one may become active.
            prepared_one, _active_one, _decision_one = server.prepare_conversation_turn_context(
                conn, user, conversation["conversation_id"], "灰色的呢", {"org_id": "ctx-store-a"}, "AUTO"
            )
            prepared_two, _active_two, _decision_two = server.prepare_conversation_turn_context(
                conn, user, conversation["conversation_id"], "蓝色的呢", {"org_id": "ctx-store-a"}, "AUTO"
            )
            active_scope = server.load_active_conversation_context(conn, user, conversation["conversation_id"])["task_scope"]

            def draft(query):
                return {
                    "intent": "ANALYZE_VISUAL",
                    "_conversation_context": {
                        "domain": VISUAL_DOMAIN,
                        "task_kind": "ANALYZE_VISUAL",
                        "effective_query": query,
                        "task_scope": active_scope,
                        "predicate": {"effective_query": query},
                        "temporal": {"mode": "CURRENT"},
                        "decision": {"scope_operation": "KEEP_SCOPE", "evidence_mode": "REFRESH_SAME_SCOPE"},
                        "result_refs": [],
                    },
                }

            persisted_one = server.persist_online_conversation_context(
                conn, user, conversation["conversation_id"], prepared_one, draft("灰色"), None
            )
            persisted_two = server.persist_online_conversation_context(
                conn, user, conversation["conversation_id"], prepared_two, draft("蓝色"), None
            )
            assert persisted_one["status"] == "ACTIVE"
            assert persisted_two["status"] == "STALE_CONTEXT"

            # Context ownership is both tenant- and user-bound.
            other_user = {**user, "user_id": "another-user"}
            assert server.load_active_conversation_context(conn, other_user, conversation["conversation_id"]) is None
            other_tenant = {**user, "tenant_id": "other-tenant"}
            assert server.load_active_conversation_context(conn, other_tenant, conversation["conversation_id"]) is None
    finally:
        server.DB_PATH = original_db
        server.ONLINE_SNAPSHOT_EVIDENCE_DIR = original_evidence_dir
        server.online_agent_for_tenant = original_online_agent
        server.urlrequest.urlopen = original_urlopen
        tmp.cleanup()


def main():
    check_pure_resolver()
    check_end_to_end_nodes()
    print("PASS conversation context V2: resolver, scope, permission, evidence, routing, persistence, concurrency, UI artifact")


if __name__ == "__main__":
    main()
