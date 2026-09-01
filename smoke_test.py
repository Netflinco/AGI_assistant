#!/usr/bin/env python3
"""Business smoke tests for the P0 AGI inspection MVP."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from datetime import date
from io import BytesIO
from pathlib import Path

import server
from agent_core import SkillDefinition, ToolDefinition, validate_agent_manifest
from agent_skills import public_agent_catalog, public_skill_catalog, standard_agent_catalog
from server import add_message, build_agent_trace, parse_duration_days
from online_agent import OnlineAgentError, VisualReasoner
from comparison_service import OvdAdapterConfig, OvdAdapterFailure, validate_ovd_endpoint


ROOT = os.path.dirname(os.path.abspath(__file__))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(base: str, method: str, path: str, user: str = "u_admin", body=None, expected=200, tenant: str | None = None):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-User-Id": user}
    if tenant:
        headers["X-Tenant-Code"] = tenant
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            assert resp.status == expected, f"{method} {path}: expected {expected}, got {resp.status}"
            return payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        assert exc.code == expected, f"{method} {path}: expected {expected}, got {exc.code}: {payload}"
        return payload


def request_bytes(base: str, path: str, user: str = "u_admin", tenant: str | None = None):
    headers = {"X-User-Id": user}
    if tenant:
        headers["X-Tenant-Code"] = tenant
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = resp.read()
        assert resp.status == 200
        return data, resp.headers


def assert_ok(payload):
    assert payload.get("ok") is True, payload
    return payload["data"]


def check_online_tenant_does_not_escalate_role():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE users(user_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, tenant_id TEXT NOT NULL, allowed_org_ids TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users VALUES (?,?,?,?,?)",
        ("u_store", "门店负责人", "store_manager", "tenant_jihu", json.dumps(["org_gz"])),
    )
    original_online = server.online_agent_for_tenant
    original_tenant_name = server.tenant_name_for_code
    try:
        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: types.SimpleNamespace(tenant_code="oppo")
        server.tenant_name_for_code = lambda _conn, _tenant: "OPPO"
        handler = types.SimpleNamespace(headers={"X-User-Id": "u_store", "X-Tenant-Code": "oppo"})
        user = server.user_from_request(handler, conn)
    finally:
        server.online_agent_for_tenant = original_online
        server.tenant_name_for_code = original_tenant_name
        conn.close()
    assert user["tenant_id"] == "oppo"
    assert user["role"] == "store_manager"
    assert json.loads(user["allowed_org_ids"]) == ["org_gz"]


def check_online_delivery_failure_contract():
    transient = server.public_online_delivery_failure(server.OnlineAgentError("UPSTREAM_UNAVAILABLE", "vendor details"))
    assert transient["status"] == "FAILED"
    assert transient["state"] == "TEMPORARY_FAILURE"
    assert transient["retryable"] is True
    assert transient["next_action"] == "RETRY"
    assert "vendor details" not in transient["message"]

    rejected = server.public_online_delivery_failure(server.OnlineAgentError("UPSTREAM_REJECTED", "vendor details"))
    assert rejected["state"] == "CAPABILITY_UNAVAILABLE"
    assert rejected["retryable"] is False
    assert rejected["next_action"] == "CHECK_ACCESS"

    deployment = server.public_online_delivery_failure(
        server.OnlineAgentError(
            "UPSTREAM_REJECTED",
            "vendor details",
            {"vendor_code": 400, "vendor_message": "内部错误：请联系技术人员配置该产品对应部署形态"},
        )
    )
    assert deployment["state"] == "CAPABILITY_UNAVAILABLE"
    assert deployment["next_action"] == "CONFIGURE_PRODUCT_DEPLOYMENT"
    assert "部署形态" in deployment["message"]


def check_open_web_search_trace_contract():
    response = {
        "web_search": {
            "query": "2026年8月4日杭州当前天气",
            "provider": "tavily",
            "topic": "general",
            "status": "SUCCEEDED",
            "fetched_at": "2026-08-03T10:00:00+00:00",
            "request_id": "search-safe-1",
            "freshness": "day",
            "temporal_context": {
                "reference_time": "2026-08-04T16:36:52+08:00",
                "target_date": "2026-08-04",
            "timezone": "Asia/Shanghai",
            "scope": "weather",
            "query_rewrite": "WEATHER_CANONICAL",
            "weather_location": "杭州",
        },
            "citations": [
                {
                    "title": "公开来源",
                    "url": "https://www.example.gov.cn/public-role?tracking=1",
                    "snippet": "公开摘要",
                    "published_at": "2026-08-03",
                    "domain": "www.example.gov.cn",
                    "raw_html": "must-not-persist",
                }
            ],
        }
    }
    artifact = server.conversation_artifact(response)
    assert artifact["webSearch"]["citations"][0]["url"] == "https://www.example.gov.cn/public-role?tracking=1"
    assert artifact["webSearch"]["topic"] == "general"
    assert artifact["webSearch"]["status"] == "SUCCEEDED"
    assert artifact["webSearch"]["freshness"] == "day"
    assert artifact["webSearch"]["temporal_context"]["target_date"] == "2026-08-04"
    assert artifact["webSearch"]["temporal_context"]["query_rewrite"] == "WEATHER_CANONICAL"
    assert artifact["webSearch"]["temporal_context"]["weather_location"] == "杭州"
    assert "raw_html" not in artifact["webSearch"]["citations"][0]
    agent = {
        "intent": "OPEN_QA",
        "mode": "OPEN_QA",
        "status": "SUCCEEDED",
        "engine": "web_search_source_fallback",
        "confidence": 0.99,
        "tool_calls": ["web.search"],
        "skill": {"name": "open_question_answering"},
        "analysis": {"intent": "OPEN_QA", "confidence": 0.99, "state": "WEB_SEARCHED"},
    }
    trace = server.build_agent_trace("美国总统是谁", agent, artifact, "open_qa")
    nodes = trace["nodes"]
    assert all(node["node_id"] not in {"memory_retrieve", "knowledge_recall"} for node in nodes)
    search_node = next(node for node in nodes if node["node_id"] == "tool_1")
    assert search_node["title"] == "工具调用"
    assert "检索公开网页" in search_node["summary"]
    assert search_node["output"]["citations"][0]["url"] == "https://www.example.gov.cn/public-role?tracking=1"
    assert search_node["input"]["query"] == "2026年8月4日杭州当前天气"
    assert search_node["input"]["temporal_context"]["target_date"] == "2026-08-04"


def check_online_delivery_failure_persistence():
    original_db_path = server.DB_PATH
    original_online_agent = server.online_agent_for_tenant
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        server.DB_PATH = Path(tmp_dir.name) / "online-delivery-failure.db"
        server.init_db(reset=True)

        class FailingOnlineAgent:
            def handle_message(self, _content, _context, _history):
                raise OnlineAgentError("UPSTREAM_UNAVAILABLE", "vendor connection refused")

        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: FailingOnlineAgent()
        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            conversation = server.create_conversation(conn, user, "在线失败交付")
            response = server.AppHandler.api_send_message(
                types.SimpleNamespace(),
                conn,
                user,
                conversation["conversation_id"],
                {"content": "读取当前门店状态", "context": {"org_id": "org_gz"}},
            )
            assert response["messages"] == []
            assert response["delivery"]["state"] == "TEMPORARY_FAILURE"
            assert response["delivery"]["retryable"] is True
            assert "vendor" not in response["delivery"]["message"]
            stored_messages = server.rows(
                conn,
                "SELECT sender, linked_object FROM messages WHERE conversation_id=? ORDER BY created_at",
                (conversation["conversation_id"],),
            )
            assert [item["sender"] for item in stored_messages] == ["user"]
            delivery = json.loads(stored_messages[0]["linked_object"])["delivery"]
            assert delivery["state"] == "TEMPORARY_FAILURE"
            audit = server.one(
                conn,
                "SELECT after_json FROM audit_logs WHERE action='agent.online.query.failed' ORDER BY created_at DESC LIMIT 1",
                (),
            )
            assert json.loads(audit["after_json"])["failure_state"] == "TEMPORARY_FAILURE"
    finally:
        server.online_agent_for_tenant = original_online_agent
        server.DB_PATH = original_db_path
        tmp_dir.cleanup()


def check_online_snapshot_archiving_contract():
    original_db_path = server.DB_PATH
    original_snapshot_dir = server.ONLINE_SNAPSHOT_EVIDENCE_DIR
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        server.DB_PATH = Path(tmp_dir.name) / "online-snapshot.db"
        server.ONLINE_SNAPSHOT_EVIDENCE_DIR = Path(tmp_dir.name) / "online-snapshot-evidence"
        server.init_db(reset=True)
        image_bytes = b"\xff\xd8\xff\xe0" + b"snapshot-image" + b"\xff\xd9"

        class FakeResponse:
            headers = {"Content-Type": "image/jpeg"}

            def read(self, _limit):
                return image_bytes

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        original_urlopen = server.urlrequest.urlopen
        server.urlrequest.urlopen = lambda _request, timeout=20: FakeResponse()
        try:
            with server.connect() as conn:
                raw_url = "https://signed.example/snapshot.jpg?OSSAccessKeyId=id&Signature=secret&Expires=999"
                response = {
                    "media": {
                        "kind": "IMAGE",
                        "org_id": "store-a",
                        "org_name": "测试门店",
                        "camera_id": "camera-a",
                        "camera_name": "入口摄像头",
                        "captured_at": "2026-08-13T12:00:00+08:00",
                        "snapshot_url": raw_url,
                    },
                    "media_gallery": [
                        {
                            "kind": "IMAGE",
                            "org_id": "store-a",
                            "org_name": "测试门店",
                            "camera_id": "camera-a",
                            "camera_name": "入口摄像头",
                            "captured_at": "2026-08-13T12:00:00+08:00",
                            "snapshot_url": raw_url,
                        }
                    ],
                    "_visual_context": {"images": [{"snapshot_url": raw_url, "camera_name": "入口摄像头"}]},
                }
                server.archive_online_response_snapshots(conn, "oppo", response)
                local_url = response["media"]["snapshot_url"]
                assert local_url.startswith("/api/online-snapshot-evidence/os_")
                assert "signature" not in local_url.lower()
                assert response["media_gallery"][0]["snapshot_url"] == local_url
                assert response["_visual_context"]["images"][0]["snapshot_url"] == local_url
                assert server.sanitize_linked_object_for_storage({"artifact": server.conversation_artifact(response)})["artifact"]["media"]["snapshot_url"] == local_url
                evidence = server.one(conn, "SELECT * FROM online_snapshot_evidence", ())
                assert evidence and Path(evidence["storage_path"]).read_bytes() == image_bytes
        finally:
            server.urlrequest.urlopen = original_urlopen
    finally:
        server.DB_PATH = original_db_path
        server.ONLINE_SNAPSHOT_EVIDENCE_DIR = original_snapshot_dir
        tmp_dir.cleanup()


def check_auto_online_open_qa_pdf_persistence():
    original_db_path = server.DB_PATH
    original_export_dir = server.OPEN_QA_EXPORT_DIR
    original_online_agent = server.online_agent_for_tenant
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        server.DB_PATH = Path(tmp_dir.name) / "auto-online-open-qa.db"
        server.OPEN_QA_EXPORT_DIR = Path(tmp_dir.name) / "open-qa-exports"
        server.init_db(reset=True)

        agent_calls = []

        class FakeOpenQaAgent:
            def handle_message(self, content, _context, history):
                assert "东京" in content
                assert history == []
                agent_calls.append(content)
                response = {
                    "intent": "OPEN_QA",
                    "confidence": 0.97,
                    "source": "open_qa",
                    "assistant_content": "东京五日通用行程建议。",
                    "agent": {
                        "intent": "OPEN_QA",
                        "mode": "OPEN_QA",
                        "engine": "policy_response",
                        "status": "SUCCEEDED",
                        "tool_calls": ["web.search"],
                        "stages": ["UNDERSTAND", "RETURN_GENERAL_ANSWER"],
                        "decision": {"response_strategy": "SEARCH_AND_CITE"},
                    },
                    "web_search": {
                        "query": "东京五日旅行规划",
                        "provider": "tavily",
                        "status": "SUCCEEDED",
                        "citations": [
                            {
                                "title": "Tokyo official travel guide",
                                "url": "https://www.gotokyo.org/en/",
                                "snippet": "Official destination information.",
                                "domain": "www.gotokyo.org",
                            }
                        ],
                    },
                    "travel_guide": {
                        "destination": "东京",
                        "days": 5,
                        "travel_year": 2026,
                        "images": [],
                        "hotels": [
                            {
                                "name": "Tokyo",
                                "address": "Tokyo · 坐标 35.6762, 139.6503",
                                "address_verified": False,
                                "summary": "Generic destination page, not a hotel.",
                                "source_url": "https://guide.example/tokyo",
                                "map_url": "https://www.google.com/maps/search/?api=1&query=Tokyo",
                            },
                            {
                                "name": "Example Tokyo Hotel",
                                "address": "1-1-1 Marunouchi, Chiyoda Ward, Tokyo",
                                "address_verified": True,
                                "summary": "Publicly listed hotel near a major transit station.",
                                "source_url": "https://hotel.example/tokyo",
                                "map_url": "https://www.google.com/maps/search/?api=1&query=Example+Tokyo+Hotel",
                            }
                        ],
                        "restaurants": [
                            {
                                "name": "Example Tokyo Restaurant",
                                "address": "2-2-2 Ginza, Chuo Ward, Tokyo",
                                "address_verified": True,
                                "summary": "Publicly listed local restaurant candidate.",
                                "source_url": "https://restaurant.example/tokyo",
                                "map_url": "https://www.google.com/maps/search/?api=1&query=Example+Tokyo+Restaurant",
                            }
                        ],
                        "recommendation_notice": "地点须在出发前复核。",
                    },
                }
                if "PDF" in content or "pdf" in content:
                    response["requested_output_format"] = "PDF"
                return response

        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: FakeOpenQaAgent()
        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            conversation = server.create_conversation(conn, user, "AUTO 在线开放问答 PDF")
            response = server.AppHandler.api_send_message(
                types.SimpleNamespace(),
                conn,
                user,
                conversation["conversation_id"],
                {
                    "content": "想去东京玩5天，请生成一份旅行攻略和PDF文档",
                    "context": {"mode_override": "AUTO"},
                },
            )
            assert response["requested_output_format"] == "PDF"
            assert "document.generate_pdf" in response["agent"]["tool_calls"]
            assert "已生成 PDF 文档" in response["messages"][0]["content"]
            artifact = response["messages"][0]["linked_object"]["artifact"]
            document = artifact["generatedDocument"]
            assert document["mime_type"] == "application/pdf"
            assert document["size_bytes"] > 4000
            assert [item["name"] for item in artifact["travelGuide"]["hotels"]] == ["Example Tokyo Hotel"]
            stored_messages = server.rows(
                conn,
                "SELECT sender FROM messages WHERE conversation_id=? ORDER BY created_at",
                (conversation["conversation_id"],),
            )
            assert [item["sender"] for item in stored_messages] == ["user", "assistant"]
            generated_path = next(server.OPEN_QA_EXPORT_DIR.rglob(f"{document['document_id']}.pdf"))
            assert generated_path.read_bytes().startswith(b"%PDF-")
            from pypdf import PdfReader
            reader = PdfReader(str(generated_path))
            assert len(reader.pages) >= 3
            pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            assert "酒店地址" in pdf_text and "1-1-1 Marunouchi" in pdf_text
            assert "Tokyo · 坐标" not in pdf_text

            followup_conversation = server.create_conversation(conn, user, "上下文 PDF 导出")
            initial_response = server.AppHandler.api_send_message(
                types.SimpleNamespace(),
                conn,
                user,
                followup_conversation["conversation_id"],
                {
                    "content": "想去东京玩5天，请先给我一份完整攻略",
                    "context": {"mode_override": "AUTO"},
                },
            )
            assert "generatedDocument" not in initial_response["messages"][0]["linked_object"].get("artifact", {})
            call_count = len(agent_calls)
            followup_response = server.AppHandler.api_send_message(
                types.SimpleNamespace(),
                conn,
                user,
                followup_conversation["conversation_id"],
                {
                    "content": "整理一个p d f",
                    "context": {"mode_override": "AUTO"},
                },
            )
            assert len(agent_calls) == call_count, "PDF follow-up must not call the model or web search again"
            assert followup_response["requested_output_format"] == "PDF"
            assert followup_response["messages"][0]["content"] == "已根据上一轮内容生成 PDF 文档，可在下方下载。"
            followup_agent = followup_response["agent"]
            assert followup_agent["engine"] == "context_document_export"
            assert followup_agent["tool_calls"] == ["conversation.context.read", "document.generate_pdf"]
            followup_document = followup_response["messages"][0]["linked_object"]["artifact"]["generatedDocument"]
            followup_path = next(server.OPEN_QA_EXPORT_DIR.rglob(f"{followup_document['document_id']}.pdf"))
            followup_reader = PdfReader(str(followup_path))
            followup_text = "\n".join(page.extract_text() or "" for page in followup_reader.pages)
            assert "东京 5 日旅行攻略" in followup_text
            assert "东京五日通用行程建议" in followup_text
            assert "使用浏览器打印功能" not in followup_text
            followup_messages = server.rows(
                conn,
                "SELECT sender FROM messages WHERE conversation_id=? ORDER BY created_at",
                (followup_conversation["conversation_id"],),
            )
            assert [item["sender"] for item in followup_messages] == ["user", "assistant", "user", "assistant"]
    finally:
        server.online_agent_for_tenant = original_online_agent
        server.OPEN_QA_EXPORT_DIR = original_export_dir
        server.DB_PATH = original_db_path
        tmp_dir.cleanup()


def check_frontend_contracts():
    """Guard high-risk frontend error handling without requiring a browser."""
    app_path = os.path.join(ROOT, "static", "app.js")
    index_path = os.path.join(ROOT, "static", "index.html")
    with open(app_path, "r", encoding="utf-8") as handle:
        source = handle.read()
    with open(index_path, "r", encoding="utf-8") as handle:
        index_source = handle.read()

    api_source = source[source.index("async function api(") : source.index("function toast(")]
    assert 'code: "NETWORK_UNAVAILABLE"' in api_source, "api() must normalize fetch failures"
    assert 'code: "INVALID_SERVER_RESPONSE"' in api_source, "api() must normalize invalid JSON responses"
    assert "Failed to fetch" not in api_source, "api() must not expose raw browser network errors"

    friendly_source = source[source.index("function friendlyError(") : source.index("function friendlyAssistantContent(")]
    assert "本地服务连接失败，请确认服务已启动后重试。" in friendly_source
    assert "服务响应格式异常，请刷新后重试。" in friendly_source
    assert "INTEGRATION_ALREADY_EXISTS" in friendly_source
    assert "INTEGRATION_VALIDATION_FAILED" in friendly_source
    assert "SECURE_STORAGE_UNAVAILABLE" in friendly_source
    assert "INTERNAL_ERROR" in friendly_source

    loader_source = source[source.index("async function loadConversation(") : source.index("async function loadSubscriptions(")]
    assert 'error?.code !== "NETWORK_UNAVAILABLE"' in loader_source, "history loading should only retry network failures"
    assert "window.setTimeout(resolve, 300)" in loader_source, "history loading should retry transient disconnects once"
    assert "if (!renderAfter) throw error;" in loader_source, "background history loads must bubble errors"
    assert "toast(friendlyError(error));" in loader_source, "visible history failures must use friendly messages"
    assert "isCurrentConversation && renderAfter" in loader_source, "clicking the current history item must still be handled"
    assert 'state.activeView !== "chat"' in loader_source, "current history item should reopen chat from non-chat views"

    assert "integrationSetupDrafts" in source, "integration setup drafts must survive chat rerenders"
    assert '$("messages").addEventListener("input"' in source, "integration setup inputs must be captured on edit"
    assert "saveIntegrationSetupDraft(form);" in source, "integration setup submit must persist the latest draft before posting"
    assert 'const integrationFocus = captureIntegrationSetupFocus();' in source, "chat rerenders must capture focused setup input"
    assert "restoreIntegrationSetupFocus(integrationFocus);" in source, "chat rerenders must restore setup input focus"
    assert "data-integration-setup-key" in source, "integration setup forms need stable draft keys"
    assert "expandedWebSearchKeys: new Set()" in source, "public sources must start collapsed"
    assert 'data-web-search-key="${escapeHtml(webSearchKey)}"${openAttr}' in source, "public sources need stable rerender keys"
    assert '<summary class="web-search-summary">' in source, "public source section must have an expandable summary"
    assert 'class="web-search-citation-list"' in source, "expanded public sources must retain their citation list"
    assert 'status === "FAILED"' in source, "public search failures must remain distinguishable from empty results"
    assert '"已执行公开检索，但没有找到可用于核验的来源。"' in source, "zero-result searches must not disappear from chat"
    assert "captureExpandedWebSearchState();" in source, "chat rerenders must preserve expanded public sources"
    assert '.web-search-artifact[data-web-search-key]' in source, "public source toggle events must update persisted UI state"
    assert "state.expandedWebSearchKeys.clear();" in source, "public source UI state must reset with conversation context"
    assert ".web-search-artifact[open] .web-search-summary::after" in open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read(), "public source affordance must reflect expanded state"
    assert "const prefill = setup.prefill || {};" in source, "integration setup must consume backend extracted fields"
    assert "transient_secret_prefill" in source, "integration setup must keep response-only secrets in memory"
    assert 'NONE: "不复用历史证据"' in source, "new visual tasks must not be mislabeled as requiring no evidence"
    assert 'visualScope?.type === "CAMERA_COVERAGE"' in source, "store-wide visual coverage needs its own scope summary"
    assert '可用镜头 ${Number(visualScope.eligible_camera_count) || 0} 路' in source
    assert '已抓取并分析 ${Number(visualScope.captured_camera_count) || 0} 路' in source
    assert 'secretFields.has(key) ? "已接收" : value' in source, "recognized secret fields must be masked in summaries"
    assert 'draft[field] = savedDraft[field] !== undefined ? savedDraft[field] : (prefill[field] || "");' in source, "preserved drafts must override extracted prefill"
    assert 'value="${escapeHtml(draft.tenant_name || "")}"' in source, "tenant name input must render from preserved or extracted draft"
    assert "function hydrateMessage(message)" in source, "history messages must hydrate linked agent trace"
    assert "function isExplicitTranslationRequest(content)" in source, "translation playback must be gated by an explicit translation request"
    assert "function renderTranslationSpeechAction(message, index, displayContent)" in source, "translation responses need an on-demand playback action"
    assert 'if (!isExplicitTranslationRequest(requestContent)) return "";' in source, "inspection and ordinary assistant replies must not expose speech playback"
    assert 'locale: "fi-FI"' in source, "Finnish translations must request a Finnish browser voice"
    assert 'utterance.rate = 0.9;' in source, "learning playback should use a measured speaking rate"
    assert 'data-translation-speech' in source, "translation speech buttons need a stable delegated event target"
    assert "stopTranslationSpeech();\n  state.messages = [];" in source, "starting a new conversation must stop active speech"
    assert "delivery: message?.delivery || linked.delivery || null" in source, "history messages must retain delivery state"
    assert "function renderMessageDelivery(message)" in source, "failed requests must render beside the originating user message"
    assert "data.delivery && data.message" in source, "server delivery state must replace the optimistic user message"
    assert "state.messages.push({ sender: \"assistant\", content: message" not in source, "frontend must not fabricate assistant replies for request failures"
    assert "message-delivery-failure" in source, "delivery failures must use a durable chat card"
    assert "batchInspection" in source, "history messages must hydrate linked inspection batch artifacts"
    assert "inspection_batch_confirm" in source, "plan confirmation must preserve inspection batch context"
    assert "function renderBatchInspectionArtifact" in source, "frontend must render multi-store batch execution results"
    assert "BATCH_INSPECTION_EXECUTE" in source, "frontend must distinguish immediate multi-store batch plans"
    assert "batch_inspection.execute" in source, "frontend must preserve immediate batch tool context"
    assert "batch-run-thumbs" in source, "batch results must render per-store run evidence thumbnails"
    assert "item.runs" in source, "batch result renderer must consume hydrated child runs"
    assert ".batch-artifact-list" in source and ".batch-run-thumbs" in source, "batch image preview must support same-batch navigation"
    assert "expandedBatchEvidenceKeys" in source, "batch evidence expansion state must survive ordinary chat rerenders"
    assert "data-batch-evidence-toggle" in source, "+N batch evidence affordance must be an actionable button"
    assert "展开全部 ${orderedEvidence.length} 张快照" in source, "batch evidence affordance must disclose the complete snapshot count"
    assert "anomalyEvidenceCount" in source and "问题快照" in source, "batch cards must expose and prioritize flagged snapshots"
    assert "if (data.agent) linkedObject.agent = data.agent;" in source, "plan confirmation must keep returned agent trace"
    assert "renderMessageTrace(message)" in source, "assistant messages must expose execution trace"
    assert "conversationModeSwitch" in source, "composer must expose a reversible conversation-mode control"
    assert "mode_override: state.conversationMode" in source, "composer mode must be sent as an explicit route preference"
    assert "expandedTraceKeys" in source, "execution trace details must preserve user expanded state"
    assert "captureExpandedTraceState();" in source, "chat rerenders must capture expanded trace details"
    assert ".execution-trace-artifact[data-trace-key]" in source, "execution trace details need stable keys"
    assert "renderTraceNodes(state.lastAgent.trace)" in source, "right inspector must render real agent trace"
    assert "const inputValue = node.input || inferTraceInput(node);" in source, "trace nodes must show structured input with legacy fallback"
    assert "const reasoningValue = inferTraceReasoning(node);" in source, "trace nodes must show audit reasoning"
    assert "const outputValue = node.output || inferTraceOutput(node);" in source, "trace nodes must show structured output with legacy fallback"
    assert 'id="conversationHistoryClear"' in index_source
    assert "async function clearConversationHistory()" in source, "history panel must support one-click clearing"
    assert 'method: "DELETE"' in source and "conversation_ids" in source, "history clearing must call the bulk delete API"
    assert "visibleHistoryConversations()" in source, "history rendering and clearing must share one visible-history filter"
    assert 'id="agentCatalogNavItem"' in index_source, "sidebar must expose the Agent catalog entry"
    assert 'id="agentCatalogView"' in index_source, "Agent catalog page must exist"
    assert 'id="agentAddSkillBtn"' in index_source, "Agent catalog must expose a visible add Skill action"
    assert 'id="agentAddToolBtn"' in index_source, "Agent catalog must expose a visible add Tool action"
    assert "创建 Skill 能力" in index_source, "Skill creation action must clearly describe business capability creation"
    assert "注册工具调用" in index_source, "Tool creation action must clearly describe callable tool registration"
    assert 'id="agentManifestTab"' in index_source, "Manifest import tab must have a stable interaction target"
    assert 'id="agentManifestInput"' in index_source, "Agent manifest editor must exist"
    assert 'id="agentManifestCancel"' in index_source, "Agent manifest editor must expose an explicit cancel action"
    assert 'id="agentManifestPrompt"' in index_source, "Manifest import must support natural-language draft creation"
    assert 'id="agentManifestGenerateSkill"' in index_source, "Skill draft generation action must be visible"
    assert 'id="agentManifestGenerateTool"' in index_source, "Tool draft generation action must be visible"
    assert 'data-agent-catalog-mode="memories"' in index_source, "Agent catalog must expose long-term memory"
    assert 'data-agent-catalog-mode="knowledge"' in index_source, "Agent catalog must expose knowledge base"
    assert "async function loadAgentCatalog()" in source, "frontend must load the Agent catalog API"
    assert "function openAgentImportPanel" in source, "Manifest import tab must open the unified import panel"
    assert "function cancelAgentManifestEditor" in source, "Manifest editor must support returning without changing tabs manually"
    assert "cloneManifest(item?.manifest)" in source, "editing an imported manifest must use its original saved manifest"
    assert 'if (mode === "import")' in source, "Manifest tab click must be handled explicitly"
    assert "validateAgentManifestDraft" in source, "frontend must support manifest validation"
    assert "importAgentManifestDraft" in source, "frontend must support manifest import"
    assert "generateAgentManifestDraft" in source, "frontend must support natural-language manifest draft generation"
    assert "renderManifestDiagnostics" in source, "frontend must render localized manifest diagnostics"
    assert "function renderAgentCapabilityCard" in source, "Skill and Tool entries must share one card renderer"
    assert "function renderAgentCatalogDetail" in source, "Agent catalog items must expose details"
    assert "async function deleteAgentManifest" in source, "imported Skill/Tool manifests must support deletion"
    assert "async function createAgentMemory" in source, "frontend must support creating long-term memories"
    assert "async function createAgentKnowledge" in source, "frontend must support creating knowledge items"
    assert "async function uploadKnowledgeAsset" in source, "frontend must support local knowledge image uploads"
    assert "async function deleteAgentKnowledge" in source, "frontend must support deleting knowledge items"
    assert 'name="asset_file"' in source, "knowledge form must expose local file upload"
    assert 'minlength="2" required' in source, "knowledge title must have client-side validation"
    assert "asset_uploads: assetUploads" in source, "local knowledge uploads must be finalized with the knowledge item"
    assert "asset_metadata: assetMetadata" in source, "knowledge saves must include per-image SKU metadata"
    assert "function knowledgeReferenceAssets" in source, "knowledge UI must normalize per-image reference metadata"
    assert "function renderSkuMatchSummary" in source, "inspection results must expose a SKU hit summary"
    assert "以下标签已同步标注在对应巡检图片右上角" in source, "SKU hit summary must explain the image label"
    assert "本轮未命中任何受控 SKU" in source, "knowledge comparison results must explicitly disclose a no-hit outcome"
    assert "data-knowledge-upload-metadata" in source, "new uploads must expose per-image SKU fields"
    assert "data-knowledge-existing-asset-metadata" in source, "retained images must expose editable per-image SKU fields"
    assert 'data-metadata-field="sku"' in source and "required />" in source, "per-image SKU must be required in the knowledge UI"
    assert "视角（可选）" in source and "特征说明（可选）" in source, "view and description must remain optional"
    assert "每张样板图仅 SKU 为必填项" in source, "knowledge UI must explain the required-field rule"
    assert "function knowledgeTitlePlaceholder" in source, "knowledge title placeholder must use the active tenant and store"
    assert 'type="file" multiple' in source, "knowledge form must allow selecting multiple local images"
    assert "state.knowledgeUploadFiles" in source, "knowledge uploads must retain an accumulated local file queue"
    assert "function mergeKnowledgeUploadFiles" in source, "separate file selections must merge into one knowledge upload batch"
    assert "data-knowledge-upload-input" in source, "knowledge form must react when images are selected"
    assert "data-knowledge-upload-remove" in source, "knowledge form must let users remove individual queued images"
    assert "data-knowledge-upload-status" in source, "knowledge form must show the selected image count"
    assert "function knowledgeUploadPreviewUrl" in source, "queued knowledge images must expose preview URLs"
    assert "data-preview-title=\"预览知识库图片\"" in source, "knowledge image previews must use the shared preview dialog"
    assert "knowledge-asset-previews" in source, "saved knowledge assets must render preview thumbnails"
    assert "function startAgentKnowledgeEdit" in source, "knowledge rows must support entering an edit state"
    assert "data-agent-knowledge-edit" in source, "knowledge rows must expose an edit action"
    assert "existing_asset_urls: existingAssetUrls" in source, "knowledge edits must explicitly retain selected existing assets"
    assert 'method: editingKnowledgeId ? "PATCH" : "POST"' in source, "knowledge edits must call the PATCH endpoint"
    assert "function setKnowledgeUrlImportOpen" in source, "URL image import must support a controlled collapsed/open state"
    assert "data-knowledge-url-import-toggle" in source, "knowledge form must expose a URL import toggle in both create and edit states"
    assert "data-knowledge-url-import-status" in source, "URL import must explain whether an address will be saved"
    assert "SKU 为必填项；可填写下方 SKU，或使用表单顶部的默认 SKU" in source, "URL image SKU guidance must be explicit"
    assert "renderAgentCatalog({ preserveContent: true })" in source, "global renders must preserve active Agent form content"
    assert "const canPreserveContent" in source, "Agent catalog must avoid replacing an active form during background updates"
    skill_render = source[source.index("function renderAgentSkills") : source.index("function renderAgentTools")]
    tool_render = source[source.index("function renderAgentTools") : source.index("function renderAgentIntents")]
    assert "data-agent-new-skill" not in skill_render, "Skill tab must not duplicate the global add action"
    assert "data-agent-new-tool" not in tool_render, "Tool tab must not duplicate the global add action"
    assert "renderAgentCapabilityCard(\"skill\", skill, \"extension\")" in skill_render, "imported Skills must use the same card layout as builtin Skills"
    assert "renderAgentCapabilityCard(\"tool\", tool, \"extension\")" in tool_render, "imported Tools must use the same card layout as builtin Tools"
    assert "agent-extension-section" not in skill_render, "Skill tab should not render imported items as a separate visual format"
    assert "agent-extension-section" not in tool_render, "Tool tab should not render imported items as a separate visual format"
    assert "data-agent-view-detail" in source, "Skill/Tool cards must expose detail entry points"
    assert "data-agent-manifest-delete" in source, "imported Skill/Tool cards must expose delete action"
    assert '"/api/agent/catalog"' in source, "frontend must call the catalog API"
    assert '"/api/agent/manifests/validate"' in source, "frontend must call the manifest validation API"
    assert '"/api/agent/manifests/draft"' in source, "frontend must call the natural-language draft API"
    assert '"/api/agent/manifests"' in source, "frontend must call the manifest import API"
    assert "/api/agent/manifests/${encodeURIComponent(manifestId)}" in source, "frontend must call the manifest delete API"
    assert '"/api/agent/memories"' in source, "frontend must call the long-term memory API"
    assert '"/api/agent/knowledge"' in source, "frontend must call the knowledge base API"
    assert "function renderGeneratedDocumentArtifact" in source, "open QA must render generated PDF artifacts"
    assert "data-document-download" in source, "generated documents must expose a download action"
    assert "async function downloadGeneratedDocument" in source, "document downloads must include authenticated request headers"
    assert "renderAssistantMessageContent" in source, "open-QA markdown emphasis must render without changing inspection messages"


def check_agent_core_contracts():
    catalog = standard_agent_catalog()
    route = catalog.route("ANALYZE_VISUAL")
    assert route.intent == "ANALYZE_VISUAL"
    assert route.skill and route.skill.name == "visual_scene_inspection"
    assert route.tool and route.tool.name == "vlm.image.inspect"
    assert "CAPTURE_SNAPSHOT" in route.similar_intents

    scheduled = catalog.route("CREATE_SCHEDULED_INSPECTION")
    assert scheduled.skill and scheduled.skill.risk == "HIGH_WRITE"
    assert "effective_time_range" in scheduled.required_slots
    assert scheduled.tool and scheduled.tool.name == "scheduler.inspection.create"
    assert catalog.tools.get("event.emit")
    assert catalog.tools.get("evidence.archive")
    assert catalog.tools.get("knowledge.retrieve"), "knowledge recall tool must be registered for imported compliance skills"
    assert catalog.tools.get("memory.retrieve"), "long-term memory recall tool must be registered for imported compliance skills"
    assert catalog.tools.get("document.generate_pdf"), "PDF generation must be registered as a standard runtime tool"

    catalog.intents.associate("ANALYZE_VISUAL", "CUSTOM_VISUAL_CHECK")
    assert "CUSTOM_VISUAL_CHECK" in catalog.route("ANALYZE_VISUAL").similar_intents
    catalog.register_tool(ToolDefinition(name="third.party.echo", label="第三方 Echo 工具", source="third_party"))
    catalog.register_skill(
        SkillDefinition(
            name="third_party_echo_skill",
            label="第三方 Echo Skill",
            intent="HELP",
            default_tool="third.party.echo",
            source="third_party",
        )
    )
    assert catalog.tools.get("third.party.echo")

    legacy = public_skill_catalog()
    assert any(item["intent"] == "ANALYZE_VISUAL" and item["skill"] == "visual_scene_inspection" for item in legacy)
    manifest = public_agent_catalog()
    assert manifest["version"] == "agent-core-v1"
    assert {"input", "intent", "skill", "toolbox", "execution", "output"} <= set(manifest["layers"])

    valid_skill = validate_agent_manifest(
        {
            "kind": "skill",
            "schema_version": "skill.v1",
            "metadata": {"name": "smoke.skill", "label": "Smoke Skill", "version": "1.0.0"},
            "intent": {"name": "SMOKE_SKILL", "aliases": ["smoke skill"]},
            "slots": {"required": ["org_scope"]},
            "execution": {"steps": [{"tool": "paas.media.snapshot"}]},
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        }
    )
    assert valid_skill["ok"] is True
    assert valid_skill["normalized"]["runtime_status"] == "callable"
    unknown_tool_skill = validate_agent_manifest(
        {
            "kind": "skill",
            "schema_version": "skill.v1",
            "metadata": {"name": "smoke.unknown_tool", "label": "Smoke Unknown Tool"},
            "intent": {"name": "SMOKE_UNKNOWN_TOOL", "aliases": ["unknown"]},
            "execution": {"steps": [{"tool": "missing.tool"}]},
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        },
        known_tools={"paas.media.snapshot"},
    )
    assert unknown_tool_skill["ok"] is False
    assert any("unknown tool" in error for error in unknown_tool_skill["errors"])
    builtin_intent_skill = validate_agent_manifest(
        {
            "kind": "skill",
            "schema_version": "skill.v1",
            "metadata": {"name": "smoke.intent_conflict", "label": "Smoke Intent Conflict"},
            "intent": {"name": "ANALYZE_VISUAL", "aliases": ["conflict"]},
            "execution": {"steps": [{"tool": "paas.media.snapshot"}]},
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        },
        known_tools={"paas.media.snapshot"},
        builtin_intents={"ANALYZE_VISUAL"},
    )
    assert builtin_intent_skill["ok"] is False
    assert any("conflicts with builtin intent" in error for error in builtin_intent_skill["errors"])
    invalid_tool = validate_agent_manifest(
        {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {"name": "unsafe.tool", "label": "Unsafe Tool"},
            "runtime": {"type": "http", "endpoint": "https://example.invalid", "auth": {"api_key": "raw"}},
            "input_schema": {},
            "output_schema": {},
            "risk": {"level": "HIGH_WRITE", "confirm_required": True},
        }
    )
    assert invalid_tool["ok"] is False
    assert any("raw api_key" in error for error in invalid_tool["errors"])
    assert any("real callable endpoint" in error for error in invalid_tool["errors"])
    placeholder_tool = validate_agent_manifest(
        {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {"name": "placeholder.tool", "label": "Placeholder Tool"},
            "runtime": {"type": "http", "endpoint": "https://example.com/api/replace-me", "auth": {"credential_ref": "external_api_token"}},
            "input_schema": {},
            "output_schema": {},
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        }
    )
    assert placeholder_tool["ok"] is False
    assert any("real callable endpoint" in error for error in placeholder_tool["errors"])
    callable_builtin_tool = validate_agent_manifest(
        {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {"name": "camera.snapshot.proxy", "label": "Camera Snapshot Proxy"},
            "runtime": {"type": "builtin", "handler": "paas.media.snapshot"},
            "input_schema": {"type": "object", "required": ["camera_id"]},
            "output_schema": {"type": "object", "required": ["snapshot_url"]},
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        }
    )
    assert callable_builtin_tool["ok"] is True
    assert callable_builtin_tool["normalized"]["runtime_status"] == "callable"
    top_level_secret = validate_agent_manifest(
        {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {"name": "unsafe.top_auth", "label": "Unsafe Top Auth"},
            "auth": {"api_key": "raw"},
            "runtime": {"type": "http", "endpoint": "https://example.invalid"},
            "input_schema": {},
            "output_schema": {},
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        }
    )
    assert top_level_secret["ok"] is False
    assert any("tool.auth is not supported" in error for error in top_level_secret["errors"])
    assert any("raw secret" in error for error in top_level_secret["errors"])
    high_write_without_confirmation = validate_agent_manifest(
        {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {"name": "unsafe.write", "label": "Unsafe Write"},
            "runtime": {"type": "http", "endpoint": "https://example.invalid"},
            "input_schema": {},
            "output_schema": {},
            "risk": {"level": "HIGH_WRITE", "confirm_required": False},
        }
    )
    assert high_write_without_confirmation["ok"] is False
    assert any("confirm_required=true" in error for error in high_write_without_confirmation["errors"])


def check_slot_parsing_contracts():
    reference = date(2026, 7, 13)
    assert parse_duration_days("到7月底", reference) == 19
    assert parse_duration_days("截止到7月31日", reference) == 19
    assert parse_duration_days("为期2周", reference) == 14
    assert parse_duration_days("为期两周", reference) == 14
    assert parse_duration_days("半个月", reference) == 15
    assert parse_duration_days("到下月底", reference) == 50


def check_visual_reasoner_contracts():
    normalized = VisualReasoner.apply_business_policy(
        "检查门店地面是否存在垃圾或杂物，排除地贴、固定标识、家具和正常堆放物。",
        {
            "business_policy": "PROHIBITED_CONDITION",
            "target_observed": True,
            "status": "POSITIVE",
            "conclusion": "地面存在散落的白色衣物，属于垃圾，违反门店清洁标准。",
            "confidence": 0.98,
            "observations": ["地面可见散落的白色衣物，疑似垃圾。"],
        },
    )
    assert normalized["status"] == "POSITIVE"
    assert "衣物" not in normalized["conclusion"]
    assert all("衣物" not in item for item in normalized["observations"])
    assert "布状物" in normalized["conclusion"]
    assert normalized["confidence"] == 0.90

    reasoner = VisualReasoner({"api_key": "test", "model": "Qwen3-VL-8B-Instruct-FP8"})
    result = reasoner._normalize_result(
        "检查门店地面是否存在垃圾或杂物",
        [{"camera_name": "展厅5", "snapshot_url": "data:image/jpeg;base64,AA=="}],
        {
            "business_policy": "PROHIBITED_CONDITION",
            "target_observed": True,
            "status": "POSITIVE",
            "conclusion": "地面存在散落的白色衣物，属于垃圾，违反门店清洁标准。",
            "confidence": 0.98,
            "observations": ["展厅5左下角存在散落的白色衣物。"],
        },
        0,
    )
    assert result["model_raw_output"]["conclusion"] == "地面存在散落的白色衣物，属于垃圾，违反门店清洁标准。"
    assert "衣物" not in result["conclusion"]
    assert result["model"] == "Qwen3-VL-8B-Instruct-FP8"

    candidate_calls = []
    multi_reasoner = VisualReasoner(
        {"api_key": "test", "model": "smoke-vlm", "max_images": 8, "max_candidate_images": 24}
    )
    one_pixel_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    references = [
        {"knowledge_title": "门店出样样板图", "snapshot_url": one_pixel_png}
        for _ in range(5)
    ]

    def fake_candidate_request(system, content, max_tokens=512):
        if isinstance(content, list):
            image_count = len([item for item in content if item.get("type") == "image_url"])
            assert image_count == 1, "当前网关只应接收一张本地合成的参考图与现场图拼图"
            candidate_calls.append(content)
            return {
                "relevance": 0.8,
                "business_policy": "OBSERVATION_ONLY",
                "status": "NEGATIVE",
                "conclusion": "现场画面与样板图一致。",
                "confidence": 0.9,
                "observations": [],
                "exclusions": [],
            }
        return {
            "business_policy": "OBSERVATION_ONLY",
            "status": "NEGATIVE",
            "conclusion": "已完成全部候选画面的样板比对。",
            "confidence": 0.9,
            "selected_camera_names": ["展厅17"],
            "observations": [],
            "exclusions": [],
        }

    multi_reasoner._request_json = fake_candidate_request
    multi_result = multi_reasoner.analyze(
        "检查所有镜头画面中的家具是否符合出样样板图",
        [
            {"camera_name": f"展厅{index}", "snapshot_url": one_pixel_png}
            for index in range(1, 18)
        ],
        references,
    )
    assert multi_result["image_count"] == 17
    assert len(candidate_calls) == 17
    assert len(multi_result["candidate_model_outputs"]) == 17

    rejected_reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})

    def rejected_request(*_args, **_kwargs):
        raise OnlineAgentError("VLM_REQUEST_REJECTED", "视觉分析服务拒绝本次请求（HTTP 413）", {"http_status": 413})

    rejected_reasoner._request_json = rejected_request
    try:
        rejected_reasoner.analyze(
            "检查样板图",
            [
                {"camera_name": "展厅1", "snapshot_url": "data:image/jpeg;base64,AA=="},
                {"camera_name": "展厅2", "snapshot_url": "data:image/jpeg;base64,AA=="},
            ],
        )
        raise AssertionError("所有候选请求被拒绝时必须返回可诊断错误")
    except OnlineAgentError as exc:
        assert exc.code == "VLM_CANDIDATE_ANALYSIS_FAILED"
        assert exc.detail["attempted_image_count"] == 2
        assert exc.detail["http_status"] == 413

    legacy_trace = {
        "nodes": [
            {
                "node_id": "tool_4",
                "output": {"failed_image_count": 1},
            },
            {
                "node_id": "model_output",
                "detail": {
                    "candidate_outputs": [
                        {"camera_name": "展厅1"},
                        {"camera_name": "展厅2"},
                    ]
                },
            },
        ]
    }
    assert server.failed_camera_names_from_run_trace(
        {"trace_json": json.dumps(legacy_trace, ensure_ascii=False)},
        [{"camera_name": "展厅1"}, {"camera_name": "展厅2"}, {"camera_name": "展厅3"}],
    ) == ["展厅3"]
    assert "未被判定为风险" in server.visual_analysis_partial_note({"failed_camera_names": ["展厅3"]})
    legacy_sku_trace = {
        "nodes": [
            {"node_id": "tool_4", "input": {"question": "按镜头执行 SKU 比对"}},
            {
                "node_id": "model_output",
                "detail": {
                    "candidate_outputs": [
                        {
                            "camera_name": "展厅1",
                            "output": {"relevance": 1, "target_observed": True, "status": "POSITIVE", "matched_skus": []},
                        },
                        {
                            "camera_name": "展厅2",
                            "output": {"relevance": 1, "target_observed": True, "status": "NEGATIVE", "matched_skus": ["SMOKE-001"]},
                        },
                    ]
                },
            },
        ]
    }
    assert server.sku_risk_camera_names_from_run_trace(
        {"trace_json": json.dumps(legacy_sku_trace, ensure_ascii=False)},
        [{"camera_name": "展厅1"}, {"camera_name": "展厅2"}, {"camera_name": "展厅3"}],
    ) == ["展厅1"]


def check_inspection_knowledge_retrieval_contracts():
    original_db_path = server.DB_PATH
    original_upload_dir = server.KNOWLEDGE_UPLOAD_DIR
    tmp_dir = tempfile.TemporaryDirectory()
    one_pixel_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    try:
        server.DB_PATH = Path(tmp_dir.name) / "knowledge-retrieval.db"
        server.KNOWLEDGE_UPLOAD_DIR = Path(tmp_dir.name) / "knowledge_uploads"
        server.init_db(reset=True)
        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            try:
                server.create_agent_knowledge_item(
                    conn,
                    user,
                    {
                        "title": "missing image sku",
                        "knowledge_type": "brand_standard",
                        "modality": "image",
                        "content_text": "图片型知识必须有单图 SKU。",
                        "asset_uploads": [{"filename": "missing-sku.png", "data_url": one_pixel_png}],
                    },
                )
                raise AssertionError("image knowledge without SKU must be rejected")
            except server.ApiError as exc:
                assert exc.code == "AGENT_KNOWLEDGE_INVALID"
                assert "必须填写 SKU" in exc.detail["message"]
            sku_only_knowledge = server.create_agent_knowledge_item(
                conn,
                user,
                {
                    "title": "SKU only image metadata",
                    "knowledge_type": "brand_standard",
                    "modality": "image",
                    "content_text": "图片型知识允许省略视角与特征说明。",
                    "asset_uploads": [{"filename": "sku-only.png", "data_url": one_pixel_png}],
                    "asset_metadata": [{"upload_index": 0, "sku": "SKU-ONLY-001"}],
                },
            )
            assert sku_only_knowledge["reference_assets"] == [
                {
                    "asset_url": sku_only_knowledge["asset_url"],
                    "sku": "SKU-ONLY-001",
                    "description": "",
                    "view_tag": "",
                }
            ]
            knowledge = server.create_agent_knowledge_item(
                conn,
                user,
                {
                    "title": "Smoke 出样样板图",
                    "sku": "smoke-001",
                    "knowledge_type": "brand_standard",
                    "modality": "image",
                    "content_text": "用于比对门店出样家具是否符合标准。",
                    "tags": "出样,家具,样板",
                    "asset_uploads": [
                        {"filename": "reference-one.png", "data_url": one_pixel_png},
                        {"filename": "reference-two.png", "data_url": one_pixel_png},
                    ],
                    "asset_metadata": [
                        {"upload_index": 0, "sku": "smoke-001", "view_tag": "正面", "description": "浅色双人沙发，带左扶手"},
                        {"upload_index": 1, "sku": "smoke-002", "view_tag": "左侧", "description": "同系列沙发的左侧展示图"},
                    ],
                },
            )
            query = "检查所有出样家具是否和“Smoke 出样样板图”这个知识库一致，不一致标出违规"
            hits = server.retrieve_agent_knowledge(conn, user, query)
            assert [item["knowledge_id"] for item in hits] == [knowledge["knowledge_id"]]
            assert hits[0]["sku"] == "SMOKE-001"
            assert len(hits[0]["asset_urls"]) == 2
            assert [asset["sku"] for asset in hits[0]["reference_assets"]] == ["SMOKE-001", "SMOKE-002"]
            assert hits[0]["reference_assets"][0]["view_tag"] == "正面"
            references = server.inspection_reference_images(user["tenant_id"], hits)
            assert len(references) == 2
            assert [item["sku"] for item in references] == ["SMOKE-001", "SMOKE-002"]
            assert references[0]["description"] == "浅色双人沙发，带左扶手"
            assert all(item["snapshot_url"].startswith("data:image/jpeg;base64,") for item in references)
            assert all(item["prepared_bytes"] <= server.MAX_INSPECTION_REFERENCE_IMAGE_BYTES for item in references)
            legacy_task = {
                "task_id": "legacy_knowledge_task",
                "tenant_id": user["tenant_id"],
                "thresholds": json.dumps({"confidence": 0.8}),
            }
            backfilled_hits = server.resolve_inspection_knowledge_context(conn, legacy_task, query)
            assert backfilled_hits[0]["knowledge_id"] == knowledge["knowledge_id"]
            assert legacy_task["thresholds"]["knowledge_context"][0]["title"] == "Smoke 出样样板图"
            assert legacy_task["thresholds"]["knowledge_context"][0]["knowledge_updated_at"] == knowledge["updated_at"]
            stale_task = {
                "task_id": "stale_knowledge_task",
                "tenant_id": user["tenant_id"],
                "thresholds": {
                    "confidence": 0.8,
                    "knowledge_context": [
                        {
                            **hits[0],
                            "knowledge_updated_at": "2000-01-01T00:00:00+08:00",
                            "reference_assets": [
                                {**asset, "sku": ""}
                                for asset in hits[0]["reference_assets"]
                            ],
                        }
                    ],
                },
            }
            refreshed_hits = server.resolve_inspection_knowledge_context(conn, stale_task, query)
            assert [asset["sku"] for asset in refreshed_hits[0]["reference_assets"]] == ["SMOKE-001", "SMOKE-002"]
            assert stale_task["thresholds"]["knowledge_context"][0]["knowledge_updated_at"] == knowledge["updated_at"]
            model_question = server.inspection_question_with_knowledge(query, hits)
            assert "不得以“缺少样板图/比对依据”为由跳过" in model_question
            assert "样板 SKU：SMOKE-001、SMOKE-002" in model_question
            assert server.validate_agent_knowledge_payload(
                {
                    "title": "bad metadata",
                    "content_text": "valid text",
                    "asset_uploads": [{"filename": "reference-one.png", "data_url": one_pixel_png}],
                    "asset_metadata": [{"upload_index": 1, "sku": "SMOKE-001"}],
                }
            )["ok"] is False
            assert server.validate_agent_knowledge_payload(
                {
                    "title": "中文 SKU 标签",
                    "sku": "圣洁白+赤褐色",
                    "content_text": "中文型号与色号可作为受控图片标签。",
                }
            )["ok"] is True

        captured = {}
        reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})
        assert reasoner._allowed_reference_skus([{"sku": "松果棕"}]) == {"松果棕"}

        def fake_request(system, content, max_tokens=512):
            captured["system"] = system
            captured["content"] = content
            return {
                "business_policy": "OBSERVATION_ONLY",
                "status": "NEGATIVE",
                "conclusion": "现场出样与样板图一致。",
                "confidence": 0.95,
                "selected_camera_names": ["门店全景"],
                "matched_skus": ["SMOKE-001", "NOT-IN-KNOWLEDGE"],
                "observations": ["已完成样板图与现场画面的比对。"],
                "exclusions": [],
            }

        reasoner._request_json = fake_request
        result = reasoner.analyze(
            "检查出样家具是否符合知识库样板图",
            [{"camera_name": "门店全景", "snapshot_url": one_pixel_png}],
            references,
        )
        assert result["reference_image_count"] == 2
        assert result["reference_knowledge_titles"] == ["Smoke 出样样板图"]
        assert result["sku_matches"] == [{"camera_name": "门店全景", "sku": "SMOKE-001"}]
        assert result["status"] == "NEGATIVE"
        assert result["anomaly_camera_names"] == []
        assert result["sku_comparison"]["matched_camera_names"] == ["门店全景"]
        assert "不能声称没有样板图或比对依据" in captured["system"]
        assert "右上角" in captured["system"] and "非风险" in captured["system"]
        assert "特征：浅色双人沙发，带左扶手" in str(captured["content"])
        assert len([item for item in captured["content"] if item.get("type") == "image_url"]) == 1

        multi_reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})

        def multi_camera_request(system, content, max_tokens=512):
            if "候选镜头分析器" in system:
                is_hit_camera = "· 命中镜头" in str(content)
                return {
                    "relevance": 1.0,
                    "business_policy": "OBSERVATION_ONLY",
                    "status": "POSITIVE" if is_hit_camera else "NEGATIVE",
                    "target_observed": True,
                    "evidence_type": "DIRECT_ACTION",
                    "conclusion": "模型的通用状态不应覆盖 SKU 比对规则。",
                    "confidence": 0.95,
                    "observations": [],
                    "exclusions": [],
                    "matched_skus": ["SMOKE-001"] if is_hit_camera else [],
                }
            return {
                "business_policy": "OBSERVATION_ONLY",
                "status": "NEGATIVE",
                "conclusion": "汇总模型的通用状态不应覆盖 SKU 比对规则。",
                "confidence": 0.95,
                "selected_camera_names": ["命中镜头", "未命中镜头"],
                "observations": [],
                "exclusions": [],
            }

        multi_reasoner._request_json = multi_camera_request
        mixed_sku_result = multi_reasoner.analyze(
            "检查出样家具是否符合知识库样板图",
            [
                {"camera_name": "命中镜头", "snapshot_url": one_pixel_png},
                {"camera_name": "未命中镜头", "snapshot_url": one_pixel_png},
            ],
            references,
        )
        assert mixed_sku_result["status"] == "POSITIVE"
        assert mixed_sku_result["anomaly_camera_names"] == ["未命中镜头"]
        assert mixed_sku_result["sku_matches"] == [{"camera_name": "命中镜头", "sku": "SMOKE-001"}]
        assert mixed_sku_result["sku_comparison"]["matched_camera_names"] == ["命中镜头"]
        assert mixed_sku_result["sku_comparison"]["risk_camera_names"] == ["未命中镜头"]
        assert "未命中任何库内 SKU" in mixed_sku_result["conclusion"]

        # A model request budget must control only the batch size.  Seven camera
        # frames with a two-frame batch budget must all receive a final SKU/risk
        # outcome; the transient failure on 展厅4 must be retried rather than
        # silently disappearing from the result.
        adaptive_reasoner = VisualReasoner(
            {
                "api_key": "test",
                "model": "smoke-vlm",
                "max_images": 2,
                "candidate_batch_size": 2,
            }
        )
        candidate_attempts = {}

        def adaptive_request(system, content, max_tokens=512):
            content_text = str(content)
            camera_name = next(
                (f"展厅{index}" for index in range(1, 8) if f"展厅{index}" in content_text),
                "",
            )
            if "候选镜头分析器" in system:
                candidate_attempts[camera_name] = candidate_attempts.get(camera_name, 0) + 1
                if camera_name == "展厅4" and candidate_attempts[camera_name] == 1:
                    raise OnlineAgentError("VLM_UNAVAILABLE", "候选镜头瞬时超时")
                return {
                    "relevance": 1.0,
                    "business_policy": "OBSERVATION_ONLY",
                    "status": "NEGATIVE",
                    "target_observed": True,
                    "conclusion": "已完成单镜头 SKU 比对。",
                    "confidence": 0.95,
                    "observations": [],
                    "exclusions": [],
                    "matched_skus": ["SMOKE-001"] if camera_name == "展厅2" else [],
                }
            if "视觉判断器" in system:
                return {
                    "business_policy": "OBSERVATION_ONLY",
                    "status": "NEGATIVE",
                    "target_observed": True,
                    "conclusion": "已完成单镜头 SKU 比对。",
                    "confidence": 0.95,
                    "selected_camera_names": [camera_name],
                    "matched_skus": [],
                    "observations": [],
                    "exclusions": [],
                }
            return {
                "business_policy": "OBSERVATION_ONLY",
                "status": "NEGATIVE",
                "conclusion": "已完成候选批次汇总。",
                "confidence": 0.95,
                "selected_camera_names": ["展厅1", "展厅2"],
                "observations": [],
                "exclusions": [],
            }

        adaptive_reasoner._request_json = adaptive_request
        adaptive_result = adaptive_reasoner.analyze(
            "检查出样家具是否符合知识库样板图",
            [{"camera_name": f"展厅{index}", "snapshot_url": one_pixel_png} for index in range(1, 8)],
            references,
        )
        assert adaptive_result["image_count"] == 7
        assert adaptive_result["batch_count"] == 4
        assert adaptive_result["candidate_batch_size"] == 2
        assert adaptive_result["failed_image_count"] == 0
        assert candidate_attempts["展厅4"] == 2
        assert adaptive_result["sku_matches"] == [{"camera_name": "展厅2", "sku": "SMOKE-001"}]
        assert adaptive_result["anomaly_camera_names"] == ["展厅1", "展厅3", "展厅4", "展厅5", "展厅6", "展厅7"]
        assert adaptive_result["sku_comparison"]["matched_camera_names"] == ["展厅2"]
        assert adaptive_result["sku_comparison"]["risk_camera_names"] == adaptive_result["anomaly_camera_names"]

        assert server.Image is not None
        original = server.Image.effect_noise((2200, 1600), 120).convert("RGB")
        source = BytesIO()
        original.save(source, format="JPEG", quality=98)
        assert len(source.getvalue()) > server.MAX_INSPECTION_REFERENCE_IMAGE_BYTES
        prepared = server.prepare_inspection_reference_image(source.getvalue())
        assert prepared is not None
        prepared_bytes, mime_type = prepared
        assert mime_type == "image/jpeg"
        assert len(prepared_bytes) <= server.MAX_INSPECTION_REFERENCE_IMAGE_BYTES
        with server.Image.open(BytesIO(prepared_bytes)) as compressed:
            assert max(compressed.size) <= server.MAX_INSPECTION_REFERENCE_EDGE
    finally:
        server.DB_PATH = original_db_path
        server.KNOWLEDGE_UPLOAD_DIR = original_upload_dir
        tmp_dir.cleanup()


def check_comparison_p0_p1_contracts():
    """Exercise the v1.3 catalog/slot/window fail-safe path without external OVD."""
    original_db_path = server.DB_PATH
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        try:
            validate_ovd_endpoint(
                OvdAdapterConfig(
                    endpoint="http://127.0.0.1/ovd",
                    authorization="test-token",
                    client_id="comparison-smoke",
                    allowed_hosts=frozenset({"127.0.0.1"}),
                ),
                resolve_dns=False,
            )
            raise AssertionError("non-HTTPS OVD endpoint must be rejected")
        except OvdAdapterFailure as exc:
            assert exc.code == "OVD_ENDPOINT_REJECTED"
        server.DB_PATH = Path(tmp_dir.name) / "comparison-p0-p1.db"
        server.init_db(reset=True)
        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            reviewer = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_system'", ()))
            version = server.create_catalog_version(conn, user, {"change_summary": "comparison smoke catalog"})
            created = server.create_catalog_sku(
                conn,
                user,
                {
                    "catalog_version_id": version["version_id"],
                    "sku_id": "CHAIR-001",
                    "canonical_name": "深象单人沙发椅",
                    "display_name": "沙发椅 001",
                    "brand": "DeepVision",
                    "family_id": "CHAIR",
                    "aliases": ["单人椅", "展示椅"],
                    "external_codes": [{"code_type": "BARCODE", "code_value": "690000000001", "source_system": "PIM"}],
                },
            )
            sku = created["sku"]
            try:
                server.update_catalog_sku(conn, user, sku["sku_item_id"], {"display_name": "并发修改"}, "bad-etag")
                raise AssertionError("catalog SKU must require matching ETag")
            except server.ApiError as exc:
                assert exc.code == "CATALOG_VERSION_CONFLICT"
            try:
                server.approve_catalog_version(conn, user, version["version_id"])
                raise AssertionError("catalog creator must not self-approve")
            except server.ApiError as exc:
                assert exc.code == "PERMISSION_DENIED"
            approved_catalog = server.approve_catalog_version(conn, reviewer, version["version_id"])
            assert approved_catalog["catalog_version"]["state"] == "PENDING_APPROVAL"
            published_catalog = server.publish_catalog_version(conn, user, version["version_id"])
            assert published_catalog["catalog_version"]["state"] == "PUBLISHED"

            profile = server.create_domain_profile(
                conn,
                user,
                {
                    "name": "家具固定镜头 POC",
                    "domain": "furniture",
                    "capture_mode": "FIXED_CAMERA",
                    "identity_policy": {"priority": ["visual_embedding", "local_match"], "ovd_prompts": ["chair"]},
                    "quality_bundle": {"quality_threshold": 0.7},
                },
            )
            profile = server.approve_domain_profile(conn, user, profile["profile_id"])
            assert profile["status"] == "ACTIVE"
            calibration = server.create_calibration_profile(
                conn,
                user,
                {"camera_id": "cam_gz_1", "version": "cal-v1", "roi": [[0, 0], [1, 0], [1, 1], [0, 1]], "health_state": "GREEN"},
            )
            calibration = server.approve_calibration_profile(conn, user, calibration["calibration_id"])
            assert calibration["status"] == "ACTIVE"
            asset = server.create_reference_asset(
                conn,
                user,
                {"catalog_version_id": version["version_id"], "sku_id": "CHAIR-001", "asset_url": "/static/evidence/ev-10231.svg", "view_tag": "front", "feature_version": "dino-v1"},
            )
            asset = server.approve_reference_asset(conn, user, asset["asset_id"])
            assert asset["approval_status"] == "APPROVED"
            slot = server.create_display_slot(
                conn,
                user,
                {
                    "org_id": "org_gz",
                    "camera_id": "cam_gz_1",
                    "domain_profile_id": profile["profile_id"],
                    "catalog_version_id": version["version_id"],
                    "calibration_version": "cal-v1",
                    "zone_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "expected_skus": ["CHAIR-001"],
                    "expected_count": 1,
                    "min_valid_frames": 3,
                    "quality_threshold": 0.7,
                    "min_roi_coverage": 0.8,
                    "max_occlusion": 0.2,
                    "automation_enabled": True,
                },
            )
            slot = server.approve_display_slot(conn, user, slot["slot_id"])
            assert slot["status"] == "ACTIVE"

            session = server.create_comparison_session(
                conn,
                user,
                {
                    "camera_id": "cam_gz_1",
                    "capture_mode": "FIXED_CAMERA",
                    "domain_profile_id": profile["profile_id"],
                    "catalog_version_id": version["version_id"],
                    "calibration_version": "cal-v1",
                    "display_slot_ids": [slot["slot_id"]],
                    "evidence_refs": ["test-session"],
                    "idempotency_key": "comparison-smoke-1",
                },
            )
            repeated = server.create_comparison_session(
                conn,
                user,
                {
                    "camera_id": "cam_gz_1",
                    "capture_mode": "FIXED_CAMERA",
                    "domain_profile_id": profile["profile_id"],
                    "catalog_version_id": version["version_id"],
                    "calibration_version": "cal-v1",
                    "display_slot_ids": [slot["slot_id"]],
                    "evidence_refs": ["test-session"],
                    "idempotency_key": "comparison-smoke-1",
                },
            )
            assert repeated["session_id"] == session["session_id"]
            for index in range(3):
                frame = server._record_comparison_frame(
                    conn,
                    user,
                    session["session_id"],
                    {
                        "evidence_sha256": f"{index + 1:064x}",
                        "captured_at": f"2026-07-31T11:00:0{index}+08:00",
                        "state": "EVIDENCE_READY",
                        "quality_score": 0.95,
                        "roi_coverage": 0.98,
                        "occlusion_ratio": 0.01,
                        "camera_health": "GREEN",
                        "object_evidence": [{"state": "MATCHED", "sku_id": "CHAIR-001", "track_id": "chair-track"}],
                    },
                    internal_worker=True,
                )
                assert frame["state"] == "EVIDENCE_READY"
            decisions = server.refresh_comparison_slot_decisions(conn, user, session["session_id"])
            assert decisions[0]["state"] == "COMPLIANT", decisions
            assert "OVD_AUTHORIZATION" not in json.dumps(decisions[0]["run_snapshot"])
            review = server.create_comparison_review(conn, user, decisions[0]["slot_decision_id"], {"decision": "CONFIRMED", "reason": "人工复核确认样板与槽位一致", "evidence_refs": ["test-session"]})
            assert review["decision"] == "CONFIRMED"

            incomplete = server.create_comparison_session(
                conn,
                user,
                {
                    "camera_id": "cam_gz_1",
                    "capture_mode": "FIXED_CAMERA",
                    "domain_profile_id": profile["profile_id"],
                    "catalog_version_id": version["version_id"],
                    "calibration_version": "cal-v1",
                    "display_slot_ids": [slot["slot_id"]],
                    "idempotency_key": "comparison-smoke-incomplete",
                },
            )
            assert server.refresh_comparison_slot_decisions(conn, user, incomplete["session_id"])[0]["state"] == "INCONCLUSIVE"
            failed = server.create_comparison_session(
                conn,
                user,
                {
                    "camera_id": "cam_gz_1",
                    "capture_mode": "FIXED_CAMERA",
                    "domain_profile_id": profile["profile_id"],
                    "catalog_version_id": version["version_id"],
                    "calibration_version": "cal-v1",
                    "display_slot_ids": [slot["slot_id"]],
                    "idempotency_key": "comparison-smoke-failed",
                },
            )
            server._record_comparison_frame(
                conn,
                user,
                failed["session_id"],
                {"evidence_sha256": "f" * 64, "captured_at": "2026-07-31T11:01:00+08:00", "state": "SYSTEM_FAILED", "quality_score": 0, "roi_coverage": 0, "occlusion_ratio": 1, "camera_health": "RED", "reason_codes": ["OVD_NOT_CONFIGURED"]},
                internal_worker=True,
            )
            assert server.refresh_comparison_slot_decisions(conn, user, failed["session_id"])[0]["state"] == "SYSTEM_FAILED"

            good_contract = server.ovd_contract_report(
                {"requestID": "req-1", "modelVersion": "ovd-v1", "imageWidth": 100, "imageHeight": 80, "detections": [{"id": "d-1", "className": "chair", "score": 0.9, "bbox_xyxy": [1, 2, 80, 70]}]},
                ["chair"],
            )
            assert good_contract["ok"] is True and good_contract["coordinate_system"] == "pixel_xyxy"
            bad_contract = server.ovd_contract_report({"detections": []}, ["chair"])
            assert bad_contract["ok"] is False and bad_contract["code"] == "OVD_INVALID_SCHEMA"
            conn.commit()
    finally:
        server.DB_PATH = original_db_path
        tmp_dir.cleanup()


def check_fixed_daily_scheduling_and_sku_artifact_contracts():
    original_db_path = server.DB_PATH
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        server.DB_PATH = Path(tmp_dir.name) / "schedule-sku-contracts.db"
        server.init_db(reset=True)
        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            plan = {"plan_id": "plan_schedule_sku", "conversation_id": "conv_schedule_sku"}
            params = {
                "org_id": "org_gz",
                "org_name": "广州悦汇城",
                "camera_ids": ["cam_gz_1"],
                "camera_names": ["门店全景"],
                "inspection_goal": "检查出样家具是否符合样板",
                "schedule": {
                    "mode": "interval",
                    "interval_minutes": 1440,
                    "daily_window": {"mode": "fixed_daily", "fixed_time": "11:00", "label": "每天 11:00 执行"},
                    "timezone": "Asia/Shanghai",
                },
                "start_at": "2099-07-31T10:50:00+08:00",
                "end_at": "2099-08-03T23:59:59+08:00",
                "thresholds": {"confidence": 0.8},
            }
            task = server.create_scheduled_inspection_task(conn, user, plan, params)
            assert task["next_run_at"] == "2099-07-31T11:00:00+08:00"
            assert server.fixed_daily_first_run_requested("每天 11 点巡检") is False
            assert server.fixed_daily_first_run_requested("现在先巡检一遍，并每天 11 点巡检") is True
            immediate = server.create_scheduled_inspection_task(
                conn,
                user,
                plan,
                {**params, "force_first_run": True},
                force_first_run=True,
            )
            assert immediate["next_run_at"] != "2099-07-31T11:00:00+08:00"

            evidence = [{
                "evidence_id": "se_schedule_sku",
                "camera_id": "cam_gz_1",
                "camera_name": "门店全景",
                "org_id": "org_gz",
                "org_name": "广州悦汇城",
                "captured_at": "2099-07-31T11:00:06+08:00",
                "access_token": "safe-token",
                "sha256": "abc",
                "byte_size": 12,
            }]
            run = {
                "run_id": "run_schedule_sku",
                "status": "SUCCEEDED",
                "scheduled_at": "2099-07-31T11:00:00+08:00",
                "started_at": "2099-07-31T11:00:02+08:00",
                "completed_at": "2099-07-31T11:00:10+08:00",
                "result_status": "NEGATIVE",
                "conclusion": "已完成样板比对。",
                "confidence": 0.95,
                "business_reason": "事实观察。",
                "observations": "[]",
                "anomaly_evidence_ids": "[]",
                "sku_matches_json": json.dumps([
                    {"camera_name": "门店全景", "sku": "SMOKE-001"},
                    {"camera_name": "未知镜头", "sku": "SHOULD-NOT-PERSIST"},
                ]),
                "trace_json": "{}",
            }
            artifact = server.scheduled_run_artifact(task, run, evidence)
            assert artifact["timing"]["capture_delay_seconds"] == 6
            assert artifact["timing"]["status"] == "ON_TIME"
            assert artifact["evidence"][0]["sku_labels"] == ["SMOKE-001"]
            assert server.validate_agent_knowledge_payload({"title": "bad sku", "sku": "contains space", "content_text": "valid text"})["ok"] is False
    finally:
        server.DB_PATH = original_db_path
        tmp_dir.cleanup()


def check_agent_trace_contracts():
    trace = build_agent_trace(
        "帮我看下地面是否有垃圾",
        {
            "intent": "ANALYZE_VISUAL",
            "engine": "vlm",
            "confidence": 0.98,
            "tool_calls": ["paas.media.snapshot", "vlm.image.inspect"],
            "skill": {"name": "visual_snapshot_inspection"},
            "memory_hits": [{"key": "清洁判断口径", "value": "地面散落物按异常处理"}],
            "knowledge_hits": [{"title": "门店清洁 SOP", "knowledge_type": "sop"}],
        },
        {
            "visualResult": {
                "status": "POSITIVE",
                "conclusion": "发现疑似杂物。",
                "confidence": 0.9,
                "model": "Qwen3-VL-8B-Instruct-FP8",
                "source": "vlm_online",
                "model_raw_output": {"conclusion": "发现白色衣物。", "status": "POSITIVE"},
                "business_reason": "观察到疑似影响地面清洁的可见目标。",
            }
        },
        "smoke",
    )
    titles = [node["title"] for node in trace["nodes"]]
    assert "意图识别" in titles
    assert "Skill 路由" in titles
    assert "长期记忆召回" in titles
    assert "知识库召回" in titles
    assert "大模型原始输出" in titles
    assert "业务规则复核" in titles
    assert any(node["node_id"] == "memory_retrieve" and node.get("output", {}).get("hit_count") == 1 for node in trace["nodes"])
    assert any(node["node_id"] == "knowledge_recall" and node.get("output", {}).get("hit_titles") == ["门店清洁 SOP"] for node in trace["nodes"])
    assert any(node["detail"].get("raw_output", {}).get("conclusion") == "发现白色衣物。" for node in trace["nodes"])
    assert all("input" in node or node["node_id"] == "model_output" for node in trace["nodes"])
    assert any(node.get("output", {}).get("intent") == "ANALYZE_VISUAL" for node in trace["nodes"])
    assert any("不展示模型内部不可见思维链" in node.get("reasoning", "") for node in trace["nodes"])
    scheduled_failure_trace = build_agent_trace(
        "按计划检查出样图",
        {
            "intent": "CREATE_SCHEDULED_INSPECTION",
            "skill": "scheduled_snapshot_inspection",
            "tenant_id": "kuka",
            "confidence": 1.0,
            "engine": "scheduled_visual_executor",
            "status": "SUCCEEDED",
            "tool_calls": ["knowledge.retrieve", "vlm.image.inspect:failed"],
        },
        {"scheduledRun": {"status": "FAILED", "result_status": "UNCERTAIN", "error_message": "HTTP 413"}},
        "scheduled_inspection",
    )
    intent_node = next(node for node in scheduled_failure_trace["nodes"] if node["node_id"] == "intent")
    skill_node = next(node for node in scheduled_failure_trace["nodes"] if node["node_id"] == "skill")
    visual_tool = next(node for node in scheduled_failure_trace["nodes"] if node["detail"].get("tool") == "vlm.image.inspect:failed")
    assert intent_node["status"] == "SUCCEEDED"
    assert skill_node["summary"] == "路由到 scheduled_snapshot_inspection。"
    assert visual_tool["status"] == "BLOCKED"


def check_immediate_batch_execution_contracts():
    original_db_path = server.DB_PATH
    original_evidence_dir = server.SCHEDULED_EVIDENCE_DIR
    original_online_agent = server.online_agent_for_tenant
    tmp_dir = tempfile.TemporaryDirectory()
    one_pixel_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    try:
        numeric_scope_text = "帮我看下2家门店镜头下，是否存在地面垃圾未处理"
        assert server.explicit_store_scope_count(numeric_scope_text) == 2
        assert server.explicit_store_scope_count("帮我看下两家门店镜头下是否存在地面垃圾") == 2
        assert server.explicit_store_scope_count("帮我看下第2家门店镜头下是否存在地面垃圾") is None
        assert server.is_multi_store_scope_request(numeric_scope_text)
        assert server.is_batch_visual_inspection_request(numeric_scope_text)
        assert not server.is_multi_store_scope_request("帮我看下1家门店镜头下是否存在地面垃圾")

        server.DB_PATH = Path(tmp_dir.name) / "batch-smoke.db"
        server.SCHEDULED_EVIDENCE_DIR = Path(tmp_dir.name) / "scheduled_evidence"
        server.init_db(reset=True)
        with server.connect() as conn:
            org_by_id = {item["org_id"]: dict(item) for item in server.rows(conn, "SELECT * FROM orgs", ())}
            camera_by_id = {item["camera_id"]: dict(item) for item in server.rows(conn, "SELECT * FROM cameras", ())}

        class FakeOnlineAgent:
            tenant_code = "tenant_jihu"

            def capture_scheduled_snapshots(self, org_id, camera_ids):
                org = org_by_id.get(org_id) or {"name": org_id}
                snapshots = []
                for camera_id in camera_ids:
                    camera = camera_by_id.get(camera_id) or {"name": camera_id}
                    snapshots.append(
                        {
                            "org_id": org_id,
                            "org_name": org["name"],
                            "camera_id": camera_id,
                            "camera_name": camera["name"],
                            "captured_at": server.now_iso(),
                            "snapshot_url": one_pixel_png,
                        }
                    )
                return snapshots

            def analyze_scheduled_snapshots(self, goal, images, reference_images=None):
                first_camera = images[0]["camera_name"] if images else "未知镜头"
                return {
                    "status": "POSITIVE",
                    "conclusion": "发现地面有杂物，不符合清洁标准。",
                    "confidence": 0.96,
                    "business_reason": "测试视觉模型观察到禁止出现目标。",
                    "observations": [f"{first_camera} 存在地面杂物。"],
                    "selected_camera_names": [first_camera],
                    "anomaly_camera_names": [first_camera],
                    "model": "fake-vlm",
                }

        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: FakeOnlineAgent()

        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            two_store_ids = [
                row["org_id"]
                for row in server.rows(
                    conn,
                    "SELECT org_id FROM orgs WHERE tenant_id=? AND org_type='store' ORDER BY org_id LIMIT 2",
                    (user["tenant_id"],),
                )
            ]
            assert len(two_store_ids) == 2
            numeric_scope_user = {**user, "allowed_org_ids": json.dumps(two_store_ids)}
            conversation = server.create_conversation(conn, user, "即时批量执行契约")
            numeric_scope_plan, _ = server.build_batch_visual_inspection_plan(
                conn,
                numeric_scope_user,
                conversation["conversation_id"],
                "帮我看下2家门店镜头下，是否存在地面垃圾未处理",
                {"org_id": two_store_ids[0]},
            )
            assert numeric_scope_plan["intent"] == "BATCH_INSPECTION_EXECUTE"
            assert numeric_scope_plan["status"] == "READY_FOR_CONFIRM"
            assert numeric_scope_plan["slots"]["org_scope"]["store_count"] == 2
            assert set(numeric_scope_plan["slots"]["org_scope"]["resolved_ids"]) == set(two_store_ids)
            assert len(numeric_scope_plan["slots"]["camera_scope"]["store_tasks"]) == 2
            plan, _ = server.build_batch_visual_inspection_plan(
                conn,
                user,
                conversation["conversation_id"],
                "帮我给当前租户所有门店立即检查地面是否干净有垃圾",
                {"org_id": "org_gz"},
            )
            assert plan["intent"] == "BATCH_INSPECTION_EXECUTE"
            assert plan["status"] == "READY_FOR_CONFIRM"
            assert plan["actions"][0]["tool"] == "batch_inspection.execute"
            assert plan["slots"]["org_scope"]["store_count"] == 7
            assert plan["slots"]["camera_scope"]["online_camera_count"] == 9
            assert plan["slots"]["camera_scope"]["offline_camera_count"] == 1
            conn.commit()

        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            executed = server.execute_plan(conn, user, plan["plan_id"])
            assert executed["status"] == "SUCCEEDED"
            batch = executed["inspection_batch"]
            assert batch["kind"] == "BATCH_VISUAL"
            assert batch["execution_mode"] == "immediate"
            assert batch["total_store_count"] == 7
            assert batch["success_store_count"] == 7
            assert batch["failed_store_count"] == 0
            assert batch["skipped_store_count"] == 0
            assert len(batch["items"]) == 7
            assert executed["artifact"]["batchInspection"]["batch_id"] == batch["batch_id"]
            assert executed["agent"]["skill"] == "multi_store_visual_inspection"
            assert "batch_inspection.execute" in executed["agent"]["tool_calls"]
            for item in batch["items"]:
                assert item["status"] == "SUCCEEDED"
                assert item["scheduled_task_id"]
                assert item["runs"], item
                run = item["runs"][0]
                assert run["status"] == "SUCCEEDED"
                assert run["result_status"] == "POSITIVE"
                assert run["evidence"], run
                anomaly_evidence_ids = set(run["anomaly_evidence_ids"])
                flagged_evidence_ids = {evidence["evidence_id"] for evidence in run["evidence"] if evidence["is_anomalous"]}
                assert anomaly_evidence_ids, "违规结论必须关联至少一张问题快照"
                assert flagged_evidence_ids == anomaly_evidence_ids, "只有模型命中的快照应带问题标记"
                assert len(flagged_evidence_ids) == 1, "测试模型仅命中一路镜头，其他快照不能被误标"
                assert run["trace_json"]["nodes"], "子门店执行链路必须随 run 持久化"
            assert any(item["is_anomalous"] for item in batch["items"])
            audit_count = server.one(
                conn,
                "SELECT COUNT(*) AS count FROM audit_logs WHERE action IN ('inspection_batch.execute','inspection_batch.item.execute')",
                (),
            )["count"]
            assert audit_count >= 8
            assistant = server.one(
                conn,
                """
                SELECT * FROM messages
                WHERE conversation_id=? AND sender='assistant'
                ORDER BY created_at DESC LIMIT 1
                """,
                (conversation["conversation_id"],),
            )
            linked_object = json.loads(assistant["linked_object"])
            assert linked_object["inspection_batch"]["batch_id"] == batch["batch_id"]
            assert linked_object["agent"]["trace"]["nodes"]
            before_batch_count = server.one(conn, "SELECT COUNT(*) AS count FROM inspection_batches", ())["count"]
            deduped = server.execute_plan(conn, user, plan["plan_id"])
            after_batch_count = server.one(conn, "SELECT COUNT(*) AS count FROM inspection_batches", ())["count"]
            assert deduped["deduped"] is True
            assert deduped["batch_id"] == batch["batch_id"]
            assert before_batch_count == after_batch_count
    finally:
        server.online_agent_for_tenant = original_online_agent
        server.DB_PATH = original_db_path
        server.SCHEDULED_EVIDENCE_DIR = original_evidence_dir
        tmp_dir.cleanup()


def check_online_integrated_tenant_batch_scope_contracts():
    original_db_path = server.DB_PATH
    original_online_agent = server.online_agent_for_tenant
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        server.DB_PATH = Path(tmp_dir.name) / "online-batch-scope.db"
        server.init_db(reset=True)

        class FakeOnlineTenantAgent:
            tenant_code = "kuka"

            def _organization_inventory(self):
                fields = [
                    {"org_id": "kuka00001", "name": "顾家综合（秋涛欧亚达店）", "org_type": "store", "camera_count": 2},
                    {"org_id": "kuka00002", "name": "LAZBOY乐至宝（古墩路红星店）", "org_type": "store", "camera_count": 1},
                    {"org_id": "kuka00003", "name": "LAZBOY乐至宝东莞红星综合店", "org_type": "store", "camera_count": 3},
                ]
                return fields, fields

            def _camera_rows(self, field):
                counts = {"kuka00001": 2, "kuka00002": 1, "kuka00003": 3}
                org_id = field["org_id"]
                org_name = field.get("name") or org_id
                cameras = []
                for index in range(counts.get(org_id, 0)):
                    is_offline = org_id == "kuka00003" and index == 2
                    cameras.append(
                        {
                            "tenant_id": "kuka",
                            "org_id": org_id,
                            "camera_id": f"{org_id}_cam_{index + 1}",
                            "name": f"{org_name} 镜头{index + 1}",
                            "point_label": f"镜头{index + 1}",
                            "stream_status": "OFFLINE" if is_offline else "ONLINE",
                            "snapshot_url": "",
                        }
                    )
                return cameras

        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: (
            FakeOnlineTenantAgent() if _tenant == "kuka" else None
        )
        timestamp = server.now_iso()
        with server.connect() as conn:
            conn.execute(
                """INSERT INTO tenant_integrations(
                     integration_id, tenant_code, tenant_name, app_key_masked, encrypted_credentials,
                     credential_fingerprint, source, status, store_count, last_synced_at, last_error,
                     created_by, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "int_kuka_smoke",
                    "kuka",
                    "顾家家居",
                    "Sm********Key",
                    "{}",
                    "fp_kuka_smoke",
                    "SMOKE",
                    "CONNECTED",
                    3,
                    timestamp,
                    None,
                    "u_admin",
                    timestamp,
                    timestamp,
                ),
            )
            for org_id, name, camera_count in (
                ("kuka00001", "顾家综合（秋涛欧亚达店）", 2),
                ("kuka00002", "LAZBOY乐至宝（古墩路红星店）", 1),
                ("kuka00003", "LAZBOY乐至宝东莞红星综合店", 3),
            ):
                conn.execute(
                    """INSERT INTO tenant_integration_stores(
                         integration_id, org_id, parent_id, name, org_type, status, camera_count, synced_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    ("int_kuka_smoke", org_id, None, name, "store", "CONNECTED", camera_count, timestamp),
                )
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            user["tenant_id"] = "kuka"
            user["allowed_org_ids"] = json.dumps(["*"])
            conversation = server.create_conversation(conn, user, "在线租户多门店范围")
            allowed = server.allowed_org_ids(conn, user)
            assert {"kuka00001", "kuka00002", "kuka00003"}.issubset(allowed)

            scope_texts = [
                "帮我每天中午12点～13点，每20分钟看下所有门店的镜头画面，判断地面是否有垃圾，周期为一周时间",
                "帮我给当前租户全部门店每隔3小时检查地面是否有垃圾，巡检周期为期一周，从今天开始，按门店营业时间执行",
                "每家门店都看一下地面是否干净",
            ]
            for text in scope_texts:
                scope, ambiguous = server.resolve_batch_store_scope(conn, user, text, {"org_id": "kuka00003"})
                assert ambiguous is None
                assert len(scope["stores"]) == 3
                assert len(scope["store_tasks"]) == 3
                assert scope["online_camera_count"] == 5
                assert scope["offline_camera_count"] == 1
                assert all(item["status"] == "READY" for item in scope["store_tasks"])

            plan, reply = server.build_batch_scheduled_inspection_plan(
                conn,
                user,
                conversation["conversation_id"],
                "我需要每天中午12点～13点，每20min看下所有门店的镜头画面，判断画面中是否有员工店内吃饭以及地面是否有垃圾，周期为一周时间",
                {"org_id": "kuka00003"},
            )
            assert plan["intent"] == "BATCH_SCHEDULED_INSPECTION_CREATE"
            assert plan["status"] == "READY_FOR_CONFIRM"
            assert plan["slots"]["missing_slots"] == []
            assert plan["slots"]["org_scope"]["store_count"] == 3
            assert plan["slots"]["batch"]["executable_store_count"] == 3
            assert plan["slots"]["camera_scope"]["online_camera_count"] == 5
            assert plan["slots"]["camera_scope"]["offline_camera_count"] == 1
            assert plan["slots"]["schedule"]["interval_minutes"] == 20
            assert plan["slots"]["schedule"]["daily_window"]["mode"] == "daily_window"
            assert plan["slots"]["schedule"]["daily_window"]["start_time"] == "12:00"
            assert plan["slots"]["schedule"]["daily_window"]["end_time"] == "13:00"
            assert plan["slots"]["time_range"]["end"]
            assert "员工店内吃饭" in plan["slots"]["inspection_goal"]
            assert "覆盖 3 家门店" in reply
            linked = server.attach_agent_trace(
                {
                    "plan": plan,
                    "agent": {
                        "intent": "BATCH_SCHEDULED_INSPECTION_CREATE",
                        "skill": "multi_store_scheduled_inspection",
                        "engine": "deterministic_batch_scheduler_planner",
                        "status": "SUCCEEDED",
                        "tool_calls": ["paas.org.resolve", "paas.camera.page", "batch.scheduler.plan.validate"],
                    },
                    "source": "batch_scheduled_inspection",
                },
                plan["slots"]["request_text"],
            )
            camera_trace_nodes = [
                node
                for node in linked["agent"]["trace"]["nodes"]
                if node.get("detail", {}).get("tool") == "paas.camera.page"
            ]
            assert camera_trace_nodes
            camera_trace_output = camera_trace_nodes[0].get("output") or {}
            assert camera_trace_output["store_count"] == 3
            assert camera_trace_output["online_camera_count"] == 5
            assert camera_trace_output["offline_camera_count"] == 1
            assert camera_trace_output["total_camera_count"] == 6

            immediate_plan, _ = server.build_batch_visual_inspection_plan(
                conn,
                user,
                conversation["conversation_id"],
                "帮我看下当前租户所有门店地面是否有垃圾",
                {"org_id": "kuka00003"},
            )
            assert immediate_plan["intent"] == "BATCH_INSPECTION_EXECUTE"
            assert immediate_plan["status"] == "READY_FOR_CONFIRM"
            assert immediate_plan["slots"]["org_scope"]["store_count"] == 3
            assert immediate_plan["slots"]["camera_scope"]["online_camera_count"] == 5

            confirmed = server.execute_plan(conn, user, plan["plan_id"])
            assert confirmed["status"] == "RUNNING"
            batch = confirmed["inspection_batch"]
            assert batch["total_store_count"] == 3
            assert batch["success_store_count"] == 0
            assert batch["failed_store_count"] == 0
            assert batch["skipped_store_count"] == 0
            assert {item["store_id"] for item in batch["items"]} == {"kuka00001", "kuka00002", "kuka00003"}
            assert all(item["status"] == "RUNNING" for item in batch["items"])
            assert all(item["scheduled_task_id"] for item in batch["items"])
            assert all(item["scheduled_task"]["batch_id"] == batch["batch_id"] for item in batch["items"])
            tasks = server.rows(
                conn,
                "SELECT task_id,org_id,status,batch_id,next_run_at FROM scheduled_inspections WHERE batch_id=? ORDER BY org_id",
                (batch["batch_id"],),
            )
            assert [task["org_id"] for task in tasks] == ["kuka00001", "kuka00002", "kuka00003"]
            assert all(task["status"] == "ACTIVE" for task in tasks)
            assert all(task["next_run_at"] for task in tasks)

            first_task = tasks[0]
            run_id = "run_online_batch_first"
            conn.execute(
                """
                INSERT INTO inspection_runs(
                  run_id, task_id, scheduled_at, started_at, completed_at, status, attempt,
                  result_status, conclusion, confidence, business_reason, observations,
                  evidence_ids, anomaly_evidence_ids, model_version, error_message, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    first_task["task_id"],
                    first_task["next_run_at"],
                    server.now_iso(),
                    server.now_iso(),
                    "SUCCEEDED",
                    1,
                    "NEGATIVE",
                    "地面干净，未发现异常。",
                    0.98,
                    "未观察到禁止出现的目标。",
                    json.dumps([], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    "smoke-vlm",
                    None,
                    server.now_iso(),
                ),
            )
            conn.commit()
            original_db_path_for_run = server.DB_PATH
            try:
                server.DB_PATH = Path(server.DB_PATH)
                server.complete_scheduled_run(
                    first_task["task_id"],
                    run_id,
                    "",
                    {
                        "status": "NEGATIVE",
                        "conclusion": "地面干净，未发现异常。",
                        "confidence": 0.98,
                        "business_reason": "未观察到禁止出现的目标。",
                        "observations": [],
                        "selected_camera_names": [],
                        "model": "smoke-vlm",
                    },
                    None,
                    False,
                )
            finally:
                server.DB_PATH = original_db_path_for_run
            synced_batch = server.serialize_inspection_batch(
                conn,
                server.one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch["batch_id"],)),
            )
            assert synced_batch["success_store_count"] == 1
            assert synced_batch["status"] == "RUNNING"
            synced_item = next(item for item in synced_batch["items"] if item["scheduled_task_id"] == first_task["task_id"])
            assert synced_item["status"] == "SUCCEEDED"
            assert synced_item["run_ids"] == [run_id]
            deduped = server.execute_plan(conn, user, plan["plan_id"])
            assert deduped["deduped"] is True
            assert deduped["batch_id"] == batch["batch_id"]

            original_task_ids = {task["org_id"]: task["task_id"] for task in tasks}
            conn.execute("UPDATE inspection_batches SET status='RUNNING' WHERE batch_id=?", (batch["batch_id"],))
            conn.execute(
                "UPDATE inspection_batch_items SET status='RUNNING', failure_code=NULL WHERE batch_id=?",
                (batch["batch_id"],),
            )
            conn.execute(
                "UPDATE scheduled_inspections SET status='CANCELLED', next_run_at=NULL WHERE batch_id=?",
                (batch["batch_id"],),
            )
            conn.commit()
            page_repair = server.repair_visible_batch_schedules(conn, user)
            assert page_repair["batch_count"] == 1
            assert page_repair["repaired_store_count"] == 3
            repaired_tasks = server.rows(
                conn,
                "SELECT task_id,org_id,status,batch_id,next_run_at FROM scheduled_inspections WHERE batch_id=? ORDER BY org_id",
                (batch["batch_id"],),
            )
            assert {task["org_id"]: task["task_id"] for task in repaired_tasks} == original_task_ids
            assert all(task["status"] == "ACTIVE" for task in repaired_tasks)
            assert all(task["next_run_at"] for task in repaired_tasks)

            params = (plan["actions"] or [{}])[0]["params"]
            orphan = server.create_scheduled_inspection_task(
                conn,
                user,
                plan,
                {
                    "org_id": "kuka00001",
                    "org_name": "顾家综合（秋涛欧亚达店）",
                    "camera_ids": ["orphan_cam"],
                    "camera_names": ["孤立镜头"],
                    "inspection_goal": params["inspection_goal"],
                    "schedule": params["schedule"],
                    "start_at": params["start_at"],
                    "end_at": params["end_at"],
                    "thresholds": params.get("thresholds") or {"confidence": 0.8},
                    "force_first_run": True,
                },
                batch_id=batch["batch_id"],
                force_first_run=True,
            )
            conn.execute(
                "UPDATE scheduled_inspections SET status='CANCELLED', next_run_at=NULL WHERE task_id=?",
                (orphan["task_id"],),
            )
            conn.commit()
            visible_batch_task_ids = {
                item["task_id"]
                for item in server.visible_scheduled_inspection_tasks(conn, user)
                if item.get("batch_id") == batch["batch_id"]
            }
            assert visible_batch_task_ids == set(original_task_ids.values())

            conn.execute(
                "UPDATE scheduled_inspections SET status='CANCELLED', next_run_at=NULL WHERE batch_id=? AND task_id<>?",
                (batch["batch_id"], orphan["task_id"]),
            )
            conn.commit()
            repaired_deduped = server.execute_plan(conn, user, plan["plan_id"])
            assert repaired_deduped["deduped"] is True
            assert repaired_deduped.get("schedule_repaired") is True
            assert repaired_deduped["status"] == "RUNNING"
            repaired_again = server.rows(
                conn,
                "SELECT task_id,org_id,status,next_run_at FROM scheduled_inspections WHERE batch_id=? AND task_id<>? ORDER BY org_id",
                (batch["batch_id"], orphan["task_id"]),
            )
            assert {task["org_id"]: task["task_id"] for task in repaired_again} == original_task_ids
            assert all(task["status"] == "ACTIVE" for task in repaired_again)
            assert all(task["next_run_at"] for task in repaired_again)
    finally:
        server.online_agent_for_tenant = original_online_agent
        server.DB_PATH = original_db_path
        tmp_dir.cleanup()


def check_scheduled_batch_worker_first_run_contracts():
    original_db_path = server.DB_PATH
    original_evidence_dir = server.SCHEDULED_EVIDENCE_DIR
    original_online_agent = server.online_agent_for_tenant
    tmp_dir = tempfile.TemporaryDirectory()
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    capture_calls = []
    analyze_calls = []

    try:
        server.DB_PATH = Path(tmp_dir.name) / "scheduled-batch-worker.db"
        server.SCHEDULED_EVIDENCE_DIR = Path(tmp_dir.name) / "scheduled_evidence"
        server.init_db(reset=True)

        class FakeOnlineTenantAgent:
            tenant_code = "kuka"

            def __init__(self):
                self.camera_index = {}

            def _organization_inventory(self):
                fields = [
                    {"org_id": "kuka00001", "name": "顾家综合（秋涛欧亚达店）", "org_type": "store", "camera_count": 2},
                    {"org_id": "kuka00002", "name": "LAZBOY乐至宝（古墩路红星店）", "org_type": "store", "camera_count": 1},
                    {"org_id": "kuka00003", "name": "LAZBOY乐至宝东莞红星综合店", "org_type": "store", "camera_count": 3},
                ]
                return fields, fields

            def _camera_rows(self, field):
                counts = {"kuka00001": 2, "kuka00002": 1, "kuka00003": 3}
                org_id = field["org_id"]
                org_name = field.get("name") or org_id
                cameras = []
                for index in range(counts.get(org_id, 0)):
                    is_offline = org_id == "kuka00003" and index == 2
                    row = {
                        "tenant_id": "kuka",
                        "org_id": org_id,
                        "camera_id": f"{org_id}_cam_{index + 1}",
                        "name": f"{org_name} 镜头{index + 1}",
                        "point_label": f"镜头{index + 1}",
                        "stream_status": "OFFLINE" if is_offline else "ONLINE",
                        "snapshot_url": "",
                    }
                    self.camera_index[row["camera_id"]] = row
                    cameras.append(row)
                return cameras

            def capture_scheduled_snapshots(self, org_id, camera_ids):
                capture_calls.append((org_id, list(camera_ids)))
                snapshots = []
                for camera_id in camera_ids:
                    row = self.camera_index.get(camera_id) or {"name": camera_id, "org_name": org_id}
                    snapshots.append(
                        {
                            "camera_id": camera_id,
                            "camera_name": row.get("name") or camera_id,
                            "org_id": org_id,
                            "org_name": row.get("org_name") or org_id,
                            "captured_at": server.now_iso(),
                            "image": tiny_png,
                            "image_url": tiny_png,
                            "snapshot_url": tiny_png,
                        }
                    )
                return snapshots

            def analyze_scheduled_snapshots(self, goal, images, reference_images=None):
                analyze_calls.append((goal, [image.get("camera_name") for image in images]))
                return {
                    "status": "NEGATIVE",
                    "conclusion": "未发现员工在店内吃饭，地面无散落垃圾或污渍。",
                    "confidence": 1.0,
                    "business_reason": "未观察到禁止出现的目标。",
                    "observations": [],
                    "selected_camera_names": [image.get("camera_name") for image in images],
                    "anomaly_camera_names": [],
                    "model": "fake-vlm",
                }

        fake_agent = FakeOnlineTenantAgent()
        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: fake_agent if _tenant == "kuka" else None
        timestamp = server.now_iso()
        with server.connect() as conn:
            conn.execute(
                """INSERT INTO tenant_integrations(
                     integration_id, tenant_code, tenant_name, app_key_masked, encrypted_credentials,
                     credential_fingerprint, source, status, store_count, last_synced_at, last_error,
                     created_by, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "int_kuka_worker",
                    "kuka",
                    "顾家家居",
                    "Sm********Key",
                    "{}",
                    "fp_kuka_worker",
                    "SMOKE",
                    "CONNECTED",
                    3,
                    timestamp,
                    None,
                    "u_admin",
                    timestamp,
                    timestamp,
                ),
            )
            for org_id, name, camera_count in (
                ("kuka00001", "顾家综合（秋涛欧亚达店）", 2),
                ("kuka00002", "LAZBOY乐至宝（古墩路红星店）", 1),
                ("kuka00003", "LAZBOY乐至宝东莞红星综合店", 3),
            ):
                conn.execute(
                    """INSERT INTO tenant_integration_stores(
                         integration_id, org_id, parent_id, name, org_type, status, camera_count, synced_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    ("int_kuka_worker", org_id, None, name, "store", "CONNECTED", camera_count, timestamp),
                )
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            user["tenant_id"] = "kuka"
            user["allowed_org_ids"] = json.dumps(["*"])
            conversation = server.create_conversation(conn, user, "Worker 多门店首轮", org_id="kuka00003")
            plan, _reply = server.build_batch_scheduled_inspection_plan(
                conn,
                user,
                conversation["conversation_id"],
                "我需要每天中午12点～13点，每20分钟看下所有门店的镜头画面，判断画面中是否有员工店内吃饭以及地面是否有垃圾，周期为一周时间",
                {"org_id": "kuka00003"},
            )
            assert plan["status"] == "READY_FOR_CONFIRM"
            confirmed = server.execute_plan(conn, user, plan["plan_id"])
            batch_id = confirmed["inspection_batch"]["batch_id"]
            tasks = server.rows(
                conn,
                "SELECT task_id,org_id,status,batch_id,next_run_at FROM scheduled_inspections WHERE batch_id=? ORDER BY org_id",
                (batch_id,),
            )
            assert [task["org_id"] for task in tasks] == ["kuka00001", "kuka00002", "kuka00003"]
            assert all(task["status"] == "ACTIVE" and task["next_run_at"] for task in tasks)
            due_at = max(task["next_run_at"] for task in tasks)
            due = server.due_scheduled_inspection_tasks(conn, due_at, seed_limit=1, claim_limit=10)
            due_batch = [task for task in due if task.get("batch_id") == batch_id]
            assert sorted(task["org_id"] for task in due_batch) == ["kuka00001", "kuka00002", "kuka00003"]
            conn.commit()

        worker = server.ScheduledInspectionWorker(poll_seconds=999)
        worker.tick()

        with server.connect() as conn:
            run_rows = server.rows(
                conn,
                """
                SELECT r.run_id,r.status,r.result_status,s.org_id,s.run_count
                FROM inspection_runs r
                JOIN scheduled_inspections s ON s.task_id=r.task_id
                WHERE s.batch_id=?
                ORDER BY s.org_id
                """,
                (batch_id,),
            )
            assert len(run_rows) == 3
            assert [row["org_id"] for row in run_rows] == ["kuka00001", "kuka00002", "kuka00003"]
            assert all(row["status"] == "SUCCEEDED" and row["result_status"] == "NEGATIVE" for row in run_rows)
            assert all(row["run_count"] == 1 for row in run_rows)
            batch = server.serialize_inspection_batch(
                conn,
                server.one(conn, "SELECT * FROM inspection_batches WHERE batch_id=?", (batch_id,)),
            )
            assert batch["status"] == "SUCCEEDED"
            assert batch["success_store_count"] == 3
            assert batch["failed_store_count"] == 0
            assert batch["skipped_store_count"] == 0
            assert all(item["status"] == "SUCCEEDED" and item["run_ids"] for item in batch["items"])
            assert {org_id for org_id, _camera_ids in capture_calls} == {"kuka00001", "kuka00002", "kuka00003"}
            assert len(analyze_calls) == 3
    finally:
        server.online_agent_for_tenant = original_online_agent
        server.DB_PATH = original_db_path
        server.SCHEDULED_EVIDENCE_DIR = original_evidence_dir
        tmp_dir.cleanup()


def check_single_store_scheduled_worker_first_run_contracts():
    original_db_path = server.DB_PATH
    original_evidence_dir = server.SCHEDULED_EVIDENCE_DIR
    original_online_agent = server.online_agent_for_tenant
    tmp_dir = tempfile.TemporaryDirectory()
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    capture_calls = []

    try:
        server.DB_PATH = Path(tmp_dir.name) / "scheduled-single-worker.db"
        server.SCHEDULED_EVIDENCE_DIR = Path(tmp_dir.name) / "scheduled_evidence"
        server.init_db(reset=True)

        class FakeSingleStoreAgent:
            tenant_code = "oppo"

            def bootstrap(self, user):
                return {
                    "orgs": [
                        {
                            "org_id": "org_gz",
                            "parent_id": None,
                            "name": "广州天河区天河城店",
                            "org_type": "store",
                            "status": "CONNECTED",
                            "camera_count": 2,
                        }
                    ],
                    "cameras": [
                        {
                            "camera_id": "sg_cam_1",
                            "name": "展厅1",
                            "org_id": "org_gz",
                            "org_name": "广州天河区天河城店",
                            "stream_status": "ONLINE",
                            "region": "广州天河区天河城店",
                        },
                        {
                            "camera_id": "sg_cam_2",
                            "name": "展厅2",
                            "org_id": "org_gz",
                            "org_name": "广州天河区天河城店",
                            "stream_status": "ONLINE",
                            "region": "广州天河区天河城店",
                        },
                    ],
                    "status": "ONLINE",
                }

            def capture_scheduled_snapshots(self, org_id, camera_ids):
                capture_calls.append((org_id, list(camera_ids)))
                return [
                    {
                        "camera_id": camera_id,
                        "camera_name": f"单店镜头{index + 1}",
                        "org_id": org_id,
                        "org_name": "广州天河区天河城店",
                        "captured_at": server.now_iso(),
                        "image": tiny_png,
                        "image_url": tiny_png,
                        "snapshot_url": tiny_png,
                    }
                    for index, camera_id in enumerate(camera_ids)
                ]

            def analyze_scheduled_snapshots(self, goal, images, reference_images=None):
                return {
                    "status": "NEGATIVE",
                    "conclusion": "地面干净，未发现垃圾。",
                    "confidence": 1.0,
                    "business_reason": "未观察到禁止出现的目标。",
                    "observations": [],
                    "selected_camera_names": [image.get("camera_name") for image in images],
                    "anomaly_camera_names": [],
                    "model": "fake-vlm",
                }

        server.online_agent_for_tenant = lambda _conn, _tenant, required=False: (
            FakeSingleStoreAgent() if _tenant == "oppo" else None
        )
        with server.connect() as conn:
            user = dict(server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'", ()))
            user["tenant_id"] = "oppo"
            user["allowed_org_ids"] = json.dumps(["*"])
            conversation = server.create_conversation(conn, user, "Worker 单门店首轮", org_id="org_gz")
            plan, _reply = server.build_scheduled_inspection_plan(
                conn,
                user,
                conversation["conversation_id"],
                "帮我从今天开始每20分钟按门店营业时间看一下当前门店地面是否干净有垃圾，巡检周期为一周",
                {"org_id": "org_gz"},
            )
            assert plan["intent"] == "CREATE_SCHEDULED_INSPECTION"
            assert plan["status"] == "READY_FOR_CONFIRM"
            confirmed = server.execute_plan(conn, user, plan["plan_id"])
            assert confirmed["status"] == "ACTIVE"
            task = server.one(
                conn,
                "SELECT task_id,org_id,status,batch_id,next_run_at FROM scheduled_inspections WHERE plan_id=?",
                (plan["plan_id"],),
            )
            assert task["org_id"] == "org_gz"
            assert task["batch_id"] is None
            assert task["status"] == "ACTIVE"
            assert task["next_run_at"]
            conn.commit()

        worker = server.ScheduledInspectionWorker(poll_seconds=999)
        worker.tick()

        with server.connect() as conn:
            run_rows = server.rows(
                conn,
                """
                SELECT r.status,r.result_status,r.error_message,s.org_id,s.run_count,s.batch_id
                FROM inspection_runs r
                JOIN scheduled_inspections s ON s.task_id=r.task_id
                WHERE s.plan_id=?
                """,
                (plan["plan_id"],),
            )
            assert len(run_rows) == 1
            assert run_rows[0]["org_id"] == "org_gz"
            assert run_rows[0]["batch_id"] is None
            assert run_rows[0]["status"] == "SUCCEEDED", dict(run_rows[0])
            assert run_rows[0]["result_status"] == "NEGATIVE", dict(run_rows[0])
            assert run_rows[0]["run_count"] == 1
            batch_items = server.rows(
                conn,
                "SELECT * FROM inspection_batch_items WHERE scheduled_task_id=?",
                (task["task_id"],),
            )
            assert batch_items == []
            assert capture_calls and capture_calls[0][0] == "org_gz"
    finally:
        server.online_agent_for_tenant = original_online_agent
        server.DB_PATH = original_db_path
        server.SCHEDULED_EVIDENCE_DIR = original_evidence_dir
        tmp_dir.cleanup()


def wait_until_ready(base: str, proc: subprocess.Popen):
    last_error = None
    for _ in range(40):
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            payload = request(base, "GET", "/api/bootstrap")
            if payload.get("ok"):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"server did not become ready: {last_error}")


def check_comparison_http_contracts(base: str):
    contract = assert_ok(
        request(
            base,
            "POST",
            "/v1/ovd/contract-test",
            body={
                "expected_prompts": ["chair"],
                "response": {
                    "requestID": "http-contract-1",
                    "modelVersion": "ovd-contract-model",
                    "imageWidth": 128,
                    "imageHeight": 96,
                    "detections": [{"className": "chair", "score": 0.92, "bbox_xyxy": [1, 1, 100, 80]}],
                },
            },
        )
    )["contract_test"]
    assert contract["ok"] is True

    version = assert_ok(request(base, "POST", "/v1/catalog-versions", body={"change_summary": "http comparison smoke"}, expected=201))["catalog_version"]
    sku = assert_ok(
        request(
            base,
            "POST",
            "/v1/catalog/skus",
            body={
                "catalog_version_id": version["version_id"],
                "sku_id": "HTTP-CHAIR-001",
                "canonical_name": "HTTP 样板沙发椅",
                "aliases": ["HTTP 展示椅"],
                "external_codes": [{"code_type": "BARCODE", "code_value": "690000000099"}],
            },
            expected=201,
        )
    )["sku"]
    listed = assert_ok(request(base, "GET", f"/v1/catalog/skus?catalog_version_id={version['version_id']}"))
    assert listed["skus"][0]["sku_item_id"] == sku["sku_item_id"]
    request(base, "POST", f"/v1/catalog-versions/{version['version_id']}/approve", body={}, expected=403)
    assert_ok(request(base, "POST", f"/v1/catalog-versions/{version['version_id']}/approve", user="u_system", body={}))
    assert_ok(request(base, "POST", f"/v1/catalog-versions/{version['version_id']}/publish", body={}))

    profile = assert_ok(
        request(
            base,
            "POST",
            "/v1/domain-profiles",
            body={"name": "HTTP 家具 POC", "domain": "furniture", "capture_mode": "FIXED_CAMERA", "identity_policy": {"ovd_prompts": ["chair"], "priority": ["visual_embedding"]}, "quality_bundle": {}},
            expected=201,
        )
    )["domain_profile"]
    profile = assert_ok(request(base, "POST", f"/v1/domain-profiles/{profile['profile_id']}/approve", body={})) ["domain_profile"]
    calibration = assert_ok(
        request(base, "POST", "/v1/calibrations", body={"camera_id": "cam_gz_1", "version": "http-cal-v1", "roi": [[0, 0], [1, 0], [1, 1], [0, 1]], "health_state": "GREEN"}, expected=201)
    )["calibration"]
    assert_ok(request(base, "POST", f"/v1/calibrations/{calibration['calibration_id']}/approve", body={}))
    asset = assert_ok(
        request(base, "POST", "/v1/reference-assets", body={"catalog_version_id": version["version_id"], "sku_id": "HTTP-CHAIR-001", "asset_url": "/static/evidence/ev-10231.svg"}, expected=201)
    )["reference_asset"]
    assert_ok(request(base, "POST", f"/v1/reference-assets/{asset['asset_id']}/approve", body={}))
    slot = assert_ok(
        request(
            base,
            "POST",
            "/v1/display-slots",
            body={
                "org_id": "org_gz", "camera_id": "cam_gz_1", "domain_profile_id": profile["profile_id"], "catalog_version_id": version["version_id"], "calibration_version": "http-cal-v1",
                "zone_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]], "expected_skus": ["HTTP-CHAIR-001"], "automation_enabled": True,
            },
            expected=201,
        )
    )["display_slot"]
    slot = assert_ok(request(base, "POST", f"/v1/display-slots/{slot['slot_id']}/approve", body={})) ["display_slot"]
    session = assert_ok(
        request(
            base,
            "POST",
            "/v1/comparison-sessions",
            body={
                "camera_id": "cam_gz_1", "capture_mode": "FIXED_CAMERA", "domain_profile_id": profile["profile_id"], "catalog_version_id": version["version_id"], "calibration_version": "http-cal-v1",
                "display_slot_ids": [slot["slot_id"]], "idempotency_key": "http-comparison-session-1",
            },
            expected=201,
        )
    )["comparison_session"]
    detail = assert_ok(request(base, "GET", f"/v1/comparison-sessions/{session['session_id']}"))
    assert detail["comparison_session"]["run_snapshot"]["catalog_version_id"] == version["version_id"]
    request(base, "POST", f"/v1/comparison-sessions/{session['session_id']}/frames", body={"evidence_id": "not-owned-evidence"}, expected=404)


def main():
    check_online_tenant_does_not_escalate_role()
    check_online_delivery_failure_contract()
    check_open_web_search_trace_contract()
    check_online_delivery_failure_persistence()
    check_online_snapshot_archiving_contract()
    check_auto_online_open_qa_pdf_persistence()
    check_frontend_contracts()
    check_agent_core_contracts()
    check_slot_parsing_contracts()
    check_visual_reasoner_contracts()
    check_inspection_knowledge_retrieval_contracts()
    check_comparison_p0_p1_contracts()
    check_fixed_daily_scheduling_and_sku_artifact_contracts()
    check_agent_trace_contracts()
    check_immediate_batch_execution_contracts()
    check_online_integrated_tenant_batch_scope_contracts()
    check_scheduled_batch_worker_first_run_contracts()
    check_single_store_scheduled_worker_first_run_contracts()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp_dir = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp_dir.name, "smoke.db")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--port", str(port), "--db", db_path, "--reset"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "AGI_OPEN_QA_EXPORT_DIR": os.path.join(tmp_dir.name, "open-qa-exports")},
    )
    try:
        wait_until_ready(base, proc)
        check_comparison_http_contracts(base)

        bootstrap = assert_ok(request(base, "GET", "/api/bootstrap"))
        serialized_cameras = json.dumps(bootstrap["cameras"], ensure_ascii=False)
        assert "stream_url" not in serialized_cameras
        assert "credential" not in serialized_cameras
        visual_capability = next(
            item for item in bootstrap["capabilities"]
            if item["capability_id"] == "visual_compliance_inspection"
        )
        assert visual_capability["name"] == "门店视觉合规巡检"
        assert "其他品牌Logo" in visual_capability["aliases"]
        assert "agent_catalog" in bootstrap
        assert bootstrap["agent_catalog"]["summary"]["builtin_skills"] >= 1

        catalog_payload = assert_ok(request(base, "GET", "/api/agent/catalog"))
        assert catalog_payload["catalog"]["version"] == "agent-core-v1"
        assert catalog_payload["templates"]["skill"]["kind"] == "skill"
        assert "memory" in catalog_payload
        assert "knowledge" in catalog_payload
        initial_web_search_config = assert_ok(request(base, "GET", "/api/agent/web-search/config"))
        assert initial_web_search_config["configured"] is False
        assert initial_web_search_config["source"] == "unconfigured"
        template_validation = assert_ok(
            request(base, "POST", "/api/agent/manifests/validate", body={"manifest": catalog_payload["templates"]["skill"]})
        )["validation"]
        assert template_validation["ok"] is True
        request(base, "GET", "/api/agent/catalog", user="u_store", expected=403)
        request(base, "GET", "/api/agent/web-search/config", user="u_store", expected=403)
        request(
            base,
            "POST",
            "/api/agent/memories",
            body={
                "category": "business_rule",
                "scope": "tenant",
                "key": "Smoke 竞品 Logo 判断",
                "value": "门店出现非本品牌 logo 或宣传海报应判定为异常。",
                "confidence": 0.95,
            },
            expected=409,
        )
        memory = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/memories",
                body={
                    "category": "business_rule",
                    "scope": "tenant",
                    "key": "Smoke 竞品 Logo 判断",
                    "value": "门店出现非本品牌 logo 或宣传海报应判定为异常。",
                    "aliases": "竞品标识,其他品牌",
                    "confidence": 0.95,
                    "confirm_important": True,
                },
                expected=201,
            )
        )["memory"]
        assert memory["category"] == "business_rule"
        assert "竞品标识" in memory["aliases"]
        request(
            base,
            "POST",
            "/api/agent/knowledge-assets",
            user="u_store",
            body={"filename": "x.png", "data_url": "data:image/png;base64,AA=="},
            expected=403,
        )
        request(
            base,
            "POST",
            "/api/agent/knowledge-assets",
            body={"filename": "invalid.png", "data_url": "data:image/png;base64,AA=="},
            expected=422,
        )
        request(
            base,
            "POST",
            "/api/agent/knowledge",
            body={
                "title": "Smoke 超量图片",
                "knowledge_type": "brand_standard",
                "modality": "image",
                "content_text": "用于校验单次多图上传数量限制。",
                "asset_uploads": [
                    {
                        "filename": f"too-many-{index}.png",
                        "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
                    }
                    for index in range(11)
                ],
            },
            expected=422,
        )
        uploaded_asset = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/knowledge-assets",
                body={
                    "filename": "smoke-logo.png",
                    "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
                },
                expected=201,
            )
        )["asset"]
        assert uploaded_asset["asset_url"].startswith("/static/uploads/knowledge/")
        uploaded_asset_path = os.path.join(ROOT, "static", uploaded_asset["asset_url"].removeprefix("/static/"))
        assert os.path.exists(uploaded_asset_path)
        knowledge = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/knowledge",
                body={
                    "title": "Smoke 品牌规范",
                    "knowledge_type": "brand_standard",
                    "modality": "image",
                    "content_text": "品牌露出仅允许本品牌主视觉，竞品海报属于异常。",
                    "asset_uploads": [
                        {
                            "filename": "smoke-inline-logo-1.png",
                            "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
                        },
                        {
                            "filename": "smoke-inline-logo-2.png",
                            "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
                        },
                    ],
                    "asset_metadata": [
                        {"upload_index": 0, "sku": "SMOKE-HTTP-01", "view_tag": "正面", "description": "HTTP 测试第一张样板"},
                        {"upload_index": 1, "sku": "SMOKE-HTTP-02", "view_tag": "侧面", "description": "HTTP 测试第二张样板"},
                    ],
                    "tags": "品牌,视觉合规",
                    "source": "local_upload",
                },
                expected=201,
            )
        )["knowledge"]
        assert knowledge["knowledge_type"] == "brand_standard"
        assert knowledge["asset_url"].startswith("/static/uploads/knowledge/")
        assert len(knowledge["asset_urls"]) == 2
        assert knowledge["asset_urls"][0] == knowledge["asset_url"]
        assert [asset["sku"] for asset in knowledge["reference_assets"]] == ["SMOKE-HTTP-01", "SMOKE-HTTP-02"]
        assert knowledge["reference_assets"][1]["view_tag"] == "侧面"
        assert knowledge["source"] == "local_upload"
        original_asset_urls = list(knowledge["asset_urls"])
        removed_asset_path = os.path.join(ROOT, "static", original_asset_urls[1].removeprefix("/static/"))
        request(
            base,
            "PATCH",
            f"/api/agent/knowledge/{knowledge['knowledge_id']}",
            user="u_store",
            body={"title": "无权限编辑", "knowledge_type": "brand_standard", "modality": "image", "existing_asset_urls": original_asset_urls},
            expected=403,
        )
        updated_knowledge = assert_ok(
            request(
                base,
                "PATCH",
                f"/api/agent/knowledge/{knowledge['knowledge_id']}",
                body={
                    "title": "Smoke 已编辑品牌规范",
                    "knowledge_type": "sop",
                    "modality": "image",
                    "content_text": "已更新品牌素材范围，并保留第一张图片作为参考。",
                    "existing_asset_urls": [original_asset_urls[0]],
                    "asset_uploads": [
                        {
                            "filename": "smoke-inline-logo-3.png",
                            "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
                        },
                    ],
                    "asset_metadata": [
                        {"asset_url": original_asset_urls[0], "sku": "SMOKE-HTTP-01", "view_tag": "正面", "description": "保留的首张样板"},
                        {"upload_index": 0, "sku": "SMOKE-HTTP-03", "view_tag": "右侧", "description": "新增的第三张样板"},
                    ],
                    "tags": "更新,素材",
                },
            )
        )["knowledge"]
        assert updated_knowledge["knowledge_id"] == knowledge["knowledge_id"]
        assert updated_knowledge["title"] == "Smoke 已编辑品牌规范"
        assert updated_knowledge["knowledge_type"] == "sop"
        assert updated_knowledge["asset_urls"][0] == original_asset_urls[0]
        assert len(updated_knowledge["asset_urls"]) == 2
        assert [asset["sku"] for asset in updated_knowledge["reference_assets"]] == ["SMOKE-HTTP-01", "SMOKE-HTTP-03"]
        assert updated_knowledge["reference_assets"][1]["description"] == "新增的第三张样板"
        assert not os.path.exists(removed_asset_path)
        updated_asset_path = os.path.join(ROOT, "static", updated_knowledge["asset_urls"][1].removeprefix("/static/"))
        assert os.path.exists(updated_asset_path)
        catalog_with_p2 = assert_ok(request(base, "GET", "/api/agent/catalog"))
        assert catalog_with_p2["summary"]["memory_items"] == 1
        assert catalog_with_p2["summary"]["knowledge_items"] == 1
        assert catalog_with_p2["memory"]["items"][0]["key"] == "Smoke 竞品 Logo 判断"
        request(base, "POST", "/api/agent/memories", user="u_store", body={"key": "x", "value": "y"}, expected=403)
        deleted_memory = assert_ok(request(base, "DELETE", f"/api/agent/memories/{memory['memory_id']}"))["memory"]
        assert deleted_memory["status"] == "DELETED"
        request(base, "DELETE", f"/api/agent/knowledge/{knowledge['knowledge_id']}", user="u_store", expected=403)
        deleted_knowledge = assert_ok(request(base, "DELETE", f"/api/agent/knowledge/{knowledge['knowledge_id']}"))["knowledge"]
        assert deleted_knowledge["status"] == "DELETED"
        catalog_after_memory_delete = assert_ok(request(base, "GET", "/api/agent/catalog"))
        assert catalog_after_memory_delete["summary"]["memory_items"] == 0
        assert catalog_after_memory_delete["summary"]["knowledge_items"] == 0
        if os.path.exists(uploaded_asset_path):
            os.remove(uploaded_asset_path)
        for asset_url in updated_knowledge["asset_urls"]:
            knowledge_asset_path = os.path.join(ROOT, "static", asset_url.removeprefix("/static/"))
            if os.path.exists(knowledge_asset_path):
                os.remove(knowledge_asset_path)
        imported_skill_manifest = {
            "kind": "skill",
            "schema_version": "skill.v1",
            "metadata": {
                "name": "smoke.fire_lane_check",
                "label": "Smoke 消防通道检测",
                "version": "1.0.0",
            },
            "intent": {
                "name": "SMOKE_FIRE_LANE_CHECK",
                "aliases": ["smoke 检查消防通道"],
                "similar_intents": ["ANALYZE_VISUAL"],
            },
            "slots": {
                "required": ["org_scope", "camera_ids", "inspection_goal"],
                "optional": ["roi", "schedule"],
            },
            "execution": {
                "mode": "workflow",
                "steps": [
                    {"tool": "paas.media.snapshot", "purpose": "抓取点位快照"},
                    {"tool": "vlm.image.inspect", "purpose": "视觉判断"},
                ],
            },
            "risk": {"level": "READ_ONLY", "confirm_required": False},
        }
        manifest_validation = assert_ok(
            request(base, "POST", "/api/agent/manifests/validate", body={"manifest": imported_skill_manifest})
        )["validation"]
        assert manifest_validation["ok"] is True
        semantic_invalid_manifest = dict(imported_skill_manifest)
        semantic_invalid_manifest["metadata"] = {"name": "smoke.bad_route", "label": "Smoke Bad Route"}
        semantic_invalid_manifest["intent"] = {"name": "ANALYZE_VISUAL", "aliases": ["bad"]}
        semantic_validation = assert_ok(
            request(base, "POST", "/api/agent/manifests/validate", body={"manifest": semantic_invalid_manifest})
        )["validation"]
        assert semantic_validation["ok"] is False
        assert any("conflicts with builtin intent" in error for error in semantic_validation["errors"])
        unknown_tool_manifest = dict(imported_skill_manifest)
        unknown_tool_manifest["metadata"] = {"name": "smoke.unknown_tool", "label": "Smoke Unknown Tool"}
        unknown_tool_manifest["intent"] = {"name": "SMOKE_UNKNOWN_TOOL", "aliases": ["unknown"]}
        unknown_tool_manifest["execution"] = {"mode": "workflow", "steps": [{"tool": "missing.tool"}]}
        unknown_tool_validation = assert_ok(
            request(base, "POST", "/api/agent/manifests/validate", body={"manifest": unknown_tool_manifest})
        )["validation"]
        assert unknown_tool_validation["ok"] is False
        assert any("unknown tool" in error for error in unknown_tool_validation["errors"])
        unknown_tool_diagnostics = unknown_tool_validation.get("diagnostics") or []
        assert unknown_tool_diagnostics
        assert unknown_tool_diagnostics[0]["title"] == "执行步骤引用了未注册工具"
        assert "工具箱里没有这个工具" in unknown_tool_diagnostics[0]["message"]
        nl_skill_draft = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/manifests/draft",
                body={"kind": "skill", "prompt": "创建一个每天上午10点检查 OPPO 门店是否存在其他品牌 Logo 或宣传海报的巡检 Skill"},
            )
        )
        assert nl_skill_draft["kind"] == "skill"
        assert nl_skill_draft["validation"]["ok"] is True
        assert nl_skill_draft["guide"]["title"] == "已生成 Skill 草稿"
        assert nl_skill_draft["manifest"]["risk"]["confirm_required"] is True
        draft_step_tools = [step.get("tool") for step in nl_skill_draft["manifest"]["execution"]["steps"]]
        assert "knowledge.retrieve" in draft_step_tools
        assert "vlm.image.inspect" in draft_step_tools
        operation_skill_draft = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/manifests/draft",
                body={"kind": "skill", "prompt": "创建一个每天上午11点查看店内所有镜头，检查是否存在员工吃东西、广告牌灯箱未开、电视屏幕关闭的情况"},
            )
        )
        operation_manifest = operation_skill_draft["manifest"]
        assert operation_skill_draft["kind"] == "skill"
        assert operation_skill_draft["validation"]["ok"] is True
        assert operation_skill_draft["validation"]["normalized"]["runtime_status"] == "callable"
        assert operation_manifest["metadata"]["label"] == "门店运营合规巡检"
        assert "品牌露出" not in operation_manifest["metadata"]["label"]
        assert "员工吃东西" in operation_manifest["metadata"]["description"]
        assert "广告牌灯箱未开" in operation_manifest["metadata"]["description"]
        assert "电视屏幕关闭" in operation_manifest["metadata"]["description"]
        assert operation_manifest["intent"]["name"].startswith("CHECK_STORE_OPERATION_COMPLIANCE_")
        operation_tools = [step.get("tool") for step in operation_manifest["execution"]["steps"]]
        assert "paas.camera.page" in operation_tools
        assert "paas.media.snapshot" in operation_tools
        assert "knowledge.retrieve" in operation_tools
        assert "vlm.image.inspect" in operation_tools
        assert "CREATE_SCHEDULED_INSPECTION" in operation_manifest["intent"]["similar_intents"]
        assert any("员工吃东西" in item for item in operation_skill_draft["guide"]["parsed"])
        nl_tool_draft = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/manifests/draft",
                body={"kind": "tool", "prompt": "注册一个 POST https://example.com/api/tickets 的外部工单工具"},
            )
        )
        assert nl_tool_draft["kind"] == "tool"
        assert nl_tool_draft["validation"]["ok"] is True
        assert nl_tool_draft["validation"]["normalized"]["runtime_status"] == "callable"
        assert nl_tool_draft["manifest"]["runtime"]["method"] == "POST"
        assert nl_tool_draft["manifest"]["risk"]["confirm_required"] is True
        missing_endpoint_tool = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/manifests/draft",
                body={"kind": "tool", "prompt": "注册一个查询外部库存系统的接口工具"},
            )
        )
        assert missing_endpoint_tool["kind"] == "tool"
        assert missing_endpoint_tool["validation"]["ok"] is False
        assert any(item["code"] == "ENDPOINT_PLACEHOLDER" for item in missing_endpoint_tool["validation"].get("diagnostics") or [])
        builtin_snapshot_tool = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/manifests/draft",
                body={"kind": "tool", "prompt": "创建一个抓取摄像头快照的工具"},
            )
        )
        assert builtin_snapshot_tool["validation"]["ok"] is True
        assert builtin_snapshot_tool["validation"]["normalized"]["runtime_status"] == "callable"
        assert builtin_snapshot_tool["manifest"]["runtime"]["type"] == "builtin"
        assert builtin_snapshot_tool["manifest"]["runtime"]["handler"] == "paas.media.snapshot"
        imported_manifest = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/manifests",
                body={"manifest": imported_skill_manifest},
                expected=201,
            )
        )["manifest"]
        assert imported_manifest["runtime_status"] == "callable"
        assert imported_manifest["manifest"]["metadata"]["label"] == "Smoke 消防通道检测"
        assert imported_manifest["manifest"]["execution"]["steps"][0]["tool"] == "paas.media.snapshot"
        catalog_after_import = assert_ok(request(base, "GET", "/api/agent/catalog"))
        assert catalog_after_import["summary"]["imported_skills"] == 1
        catalog_imported_skill = next(item for item in catalog_after_import["extensions"] if item["name"] == "smoke.fire_lane_check")
        assert catalog_imported_skill["manifest"]["intent"]["name"] == "SMOKE_FIRE_LANE_CHECK"
        assert catalog_imported_skill["manifest"]["execution"]["steps"][1]["tool"] == "vlm.image.inspect"
        request(base, "DELETE", f"/api/agent/manifests/{imported_manifest['manifest_id']}", user="u_store", expected=403)
        deleted_manifest = assert_ok(request(base, "DELETE", f"/api/agent/manifests/{imported_manifest['manifest_id']}"))["manifest"]
        assert deleted_manifest["status"] == "DELETED"
        catalog_after_manifest_delete = assert_ok(request(base, "GET", "/api/agent/catalog"))
        assert catalog_after_manifest_delete["summary"]["imported_skills"] == 0
        assert all(item["manifest_id"] != imported_manifest["manifest_id"] for item in catalog_after_manifest_delete["extensions"])
        assert any(item["name"] == "web.search" for item in catalog_after_manifest_delete["catalog"]["tools"])
        assert catalog_after_manifest_delete["web_search"]["configured"] is False
        unsafe_tool_manifest = {
            "kind": "tool",
            "schema_version": "tool.v1",
            "metadata": {"name": "unsafe.raw.key", "label": "Unsafe Raw Key"},
            "runtime": {"type": "http", "endpoint": "https://example.invalid", "auth": {"api_key": "raw"}},
            "input_schema": {},
            "output_schema": {},
            "risk": {"level": "HIGH_WRITE", "confirm_required": True},
        }
        invalid_manifest = request(
            base,
            "POST",
            "/api/agent/manifests",
            body={"manifest": unsafe_tool_manifest},
            expected=422,
        )
        assert invalid_manifest["error"]["code"] == "AGENT_MANIFEST_INVALID"

        open_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "open qa"}, expected=201)
        )["conversation"]
        open_answer = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{open_conv['conversation_id']}/messages",
                body={"content": "今天的天气如何？", "context": {"org_id": "org_gz"}},
            )
        )
        assert open_answer["intent"] == "OPEN_QA"
        assert open_answer["agent"]["mode"] == "OPEN_QA"
        assert open_answer["agent"]["tool_calls"] == ["web.search:unavailable"]
        assert open_answer["agent"]["decision"]["allowed_tools"] == ["web.search"]
        assert open_answer["agent"]["decision"]["evidence_state"] == "CAPABILITY_UNAVAILABLE"
        trace_node_ids = {item["node_id"] for item in open_answer["messages"][0]["linked_object"]["agent"]["trace"]["nodes"]}
        assert "memory_retrieve" not in trace_node_ids
        assert "knowledge_recall" not in trace_node_ids
        assert "tool_1" in trace_node_ids

        auto_realtime_answer = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{open_conv['conversation_id']}/messages",
                body={"content": "今天天气怎么样？", "context": {"org_id": "org_gz", "mode_override": "AUTO"}},
            )
        )
        assert auto_realtime_answer["intent"] == "OPEN_QA"
        assert auto_realtime_answer["agent"]["tool_calls"] == ["web.search:unavailable"]
        assert auto_realtime_answer["agent"]["decision"]["response_strategy"] == "SEARCH_AND_CITE"

        forced_open_answer = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{open_conv['conversation_id']}/messages",
                body={
                    "content": "查看广州悦汇城在线摄像头",
                    "context": {"org_id": "org_gz", "mode_override": "OPEN_QA"},
                },
            )
        )
        assert forced_open_answer["intent"] == "OPEN_QA"
        assert forced_open_answer["agent"]["decision"]["mode_selection"] == "OPEN_QA"
        assert forced_open_answer["agent"]["tool_calls"] == []

        conv = assert_ok(request(base, "POST", "/api/conversations", body={"title": "smoke"}, expected=201))["conversation"]
        incomplete = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{conv['conversation_id']}/messages",
                body={"content": "下周开始给广州悦汇城订阅离岗检测", "context": {"org_id": "org_gz"}},
            )
        )
        assert incomplete["plan"]["status"] == "NEED_CLARIFICATION"
        clarification = incomplete["messages"][0]["content"]
        assert "巡检时段" in clarification
        assert "schedule" not in clarification

        continued = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{conv['conversation_id']}/messages",
                body={"content": "每天 9 点到 22 点", "context": {"org_id": "org_gz"}},
            )
        )
        assert continued["intent"] == "SUBSCRIPTION_CREATE"
        assert continued["plan"]["status"] == "READY_FOR_CONFIRM"
        assert continued["plan"]["slots"]["schedule"]["start_time"] == "09:00"
        assert continued["plan"]["slots"]["schedule"]["end_time"] == "22:00"

        conversation_detail = assert_ok(
            request(base, "GET", f"/api/conversations/{conv['conversation_id']}")
        )
        assert conversation_detail["conversation"]["title"] == "smoke"
        assert [item["sender"] for item in conversation_detail["messages"][:2]] == ["user", "assistant"]
        assert all(item.get("created_at") for item in conversation_detail["messages"])
        request(base, "GET", f"/api/conversations/{conv['conversation_id']}", user="u_store", expected=404)
        signed_url = "https://oss.example/snapshot.jpg?OSSAccessKeyId=ak&Signature=secret&Expires=1890000000"
        with sqlite3.connect(db_path) as signed_db:
            signed_db.row_factory = sqlite3.Row
            add_message(
                signed_db,
                conv["conversation_id"],
                "assistant",
                "signed media guard",
                linked_object={"artifact": {"media": {"snapshot_url": signed_url}}},
            )
            persisted = signed_db.execute(
                "SELECT linked_object FROM messages WHERE content=?",
                ("signed media guard",),
            ).fetchone()[0]
        assert "OSSAccessKeyId" not in persisted
        assert "Signature" not in persisted
        assert "Expires" not in persisted
        assert "signature_redacted=1" in persisted

        titled_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "新的巡检对话"}, expected=201)
        )["conversation"]
        assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{titled_conv['conversation_id']}/messages",
                body={"content": "查看广州悦汇城离线摄像头", "context": {"org_id": "org_gz"}},
            )
        )
        conversation_list = assert_ok(request(base, "GET", "/api/conversations"))["conversations"]
        titled_summary = next(item for item in conversation_list if item["conversation_id"] == titled_conv["conversation_id"])
        assert titled_summary["title"] == "查看广州悦汇城离线摄像头"
        assert titled_summary["message_count"] == 2
        assert any(item["conversation_id"] == conv["conversation_id"] for item in conversation_list)

        clear_one = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "待清空 1"}, expected=201)
        )["conversation"]
        clear_two = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "待清空 2"}, expected=201)
        )["conversation"]
        clear_result = assert_ok(
            request(
                base,
                "DELETE",
                "/api/conversations",
                body={"conversation_ids": [clear_one["conversation_id"], clear_two["conversation_id"]]},
            )
        )
        assert clear_result["closed_count"] == 2
        assert set(clear_result["conversation_ids"]) == {clear_one["conversation_id"], clear_two["conversation_id"]}
        request(base, "GET", f"/api/conversations/{clear_one['conversation_id']}", expected=404)
        remaining_conversations = assert_ok(request(base, "GET", "/api/conversations"))["conversations"]
        remaining_ids = {item["conversation_id"] for item in remaining_conversations}
        assert clear_one["conversation_id"] not in remaining_ids
        assert clear_two["conversation_id"] not in remaining_ids
        denied_clear = assert_ok(
            request(
                base,
                "DELETE",
                "/api/conversations",
                user="u_store",
                body={"conversation_ids": [titled_conv["conversation_id"]]},
            )
        )
        assert denied_clear["closed_count"] == 0

        empty_integrations = assert_ok(request(base, "GET", "/api/integrations"))["integrations"]
        assert empty_integrations == []
        request(base, "GET", "/api/integrations", user="u_store", expected=403)
        integration_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "租户接入"}, expected=201)
        )["conversation"]
        setup_response = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{integration_conv['conversation_id']}/messages",
                body={"content": "帮我连接下这个租户"},
            )
        )
        assert setup_response["intent"] == "CONFIGURE_TENANT_INTEGRATION"
        assert setup_response["integration_setup"]["mode"] == "CREATE"
        mixed_credential_slots = server.parse_integration_credentials(
            "租户名称：顾家家居\n租户ID：kuka\nAppKey: transient-app-key\nAppSecret: transient-app-secret-value"
        )
        assert mixed_credential_slots["tenant_name"] == "顾家家居"
        assert mixed_credential_slots["tenant_code"] == "kuka"
        assert mixed_credential_slots["app_key"] == "transient-app-key"
        assert mixed_credential_slots["app_secret"] == "transient-app-secret-value"
        code_prefill_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "租户编号抽取"}, expected=201)
        )["conversation"]
        code_prefill_response = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{code_prefill_conv['conversation_id']}/messages",
                body={
                    "content": (
                        "我需要新增一个租户，下面是租户信息：\n"
                        "租户名称：顾家家居\n"
                        "租户ID：kuka\n"
                        "AppKey：[已隐藏，请使用安全配置卡]\n"
                        "AppSecret：[已隐藏，请使用安全配置卡]"
                    )
                },
            )
        )
        assert code_prefill_response["intent"] == "CONFIGURE_TENANT_INTEGRATION"
        assert code_prefill_response["integration_setup"]["prefill"] == {
            "tenant_name": "顾家家居",
            "tenant_code": "kuka",
        }
        assert "tenant_code" not in code_prefill_response["integration_setup"]["missing_fields"]
        assert "app_key" in code_prefill_response["integration_setup"]["missing_fields"]
        assert "app_secret" in code_prefill_response["integration_setup"]["missing_fields"]
        code_prefill_content = code_prefill_response["messages"][0]["content"]
        assert "租户名称“顾家家居”" in code_prefill_content
        assert "租户编码“kuka”" in code_prefill_content
        assert "顾家家居\n租户ID" not in code_prefill_content
        assert "顾家家居租户ID" not in code_prefill_content
        partial_prefill_response = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{integration_conv['conversation_id']}/messages",
                body={
                    "content": (
                        "我需要新增一个租户，下面是租户信息：\n"
                        "租户名称：顾家家居\n"
                        "AppKey：[已隐藏，请使用安全配置卡]\n"
                        "AppSecret：[已隐藏，请使用安全配置卡]"
                    )
                },
            )
        )
        assert partial_prefill_response["intent"] == "CONFIGURE_TENANT_INTEGRATION"
        assert partial_prefill_response["integration_setup"]["prefill"] == {"tenant_name": "顾家家居"}
        assert "tenant_code" in partial_prefill_response["integration_setup"]["missing_fields"]
        assert "app_key" in partial_prefill_response["integration_setup"]["missing_fields"]
        assert "app_secret" in partial_prefill_response["integration_setup"]["missing_fields"]
        assert partial_prefill_response["messages"][0]["linked_object"]["artifact"]["integrationSetup"]["auto_extract"] is True
        partial_prefill_serialized = json.dumps(partial_prefill_response, ensure_ascii=False)
        assert "顾家家居" in partial_prefill_serialized
        assert "安全配置卡]" not in json.dumps(partial_prefill_response["integration_setup"].get("prefill", {}), ensure_ascii=False)
        transient_key = "transient-app-key-no-history"
        transient_secret = "transient-app-secret-no-history"
        transient_response = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{integration_conv['conversation_id']}/messages",
                body={"content": f"AppKey: {transient_key}\nAppSecret: {transient_secret}"},
            )
        )
        transient_setup = transient_response["integration_setup"]
        assert transient_setup["prefill"]["tenant_name"] == "顾家家居"
        assert transient_setup["prefill"]["app_key"] == transient_key
        assert transient_setup["prefill"]["app_secret"] == transient_secret
        assert transient_setup["transient_secret_prefill"] is True
        transient_message_setup = transient_response["messages"][0]["linked_object"]["artifact"]["integrationSetup"]
        assert transient_message_setup["prefill"]["app_key"] == transient_key
        assert transient_message_setup["prefill"]["app_secret"] == transient_secret
        transient_detail = assert_ok(request(base, "GET", f"/api/conversations/{integration_conv['conversation_id']}"))
        transient_serialized = json.dumps(transient_detail, ensure_ascii=False)
        assert transient_key not in transient_serialized
        assert transient_secret not in transient_serialized
        raw_app_key = "test-app-key-should-not-persist"
        raw_app_secret = "short-secret"
        blocked_response = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{integration_conv['conversation_id']}/messages",
                body={"content": f"AppKey: {raw_app_key} AppSecret: {raw_app_secret} tenantCode: test_tenant"},
            )
        )
        assert blocked_response["intent"] == "CONFIGURE_TENANT_INTEGRATION"
        assert blocked_response["agent"]["status"] == "BLOCKED"
        setup_detail = assert_ok(request(base, "GET", f"/api/conversations/{integration_conv['conversation_id']}"))
        setup_serialized = json.dumps(setup_detail, ensure_ascii=False)
        assert raw_app_key not in setup_serialized
        assert raw_app_secret not in setup_serialized
        invalid_integration = request(
            base,
            "POST",
            "/api/integrations",
            body={"tenant_name": "bad", "tenant_code": "bad", "app_key": "short", "app_secret": "short"},
            expected=400,
        )
        assert invalid_integration["error"]["message"] == "AppKey 或 AppSecret 格式不正确"

        with sqlite3.connect(db_path) as integration_db:
            integration_db.execute(
                """INSERT INTO tenant_integrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "int_smoke", "tenant_smoke", "测试租户", "smok********e-key",
                    "encrypted-value-must-not-leak", "fingerprint-safe", "CHAT_SECURE_FORM",
                    "CONNECTED", 1, "2026-07-02T18:00:00+08:00", None, "u_admin",
                    "2026-07-02T18:00:00+08:00", "2026-07-02T18:00:00+08:00",
                ),
            )
            integration_db.execute(
                "INSERT INTO tenant_integration_stores VALUES (?,?,?,?,?,?,?,?)",
                ("int_smoke", "store_smoke", None, "测试门店", "store", "CONNECTED", 3, "2026-07-02T18:00:00+08:00"),
            )
        duplicate_integration = request(
            base,
            "POST",
            "/api/integrations",
            body={
                "tenant_name": "重复租户",
                "tenant_code": "tenant_smoke",
                "app_key": "valid-app-key",
                "app_secret": "valid-app-secret-123456",
            },
            expected=409,
        )
        assert duplicate_integration["error"]["code"] == "INTEGRATION_ALREADY_EXISTS"
        assert "已经接入" in duplicate_integration["error"]["message"]
        integration_list = assert_ok(request(base, "GET", "/api/integrations"))["integrations"]
        assert len(integration_list) == 1
        assert integration_list[0]["store_count"] == 1
        assert integration_list[0]["stores"][0]["name"] == "测试门店"
        assert "encrypted_credentials" not in integration_list[0]
        assert "encrypted-value-must-not-leak" not in json.dumps(integration_list)

        existing_raw_key = "existing-app-key-must-not-persist"
        existing_raw_secret = "existing-app-secret-must-not-persist"
        deduped_integration = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{integration_conv['conversation_id']}/messages",
                body={
                    "content": (
                        f"AppKey: {existing_raw_key} AppSecret: {existing_raw_secret} "
                        "tenantCode: tenant_smoke"
                    )
                },
            )
        )
        assert deduped_integration["intent"] == "CONFIGURE_TENANT_INTEGRATION"
        assert deduped_integration["agent"]["status"] == "SUCCEEDED"
        assert deduped_integration["integration"]["store_count"] == 1
        deduped_artifact = deduped_integration["messages"][0]["linked_object"]["artifact"]["integrationResult"]
        assert "stores" not in deduped_artifact
        assert deduped_artifact["store_count"] == 1
        with sqlite3.connect(db_path) as integration_db:
            encrypted_after_dedupe = integration_db.execute(
                "SELECT encrypted_credentials FROM tenant_integrations WHERE tenant_code='tenant_smoke'"
            ).fetchone()[0]
            assert encrypted_after_dedupe == "encrypted-value-must-not-leak"
        deduped_detail = assert_ok(request(base, "GET", f"/api/conversations/{integration_conv['conversation_id']}"))
        deduped_serialized = json.dumps(deduped_detail, ensure_ascii=False)
        assert existing_raw_key not in deduped_serialized
        assert existing_raw_secret not in deduped_serialized

        closable_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "closable"}, expected=201)
        )["conversation"]
        request(base, "DELETE", f"/api/conversations/{closable_conv['conversation_id']}", user="u_store", expected=404)
        closed = assert_ok(request(base, "DELETE", f"/api/conversations/{closable_conv['conversation_id']}"))
        assert closed["status"] == "CLOSED"
        request(base, "GET", f"/api/conversations/{closable_conv['conversation_id']}", expected=404)
        active_conversations = assert_ok(request(base, "GET", "/api/conversations"))["conversations"]
        assert not any(item["conversation_id"] == closable_conv["conversation_id"] for item in active_conversations)

        before_subs = assert_ok(request(base, "GET", "/api/subscriptions"))["subscriptions"]
        created = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{conv['conversation_id']}/messages",
                body={"content": "下周开始给广州悦汇城订阅离岗检测，每天 9 点到 22 点", "context": {"org_id": "org_gz"}},
            )
        )
        plan = created["plan"]
        assert plan["status"] == "READY_FOR_CONFIRM"
        assert plan["confirm_required"] is True
        after_plan_subs = assert_ok(request(base, "GET", "/api/subscriptions"))["subscriptions"]
        assert len(after_plan_subs) == len(before_subs), "subscription was created before user confirmation"

        confirmed = assert_ok(request(base, "POST", f"/api/plans/{plan['plan_id']}/confirm", body={}))
        assert confirmed["status"] == "ACTIVE"
        confirmed_again = assert_ok(request(base, "POST", f"/api/plans/{plan['plan_id']}/confirm", body={}))
        assert confirmed_again["deduped"] is True
        confirmed_plan = assert_ok(request(base, "GET", f"/api/plans/{plan['plan_id']}"))["plan"]
        assert confirmed_plan["status"] == "SUCCEEDED"
        assert confirmed_plan["result"]["status"] == "ACTIVE"
        assert confirmed_plan["result"]["subscription_id"] == confirmed["subscription_id"]
        final_subs = assert_ok(request(base, "GET", "/api/subscriptions"))["subscriptions"]
        assert len(final_subs) == len(before_subs) + 1
        assert len({sub["subscription_id"] for sub in final_subs}) == len(final_subs)

        cancel_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "取消待确认计划"}, expected=201)
        )["conversation"]
        cancel_ready = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{cancel_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每天中午12点到13点，每20分钟拉取一下店内所有镜头快照，看下地面是否干净，持续2天",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        cancel_plan = cancel_ready["plan"]
        assert cancel_plan["status"] == "READY_FOR_CONFIRM"
        cancelled_plan_response = assert_ok(request(base, "POST", f"/api/plans/{cancel_plan['plan_id']}/cancel", body={}))
        assert cancelled_plan_response["plan"]["status"] == "CANCELLED"
        assert "不会再次提醒确认" in cancelled_plan_response["message"]["content"]
        request(base, "POST", f"/api/plans/{cancel_plan['plan_id']}/confirm", body={}, expected=409)
        cancelled_detail = assert_ok(request(base, "GET", f"/api/conversations/{cancel_conv['conversation_id']}"))
        linked_statuses = [
            (message.get("linked_object") or {}).get("plan", {}).get("status")
            for message in cancelled_detail["messages"]
            if (message.get("linked_object") or {}).get("plan", {}).get("plan_id") == cancel_plan["plan_id"]
        ]
        assert linked_statuses and set(linked_statuses) == {"CANCELLED"}, linked_statuses

        visual_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "视觉合规"}, expected=201)
        )["conversation"]
        visual_plan_response = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{visual_conv['conversation_id']}/messages",
                body={
                    "content": "下周开始给广州悦汇城订阅门店视觉合规巡检，检查是否存在其他品牌Logo或宣传海报，每天 9 点到 22 点",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        assert visual_plan_response["intent"] == "SUBSCRIPTION_CREATE"
        visual_plan = visual_plan_response["plan"]
        assert visual_plan["status"] == "READY_FOR_CONFIRM"
        assert visual_plan["slots"]["capability"]["resolved_capability_id"] == "visual_compliance_inspection"
        visual_pack = visual_plan["slots"]["visual_compliance"]
        assert visual_pack["rules"][0]["rule_type"] == "FORBIDDEN_OBJECT_APPEAR"
        assert "其他品牌Logo" in visual_pack["forbidden_objects"]
        assert visual_plan["actions"][0]["params"]["thresholds"]["require_marked_anomaly_image"] is True
        visual_confirmed = assert_ok(request(base, "POST", f"/api/plans/{visual_plan['plan_id']}/confirm", body={}))
        assert visual_confirmed["status"] == "ACTIVE"
        visual_subs = assert_ok(request(base, "GET", "/api/subscriptions"))["subscriptions"]
        visual_subscription = next(item for item in visual_subs if item.get("capability_id") == "visual_compliance_inspection")
        assert visual_subscription["thresholds"]["visual_compliance"]["object_pack_update_policy"] == "APPROVAL_REQUIRED"

        scheduled_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "周期快照巡检"}, expected=201)
        )["conversation"]
        scheduled_missing = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{scheduled_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每隔3h看下门店的地面是否干净有垃圾，巡检周期为期一周，从今天开始",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        assert scheduled_missing["intent"] == "CREATE_SCHEDULED_INSPECTION"
        assert scheduled_missing["plan"]["status"] == "NEED_CLARIFICATION"
        assert scheduled_missing["plan"]["slots"]["missing_slots"] == ["daily_window"]
        assert "营业时间" in scheduled_missing["messages"][0]["content"]

        scheduled_ready = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{scheduled_conv['conversation_id']}/messages",
                body={"content": "按门店营业时间执行", "context": {"org_id": "org_gz"}},
            )
        )
        scheduled_plan = scheduled_ready["plan"]
        assert scheduled_plan["status"] == "READY_FOR_CONFIRM"
        assert scheduled_plan["slots"]["schedule"]["interval_minutes"] == 180
        assert scheduled_plan["slots"]["schedule"]["daily_window"]["mode"] == "business_hours"
        assert scheduled_plan["slots"]["time_range"]["start"]
        assert scheduled_plan["slots"]["time_range"]["end"]
        assert scheduled_plan["slots"]["camera_scope"]["resolved_ids"] == ["cam_gz_gate", "cam_gz_cashier"]

        batch_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "多门店周期巡检"}, expected=201)
        )["conversation"]
        batch_ready = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{batch_conv['conversation_id']}/messages",
                body={
                    "content": "帮我给当前租户所有门店每隔3小时看下门店地面是否干净有垃圾，巡检周期为期一周，从今天开始，按门店营业时间执行",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        assert batch_ready["intent"] == "BATCH_SCHEDULED_INSPECTION_CREATE"
        batch_plan = batch_ready["plan"]
        assert batch_plan["status"] == "READY_FOR_CONFIRM"
        assert batch_plan["confirm_required"] is True
        assert batch_plan["actions"][0]["tool"] == "batch_inspection.create"
        assert batch_plan["slots"]["batch"]["enabled"] is True
        assert batch_plan["slots"]["org_scope"]["scope_type"] == "multi_store"
        assert batch_plan["slots"]["org_scope"]["store_count"] == 7
        assert len(batch_plan["slots"]["camera_scope"]["store_tasks"]) == 7
        assert batch_plan["slots"]["camera_scope"]["online_camera_count"] == 9
        assert batch_plan["slots"]["camera_scope"]["offline_camera_count"] == 1
        assert batch_plan["slots"]["schedule"]["interval_minutes"] == 180
        assert batch_plan["slots"]["schedule"]["daily_window"]["mode"] == "business_hours"
        assert batch_plan["slots"]["missing_slots"] == []
        before_batches = assert_ok(request(base, "GET", "/api/inspection-batches"))["inspection_batches"]
        assert not any(item.get("plan_id") == batch_plan["plan_id"] for item in before_batches)
        batch_confirmed = assert_ok(request(base, "POST", f"/api/plans/{batch_plan['plan_id']}/confirm", body={}))
        batch = batch_confirmed["inspection_batch"]
        assert batch_confirmed["batch_id"] == batch["batch_id"]
        assert batch_confirmed["status"] == "RUNNING"
        assert batch["total_store_count"] == 7
        assert batch["success_store_count"] == 0
        assert batch["failed_store_count"] == 0
        assert batch["skipped_store_count"] == 0
        assert len(batch["items"]) == 7
        assert all(item["status"] == "RUNNING" for item in batch["items"])
        assert all(item["scheduled_task_id"] for item in batch["items"])
        assert all(item["scheduled_task"]["batch_id"] == batch["batch_id"] for item in batch["items"])
        assert all(item["scheduled_task"]["status"] == "ACTIVE" for item in batch["items"])
        assert all(item["scheduled_task"]["next_run_at"] for item in batch["items"])
        original_task_ids = {item["store_id"]: item["scheduled_task_id"] for item in batch["items"]}
        with sqlite3.connect(db_path) as idem_db:
            idem_db.execute(
                "UPDATE plans SET status='READY_FOR_CONFIRM', confirmed_at=NULL, result=NULL WHERE plan_id=?",
                (batch_plan["plan_id"],),
            )
        batch_again = assert_ok(request(base, "POST", f"/api/plans/{batch_plan['plan_id']}/confirm", body={}))
        assert batch_again["deduped"] is True
        assert batch_again["batch_id"] == batch["batch_id"]
        batch_plan_after_again = assert_ok(request(base, "GET", f"/api/plans/{batch_plan['plan_id']}"))["plan"]
        assert batch_plan_after_again["status"] == "SUCCEEDED"
        assert batch_plan_after_again["result"]["batch_id"] == batch["batch_id"]
        batch_messages = assert_ok(request(base, "GET", f"/api/conversations/{batch_conv['conversation_id']}"))["messages"]
        assert any(
            message.get("linked_plan_id") == batch_plan["plan_id"]
            and (message.get("linked_object") or {}).get("plan", {}).get("status") == "SUCCEEDED"
            for message in batch_messages
        )
        batch_list = assert_ok(request(base, "GET", "/api/inspection-batches"))["inspection_batches"]
        listed_batch = next(item for item in batch_list if item["batch_id"] == batch["batch_id"])
        assert listed_batch["total_store_count"] == 7
        batch_detail = assert_ok(request(base, "GET", f"/api/inspection-batches/{batch['batch_id']}"))["inspection_batch"]
        assert len(batch_detail["items"]) == 7
        assert batch_detail["scope_snapshot"]["org_scope"]["store_count"] == 7
        batch_cancelled = assert_ok(request(base, "POST", f"/api/inspection-batches/{batch['batch_id']}/cancel", body={}))["inspection_batch"]
        assert batch_cancelled["status"] == "CANCELLED"
        assert all(item["scheduled_task"]["status"] == "CANCELLED" for item in batch_cancelled["items"] if item.get("scheduled_task"))
        requeued = assert_ok(request(base, "POST", f"/api/plans/{batch_plan['plan_id']}/confirm", body={}))
        assert requeued["deduped"] is True
        assert requeued["requeued"] is True
        assert requeued["batch_id"] == batch["batch_id"]
        assert requeued["status"] == "RUNNING"
        requeued_batch = requeued["inspection_batch"]
        assert requeued_batch["success_store_count"] == 0
        assert requeued_batch["failed_store_count"] == 0
        assert requeued_batch["skipped_store_count"] == 0
        assert all(item["status"] == "RUNNING" for item in requeued_batch["items"])
        assert {item["store_id"]: item["scheduled_task_id"] for item in requeued_batch["items"]} == original_task_ids
        assert all(item["scheduled_task"]["status"] == "ACTIVE" for item in requeued_batch["items"])
        assert all(item["scheduled_task"]["next_run_at"] for item in requeued_batch["items"])
        batch_tasks = [
            task
            for task in assert_ok(request(base, "GET", "/api/scheduled-inspections"))["scheduled_inspections"]
            if task.get("batch_id") == batch["batch_id"]
        ]
        assert len(batch_tasks) == 7
        assert all(task["status"] == "ACTIVE" for task in batch_tasks)
        immediate_batch_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "多门店即时巡检"}, expected=201)
        )["conversation"]
        immediate_batch = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{immediate_batch_conv['conversation_id']}/messages",
                body={
                    "content": "帮我给当前租户所有门店立即检查地面是否干净有垃圾",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        assert immediate_batch["intent"] == "BATCH_INSPECTION_EXECUTE"
        immediate_plan = immediate_batch["plan"]
        assert immediate_plan["status"] == "READY_FOR_CONFIRM"
        assert immediate_plan["confirm_required"] is True
        assert immediate_plan["actions"][0]["tool"] == "batch_inspection.execute"
        assert immediate_plan["slots"]["batch"]["enabled"] is True
        assert immediate_plan["slots"]["batch"]["execution_mode"] == "immediate"
        assert immediate_plan["slots"]["schedule"]["mode"] == "one_off"
        assert immediate_plan["slots"]["org_scope"]["store_count"] == 7
        assert len(immediate_plan["slots"]["camera_scope"]["store_tasks"]) == 7
        assert immediate_plan["slots"]["camera_scope"]["online_camera_count"] == 9
        assert immediate_plan["slots"]["camera_scope"]["offline_camera_count"] == 1
        assert immediate_plan["slots"]["missing_slots"] == []

        store_batch_conv = assert_ok(
            request(base, "POST", "/api/conversations", user="u_store", body={"title": "门店负责人批量"}, expected=201)
        )["conversation"]
        store_batch = request(
            base,
            "POST",
            f"/api/conversations/{store_batch_conv['conversation_id']}/messages",
            user="u_store",
            body={
                "content": "帮我给当前租户所有门店每隔3小时检查地面是否有垃圾，巡检周期为期一周，从今天开始，按门店营业时间执行",
                "context": {"org_id": "org_gz"},
            },
            expected=403,
        )
        assert store_batch["error"]["code"] == "PERMISSION_DENIED"
        store_immediate_conv = assert_ok(
            request(base, "POST", "/api/conversations", user="u_store", body={"title": "门店负责人即时批量"}, expected=201)
        )["conversation"]
        store_immediate = request(
            base,
            "POST",
            f"/api/conversations/{store_immediate_conv['conversation_id']}/messages",
            user="u_store",
            body={
                "content": "帮我给当前租户所有门店立即检查地面是否有垃圾",
                "context": {"org_id": "org_gz"},
            },
            expected=403,
        )
        assert store_immediate["error"]["code"] == "PERMISSION_DENIED"

        explicit_window_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "显式中午时间窗"}, expected=201)
        )["conversation"]
        explicit_window = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{explicit_window_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每天中午12点～13点，每20分钟拉取一下店内所有镜头快照，看下有没有员工在店内吃东西，店内的屏幕有没有关闭，广告背景板的灯没有点亮的情况；为期2周时间；",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        explicit_plan = explicit_window["plan"]
        explicit_schedule = explicit_plan["slots"]["schedule"]
        assert explicit_window["intent"] == "CREATE_SCHEDULED_INSPECTION"
        assert explicit_plan["status"] == "READY_FOR_CONFIRM"
        assert explicit_schedule["interval_minutes"] == 20
        assert explicit_schedule["daily_window"]["mode"] == "daily_window"
        assert explicit_schedule["daily_window"]["start_time"] == "12:00"
        assert explicit_schedule["daily_window"]["end_time"] == "13:00"
        assert "12:00-13:00" in explicit_schedule["daily_window"]["label"]
        assert "营业时间" not in explicit_schedule["daily_window"]["label"]
        assert explicit_plan["slots"]["time_range"]["end"]
        assert "屏幕" in explicit_plan["slots"]["inspection_goal"]
        assert "广告背景板" in explicit_plan["slots"]["inspection_goal"]
        assert explicit_plan["slots"]["missing_slots"] == []

        mixed_window_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "显式时间窗优先"}, expected=201)
        )["conversation"]
        mixed_window = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{mixed_window_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每天中午12点～13点，每20分钟拉取一下店内所有镜头快照，看下店内有没有员工在吃东西，为期2周，按门店营业时间执行",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        mixed_schedule = mixed_window["plan"]["slots"]["schedule"]
        assert mixed_window["plan"]["status"] == "READY_FOR_CONFIRM"
        assert mixed_schedule["daily_window"]["mode"] == "daily_window"
        assert mixed_schedule["daily_window"]["start_time"] == "12:00"
        assert mixed_schedule["daily_window"]["end_time"] == "13:00"

        half_hour_batch_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "点半时间窗多门店巡检"}, expected=201)
        )["conversation"]
        half_hour_batch = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{half_hour_batch_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每天上午10点半到11点半，每10分钟轮询检查下所有门店的售后区域，是否存在员工玩手机以及员工空岗的场景，为期一星期",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        half_hour_plan = half_hour_batch["plan"]
        half_hour_schedule = half_hour_plan["slots"]["schedule"]
        assert half_hour_batch["intent"] == "BATCH_SCHEDULED_INSPECTION_CREATE"
        assert half_hour_plan["status"] == "READY_FOR_CONFIRM"
        assert half_hour_schedule["interval_minutes"] == 10
        assert half_hour_schedule["daily_window"]["mode"] == "daily_window"
        assert half_hour_schedule["daily_window"]["start_time"] == "10:30"
        assert half_hour_schedule["daily_window"]["end_time"] == "11:30"
        assert "每 10 分钟" in half_hour_schedule["label"]
        assert "10:30-11:30" in half_hour_schedule["label"]
        assert "10:00 执行" not in half_hour_schedule["label"]
        assert half_hour_schedule["estimated_runs"] > 7
        assert half_hour_plan["slots"]["roi"]["label"] == "售后区域"
        assert "员工玩手机" in half_hour_plan["slots"]["inspection_goal"]
        assert "员工空岗" in half_hour_plan["slots"]["inspection_goal"]

        scheduled_visual_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "周期视觉合规"}, expected=201)
        )["conversation"]
        scheduled_visual = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{scheduled_visual_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每隔3h看下门店是否存在其他品牌Logo或宣传海报，巡检周期为期一周，从今天开始，按门店营业时间执行",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        assert scheduled_visual["intent"] == "CREATE_SCHEDULED_INSPECTION"
        assert scheduled_visual["plan"]["status"] == "READY_FOR_CONFIRM"
        assert scheduled_visual["plan"]["slots"]["capability"]["name"] == "门店视觉合规巡检"
        assert scheduled_visual["plan"]["slots"]["thresholds"]["visual_compliance"]["rules"][0]["rule_type"] == "FORBIDDEN_OBJECT_APPEAR"

        fixed_daily_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "每日固定时间视觉合规"}, expected=201)
        )["conversation"]
        fixed_daily_missing = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{fixed_daily_conv['conversation_id']}/messages",
                body={
                    "content": "帮我每天早上10点看一下店铺里所有的监控摄像头，有没有存在OPPO之外的品牌logo",
                    "context": {"org_id": "org_gz"},
                },
            )
        )
        assert fixed_daily_missing["intent"] == "CREATE_SCHEDULED_INSPECTION"
        fixed_daily_plan = fixed_daily_missing["plan"]
        assert fixed_daily_plan["status"] == "NEED_CLARIFICATION"
        assert fixed_daily_plan["slots"]["missing_slots"] == ["effective_time_range"]
        assert fixed_daily_plan["slots"]["schedule"]["interval_minutes"] == 1440
        assert fixed_daily_plan["slots"]["schedule"]["daily_window"]["mode"] == "fixed_daily"
        assert fixed_daily_plan["slots"]["schedule"]["daily_window"]["fixed_time"] == "10:00"
        assert fixed_daily_plan["slots"]["capability"]["name"] == "门店视觉合规巡检"
        assert "其他品牌Logo" in fixed_daily_plan["slots"]["thresholds"]["visual_compliance"]["forbidden_objects"]
        assert "required_tools" not in fixed_daily_missing

        fixed_daily_ready = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{fixed_daily_conv['conversation_id']}/messages",
                body={"content": "到7月底", "context": {"org_id": "org_gz"}},
            )
        )
        assert fixed_daily_ready["intent"] == "CREATE_SCHEDULED_INSPECTION"
        assert fixed_daily_ready["plan"]["status"] == "READY_FOR_CONFIRM"
        assert fixed_daily_ready["plan"]["slots"]["schedule"]["daily_window"]["label"] == "每天 10:00 执行"
        assert fixed_daily_ready["plan"]["slots"]["schedule"]["estimated_runs"] > 0
        assert fixed_daily_ready["plan"]["slots"]["time_range"]["start"]
        assert fixed_daily_ready["plan"]["slots"]["time_range"]["end"]

        scheduled_confirmed = assert_ok(
            request(base, "POST", f"/api/plans/{scheduled_plan['plan_id']}/confirm", body={})
        )
        task = scheduled_confirmed["scheduled_task"]
        assert task["status"] == "ACTIVE"
        assert task["next_run_at"]
        scheduled_detail = assert_ok(request(base, "GET", f"/api/scheduled-inspections/{task['task_id']}"))
        assert scheduled_detail["scheduled_inspection"]["task_id"] == task["task_id"]
        scheduled_conversation = assert_ok(
            request(base, "GET", f"/api/conversations/{scheduled_conv['conversation_id']}")
        )
        latest_linked = scheduled_conversation["messages"][-1]["linked_object"]
        assert latest_linked["plan"]["status"] == "SUCCEEDED"
        assert latest_linked["scheduled_task"]["task_id"] == task["task_id"]

        run_id = "run_smoke_history"
        evidence_ids = [f"se_smoke_{index}" for index in range(1, 8)]
        with sqlite3.connect(db_path) as history_db:
            history_db.execute(
                """INSERT INTO inspection_runs(
                     run_id, task_id, scheduled_at, started_at, completed_at, status, attempt,
                     result_status, conclusion, confidence, business_reason, observations,
                     evidence_ids, model_version, error_message, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    task["task_id"],
                    "2026-07-02T14:00:00+08:00",
                    "2026-07-02T14:00:01+08:00",
                    "2026-07-02T14:00:42+08:00",
                    "SUCCEEDED",
                    1,
                    "POSITIVE",
                    "地面存在杂物，不符合清洁标准。",
                    0.99,
                    "观察到禁止出现的目标，判定为异常。",
                    json.dumps(["巡检镜头3地面有杂物"], ensure_ascii=False),
                    json.dumps(evidence_ids),
                    "smoke-vlm",
                    None,
                    "2026-07-02T14:00:01+08:00",
                ),
            )
            for index, evidence_id in enumerate(evidence_ids, start=1):
                history_db.execute(
                    "INSERT INTO scheduled_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        run_id,
                        task["task_id"],
                        "org_gz",
                        "广州悦汇城",
                        "cam_gz_gate" if index % 2 else "cam_gz_cashier",
                        f"巡检镜头{index}",
                        f"2026-07-02T14:00:{index:02d}+08:00",
                        os.path.join(tmp_dir.name, f"{evidence_id}.jpg"),
                        "image/jpeg",
                        f"sha256-{index}",
                        f"token-{index}",
                        1024 + index,
                        "2026-07-02T14:00:42+08:00",
                    ),
                )
            history_db.execute(
                "UPDATE inspection_runs SET anomaly_evidence_ids=? WHERE run_id=?",
                (json.dumps([evidence_ids[2]]), run_id),
            )

        closed_scheduled = assert_ok(
            request(base, "DELETE", f"/api/conversations/{scheduled_conv['conversation_id']}")
        )
        assert closed_scheduled["status"] == "CLOSED"
        inspection_history = assert_ok(
            request(base, "GET", "/api/inspection-runs?org_id=org_gz&page=1&page_size=10")
        )
        assert inspection_history["pagination"]["total"] == 1
        assert len(inspection_history["inspection_runs"]) == 1, "one run must render as one history record"
        history_record = inspection_history["inspection_runs"][0]
        assert history_record["run_id"] == run_id
        assert history_record["evidence_count"] == 7
        assert len(history_record["evidence"]) == 7, "seven camera frames must stay grouped under one run"
        assert [item["evidence_id"] for item in history_record["evidence"] if item["is_anomalous"]] == [evidence_ids[2]]
        inspection_detail = assert_ok(request(base, "GET", f"/api/inspection-runs/{run_id}"))["inspection_run"]
        assert inspection_detail["record_type"] == "AI_INSPECTION"
        assert len(inspection_detail["evidence"]) == 7
        assert inspection_detail["anomaly_evidence_ids"] == [evidence_ids[2]]
        request(base, "GET", f"/api/inspection-runs/{run_id}", user="u_store", expected=404)
        invalid_history_page = request(
            base,
            "GET",
            "/api/inspection-runs?org_id=org_gz&page=1&page_size=25",
            expected=400,
        )
        assert invalid_history_page["error"]["code"] == "BAD_REQUEST"

        paused = assert_ok(request(base, "POST", f"/api/scheduled-inspections/{task['task_id']}/pause", body={}))
        assert paused["scheduled_inspection"]["status"] == "PAUSED"
        assert paused["scheduled_inspection"]["next_run_at"] is None
        resumed = assert_ok(request(base, "POST", f"/api/scheduled-inspections/{task['task_id']}/resume", body={}))
        assert resumed["scheduled_inspection"]["status"] == "ACTIVE"
        cancelled = assert_ok(request(base, "POST", f"/api/scheduled-inspections/{task['task_id']}/cancel", body={}))
        assert cancelled["scheduled_inspection"]["status"] == "CANCELLED"
        cannot_resume = request(
            base,
            "POST",
            f"/api/scheduled-inspections/{task['task_id']}/resume",
            body={},
            expected=409,
        )
        assert cannot_resume["error"]["code"] == "VALIDATION_FAILED"
        task_subscriptions = assert_ok(request(base, "GET", "/api/subscriptions"))["subscriptions"]
        scheduled_subscription = next(item for item in task_subscriptions if item.get("task_id") == task["task_id"])
        assert scheduled_subscription["kind"] == "SCHEDULED_VISUAL"
        assert scheduled_subscription["status"] == "CANCELLED"

        agentic = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{conv['conversation_id']}/messages",
                body={"content": "下周开始在极狐汽车北京区域的所有门店上线离岗检测能力，巡检时间是上午9点到下午6点", "context": {"org_id": "org_bj"}},
            )
        )
        agentic_plan = agentic["plan"]
        assert agentic["intent"] == "SUBSCRIPTION_CREATE"
        assert agentic_plan["status"] == "READY_FOR_CONFIRM"
        assert agentic_plan["slots"]["schedule"]["start_time"] == "09:00"
        assert agentic_plan["slots"]["schedule"]["end_time"] == "18:00"
        assert agentic_plan["slots"]["org_scope"]["store_count"] == 2

        frontline_conv = assert_ok(request(base, "POST", "/api/conversations", user="u_frontline", body={"title": "deny"}, expected=201))["conversation"]
        denied = request(
            base,
            "POST",
            f"/api/conversations/{frontline_conv['conversation_id']}/messages",
            user="u_frontline",
            body={"content": "下周开始给广州悦汇城订阅离岗检测，每天 9 点到 22 点", "context": {"org_id": "org_gz"}},
            expected=403,
        )
        assert denied["error"]["code"] == "PERMISSION_DENIED"

        store_conv = assert_ok(request(base, "POST", "/api/conversations", user="u_store", body={"title": "scope"}, expected=201))["conversation"]
        scope_denied = request(
            base,
            "POST",
            f"/api/conversations/{store_conv['conversation_id']}/messages",
            user="u_store",
            body={"content": "昨天深圳前海店抽烟告警有哪些", "context": {"org_id": "org_gz"}},
            expected=403,
        )
        assert scope_denied["error"]["code"] == "TENANT_SCOPE_DENIED"

        query = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{conv['conversation_id']}/messages",
                body={"content": "昨天广州悦汇城离岗超过 5 分钟有哪些告警", "context": {"org_id": "org_gz"}},
            )
        )
        events = query["result"]["events"]
        assert len(events) >= 2
        assert all(event["evidence_count"] > 0 for event in events)

        paged = assert_ok(request(base, "GET", "/api/events?org_id=org_gz&page=1&page_size=10"))
        assert paged["pagination"]["page"] == 1
        assert paged["pagination"]["page_size"] == 10
        assert paged["pagination"]["total"] >= len(paged["events"])
        assert len(paged["events"]) <= 10
        invalid_page_size = request(base, "GET", "/api/events?org_id=org_gz&page=1&page_size=25", expected=400)
        assert invalid_page_size["error"]["code"] == "BAD_REQUEST"

        analytics = assert_ok(
            request(
                base,
                "POST",
                "/api/analytics/query",
                body={"question": "上周华东区抽烟告警最多的门店 Top10", "context": {"org_id": "org_hd"}},
            )
        )["analytics"]
        assert analytics["query_id"].startswith("qry_")
        assert analytics["ranking"], "analytics ranking should not be empty"
        assert analytics["scope"]["caliber"], "analytics result must include caliber"

        feedback = assert_ok(
            request(
                base,
                "POST",
                "/api/events/EV-10231/feedback",
                user="u_store",
                body={"feedback_type": "FALSE_POSITIVE", "reason": "摄像头遮挡", "description": "smoke test"},
                expected=201,
            )
        )
        assert feedback["status"] == "FALSE_POSITIVE"
        event_detail = assert_ok(request(base, "GET", "/api/events/EV-10231", user="u_store"))["event"]
        assert event_detail["status"] == "FALSE_POSITIVE"

        audits = assert_ok(request(base, "GET", "/api/audit-logs"))["audit_logs"]
        actions = {audit["action"] for audit in audits}
        assert "subscription.create" in actions
        assert "event.feedback.create" in actions
        assert "analytics.query" in actions

        invalid_web_search = request(
            base,
            "POST",
            "/api/agent/web-search/config",
            body={"provider": "tavily", "api_key": "", "max_results": 5, "timeout_seconds": 8},
            expected=400,
        )
        assert invalid_web_search["error"]["code"] == "BAD_REQUEST"
        request(
            base,
            "POST",
            "/api/agent/web-search/config",
            user="u_store",
            body={"provider": "tavily", "api_key": "denied", "max_results": 5, "timeout_seconds": 8},
            expected=403,
        )
        configured_web_search = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/web-search/config",
                body={
                    "provider": "tavily",
                    "api_key": "smoke-search-key-never-returned",
                    "max_results": 3,
                    "country": "CN",
                    "search_lang": "zh-hans",
                    "timeout_seconds": 5,
                },
            )
        )
        assert configured_web_search["configured"] is True
        assert configured_web_search["source"] == "tenant_config"
        assert configured_web_search["country"] == "CN"
        assert configured_web_search["search_lang"] == "zh-hans"
        assert configured_web_search["timeout_seconds"] == 5
        assert "api_key" not in json.dumps(configured_web_search, ensure_ascii=False)
        retained_web_search = assert_ok(
            request(
                base,
                "POST",
                "/api/agent/web-search/config",
                body={
                    "provider": "tavily",
                    "api_key": "",
                    "max_results": 5,
                    "country": "CN",
                    "search_lang": "zh-hans",
                    "timeout_seconds": 8,
                },
            )
        )
        assert retained_web_search["max_results"] == 5
        configured_catalog = assert_ok(request(base, "GET", "/api/agent/catalog"))
        assert configured_catalog["web_search"]["configured"] is True
        assert configured_catalog["web_search"]["source"] == "tenant_config"
        configured_audits = assert_ok(request(base, "GET", "/api/audit-logs"))["audit_logs"]
        assert any(item["action"] == "agent.web_search.configure" for item in configured_audits)

        document_conv = assert_ok(
            request(base, "POST", "/api/conversations", body={"title": "开放问答 PDF"}, expected=201)
        )["conversation"]
        document_answer = assert_ok(
            request(
                base,
                "POST",
                f"/api/conversations/{document_conv['conversation_id']}/messages",
                body={
                    "content": "帮我整理一份六天旅行清单，并生成 PDF 文档",
                    "context": {"mode_override": "OPEN_QA"},
                },
            )
        )
        document_message = document_answer["messages"][0]
        document_artifact = document_message["linked_object"]["artifact"]["generatedDocument"]
        assert document_answer["requested_output_format"] == "PDF"
        assert "document.generate_pdf" in document_answer["agent"]["tool_calls"]
        assert document_artifact["mime_type"] == "application/pdf"
        assert document_artifact["size_bytes"] > 500
        pdf_data, pdf_headers = request_bytes(base, document_artifact["download_url"])
        assert pdf_data.startswith(b"%PDF-")
        assert pdf_headers.get_content_type() == "application/pdf"
        assert pdf_headers.get("Cache-Control") == "private, no-store"
        request(base, "GET", document_artifact["download_url"], user="u_store", expected=404)
        document_audits = assert_ok(request(base, "GET", "/api/audit-logs"))["audit_logs"]
        assert any(item["action"] == "agent.document.download" for item in document_audits)

        print("PASS smoke tests: frontend contracts, plans, grouped inspection history, permissions, evidence, analytics, feedback, audit, redaction, open-QA PDF")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        tmp_dir.cleanup()


if __name__ == "__main__":
    main()
