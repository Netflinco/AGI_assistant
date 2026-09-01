#!/usr/bin/env python3
"""No-network HTTP smoke for E2E-001/002/004/005 and ACL gates."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import zipfile
from http.client import HTTPConnection
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from tests.fake_services import FakeTavilyGateway, fake_preview, fake_renderer


def request(port: int, method: str, path: str, body: bytes | dict | None = None, *, user: str | None = "u_admin", headers: dict | None = None):
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, dict) else (body or b"")
    merged = {"Content-Length": str(len(payload))}
    if user:
        merged["X-User-Id"] = user
    if isinstance(body, dict):
        merged["Content-Type"] = "application/json"
    if headers:
        merged.update(headers)
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=payload, headers=merged)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw.decode("utf-8")) if "application/json" in (response.getheader("Content-Type") or "") else raw


with tempfile.TemporaryDirectory(prefix="agi-http-smoke-") as _tmp:
    os.environ["AGI_INSPECTION_DB"] = str(Path(_tmp) / "test.db")
    os.environ["AGI_OFFICE_ASSET_DIR"] = str(Path(_tmp) / "assets")
    os.environ["AGI_OFFICE_UPLOAD_STAGING_DIR"] = str(Path(_tmp) / "upload-staging")
    import server
    from office_agent.jobs import OfficeJobService

    server.init_db(reset=True)
    release_citations = [{"title": "《长安的荔枝》已于2025年7月18日在中国大陆上映", "url": "https://www.news.cn/ent/changan-release", "publisher": "新华网", "published_at": "2026-08-18T10:00:00+00:00", "source_tier": "OFFICIAL", "content": "《长安的荔枝》已于2025年7月18日在中国大陆上映。"}]
    policy_citations = [{"title": "某机构最新政策", "url": "https://www.news.cn/policy/latest", "publisher": "新华网", "published_at": "2026-08-18T10:00:00+00:00", "source_tier": "OFFICIAL", "content": "某机构最新政策现行有效。"}]

    class QueryAwareGateway(FakeTavilyGateway):
        def search(self, query, *, freshness, topic, include_domains=()):
            self.calls.append({"query": query, "freshness": freshness, "topic": topic, "include_domains": list(include_domains)})
            return {
                "provider": "tavily", "request_id": "fake_req", "fetched_at": "2026-08-18T12:00:00+00:00",
                "citations": policy_citations if "政策" in query else release_citations,
            }

    fake = QueryAwareGateway()
    server.OPEN_RESEARCH_GATEWAY_FACTORY = lambda _conn, _user: fake

    def office_service(conn, user):
        service = OfficeJobService(conn, server.office_asset_service_for_request(conn, user), Path(_tmp) / "artifacts", renderer=fake_renderer)
        service._create_preview = fake_preview
        return service
    server.office_job_service_for_request = office_service
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # GATE-001: authenticate before reading/staging an Office body.
        raw_fixture = b"PK\x03\x04" + b"not-an-office-document"
        status, unauthenticated = request(port, "POST", "/api/office/assets", raw_fixture, user=None, headers={"Content-Type": "application/octet-stream", "X-File-Name": "no-auth.xlsx"})
        assert status == 401 and unauthenticated["error"]["code"] == "AUTH_REQUIRED"
        assert not server.OFFICE_UPLOAD_STAGING_DIR.exists() or not list(server.OFFICE_UPLOAD_STAGING_DIR.glob("*.part"))
        status, created = request(port, "POST", "/api/conversations", {"title": "P0 smoke"})
        assert status == 201
        conversation_id = created["data"]["conversation"]["conversation_id"]
        # The tenant feature centre exposes only non-secret policy metadata,
        # stays administrator-only, and refuses P0-locked data flows.
        status, flag_settings = request(port, "GET", "/api/agent/feature-flags")
        definitions = flag_settings["data"]["definitions"]
        assert status == 200 and any(item["flag"] == "open_research_enabled" for item in definitions)
        assert all("confirmation" in item and "description" in item for item in definitions)
        status, flags_denied = request(port, "GET", "/api/agent/feature-flags", user="u_region")
        assert status == 403 and flags_denied["error"]["code"] == "PERMISSION_DENIED"
        status, locked_egress = request(port, "POST", "/api/agent/feature-flags", {"flags": {"office_to_research_egress_enabled": True}})
        assert status == 409 and locked_egress["error"]["code"] == "FEATURE_FLAG_LOCKED_P0"
        status, alias_created = request(port, "POST", "/api/open-research/entity-aliases", {"alias_text": "暮光之诚", "canonical_entity": "暮光之城", "confidence": 0.97, "reason": "COMMON_TYPO"})
        assert status == 201 and alias_created["data"]["status"] == "ACTIVE"
        status, aliases_denied = request(port, "GET", "/api/open-research/entity-aliases", user="u_region")
        assert status == 403 and aliases_denied["error"]["code"] == "PERMISSION_DENIED"
        status, response = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "《长安的离职》什么时候上映？", "context": {"mode_override": "AUTO"}})
        research = response["data"]["research"]
        # GATE-OR-207: HTTP/UI delivery must prove the final fact, not merely
        # that rewrite ran.  This exercises a fresh server process and the
        # persisted assistant artifact used by the browser.
        assistant_message = response["data"]["messages"][0]
        artifact = (assistant_message.get("artifact") or assistant_message.get("linked_object", {}).get("artifact") or {})["research"]
        assert status == 200 and research["rewrite"]["applied"] and fake.calls
        assert research["status"] == "VERIFIED" and research["fact_intent"] == "EVENT_DATE"
        assert research["answer"]["claims"][0]["value"] == "2025-07-18"
        assert research["answer"]["claims"][0]["territory"] == "CN-MAINLAND"
        assert artifact["answer"]["claims"][0]["value"] == "2025-07-18" and artifact["citations"][0]["canonical_url"].startswith("https://")
        run_id = research["run_id"]
        evidence_id = research["citations"][0]["evidence_id"]
        # GATE-OR-226..228: the dedicated record projection is paginated and
        # private. It is assembled from final chat delivery only, so it must
        # not expose trace/provider payloads or page bodies.
        status, record_page = request(port, "GET", "/api/open-research/records?fact_intent=EVENT_DATE&page=1&page_size=20")
        records = record_page["data"]["records"]
        assert status == 200 and any(item["run_id"] == run_id for item in records)
        status, record_detail = request(port, "GET", f"/api/open-research/records/{run_id}")
        delivered_record = record_detail["data"]["record"]
        encoded_record = json.dumps(delivered_record, ensure_ascii=False)
        assert status == 200 and delivered_record["answer"]["claims"][0]["value"] == "2025-07-18"
        assert "provider_requests" not in encoded_record and "<html" not in encoded_record
        status, foreign_page = request(port, "GET", "/api/open-research/records?page=1", user="u_region")
        assert status == 200 and foreign_page["data"]["records"] == []
        status, foreign_detail = request(port, "GET", f"/api/open-research/records/{run_id}", user="u_region")
        assert status == 404 and foreign_detail["error"]["code"] == "RESOURCE_NOT_FOUND"
        status, source_open = request(port, "POST", f"/api/open-research/runs/{run_id}/source-open", {"evidence_id": evidence_id})
        assert status == 201 and source_open["data"]["interaction_id"]
        status, source_open_denied = request(port, "POST", f"/api/open-research/runs/{run_id}/source-open", {"evidence_id": evidence_id}, user="u_region")
        assert status == 404 and source_open_denied["error"]["code"] == "RESOURCE_NOT_FOUND"
        prior_calls = len(fake.calls)
        status, refined = request(port, "POST", f"/api/open-research/runs/{run_id}/refine", {})
        new_run_id = refined["data"]["research"]["run_id"]
        assert status == 200 and new_run_id != run_id and refined["data"]["messages"] and len(fake.calls) > prior_calls
        status, refined_record = request(port, "GET", f"/api/open-research/records/{new_run_id}")
        assert status == 200 and refined_record["data"]["record"]["force_fresh"]
        # An evidence-first request must not fall through to the legacy
        # OPEN_QA adapter when it is configured with Brave.  P0 either uses
        # Tavily or returns a clear, zero-egress availability status.
        original_factory = server.OPEN_RESEARCH_GATEWAY_FACTORY
        original_provider = os.environ.get("AGENT_WEB_SEARCH_PROVIDER")
        original_key = os.environ.get("AGENT_WEB_SEARCH_API_KEY")
        try:
            server.OPEN_RESEARCH_GATEWAY_FACTORY = None
            os.environ["AGENT_WEB_SEARCH_PROVIDER"] = "brave"
            os.environ["AGENT_WEB_SEARCH_API_KEY"] = "test_brave_key"
            status, non_tavily = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "某机构最新政策是什么？", "context": {"mode_override": "AUTO"}})
            assert status == 200 and non_tavily["data"]["research"]["status"] == "SEARCH_UNAVAILABLE"
            assert len(fake.calls) == prior_calls + 1
        finally:
            server.OPEN_RESEARCH_GATEWAY_FACTORY = original_factory
            if original_provider is None:
                os.environ.pop("AGENT_WEB_SEARCH_PROVIDER", None)
            else:
                os.environ["AGENT_WEB_SEARCH_PROVIDER"] = original_provider
            if original_key is None:
                os.environ.pop("AGENT_WEB_SEARCH_API_KEY", None)
            else:
                os.environ["AGENT_WEB_SEARCH_API_KEY"] = original_key
        status, denied = request(port, "GET", f"/api/open-research/runs/{run_id}", user="u_region")
        assert status == 404 and denied["error"]["code"] == "RESOURCE_NOT_FOUND"
        from openpyxl import Workbook
        book = Workbook(); book.active.append(["指标", "数值"]); book.active.append(["收入", 8])
        stream = io.BytesIO(); book.save(stream)
        boundary = b"----agi-smoke"
        multipart = b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"files\"; filename=\"weekly.xlsx\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n" + stream.getvalue() + b"\r\n--" + boundary + b"--\r\n"
        status, upload = request(port, "POST", "/api/office/assets", multipart, headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
        assert status == 201
        assert not list(server.OFFICE_UPLOAD_STAGING_DIR.glob("*.part")), "request staging must not become retained storage"
        asset_id = upload["data"]["assets"][0]["asset_id"]
        # Cross the multipart reader's 64 KiB boundary with a harmless OOXML
        # container; this validates streaming form-data rather than only the
        # small-body happy path.
        large_zip = io.BytesIO()
        with zipfile.ZipFile(large_zip, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("[Content_Types].xml", b"x")
            archive.writestr("xl/workbook.xml", b"x")
            archive.writestr("xl/media/payload.bin", b"x" * (96 * 1024))
        streamed = b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"files\"; filename=\"streamed.xlsx\"\r\n\r\n" + large_zip.getvalue() + b"\r\n--" + boundary + b"--\r\n"
        status, streamed_upload = request(port, "POST", "/api/office/assets", streamed, headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
        assert status == 201 and streamed_upload["data"]["assets"][0]["byte_size"] > 64 * 1024
        assert not list(server.OFFICE_UPLOAD_STAGING_DIR.glob("*.part"))
        status, office_msg = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "整理成管理层 PPT", "attachment_ids": [asset_id], "context": {"mode_override": "AUTO"}})
        job_id = office_msg["data"]["office"]["job"]["job_id"]
        status, completed = request(port, "POST", f"/api/office/jobs/{job_id}/run", {})
        assert status == 200 and completed["data"]["job"]["status"] == "SUCCEEDED"
        status, hybrid = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "查最新政策并做 PPT", "context": {"mode_override": "AUTO"}})
        assert status == 200, hybrid
        hybrid_data = hybrid["data"]
        assert status == 200 and hybrid_data["intent"] == "RESEARCH_TO_OFFICE"
        workflow = hybrid_data["workflow"]
        hybrid_job = hybrid_data["office"]["job"]
        assert workflow["research_run_id"] and workflow["office_job_id"] == hybrid_job["job_id"]
        status, workflow_view = request(port, "GET", f"/api/agent/workflows/{workflow['workflow_id']}")
        assert status == 200 and workflow_view["data"]["workflow"]["workflow_id"] == workflow["workflow_id"]
        status, effectiveness = request(port, "GET", "/api/agent/effectiveness")
        metrics = effectiveness["data"].get("metrics") or {}
        assert status == 200 and metrics["research"]["tavily_credits"] >= 0 and metrics["research"]["source_open_count"] == 1 and metrics["research"]["no_evidence_deterministic_count"] == 0
        status, effectiveness_denied = request(port, "GET", "/api/agent/effectiveness", user="u_region")
        assert status == 403 and effectiveness_denied["error"]["code"] == "PERMISSION_DENIED"
        status, completed_hybrid = request(port, "POST", f"/api/office/jobs/{hybrid_job['job_id']}/run", {})
        assert status == 200 and completed_hybrid["data"]["job"]["status"] == "SUCCEEDED"
        status, blocked = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "用这份 Excel 搜索竞品后做 PPT", "attachment_ids": [asset_id], "context": {"mode_override": "AUTO"}})
        assert status == 200 and blocked["data"]["reason_code"] == "OFFICE_TO_RESEARCH_EGRESS_DISABLED"
        before = len(fake.calls)
        status, inspection = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "《长安的离职》什么时候上映？", "context": {"mode_override": "INSPECTION"}})
        # GATE-OR-208: inspection remains a hard local mode lock.
        assert status == 200 and len(fake.calls) == before and inspection["data"]["intent"] != "OPEN_RESEARCH"
        # Feature fallbacks stay explicit and do not call Tavily/Office or
        # retain stream staging data when a domain is disabled.
        status, _flags = request(port, "POST", "/api/agent/feature-flags", {"flags": {"open_research_enabled": False, "office_enabled": False}})
        assert status == 200 and "research_to_office_enabled" in _flags["data"]["forced_disabled"]
        assert _flags["data"]["history"]
        before = len(fake.calls)
        status, disabled_research = request(port, "POST", f"/api/conversations/{conversation_id}/messages", {"content": "最新政策是什么？", "context": {"mode_override": "AUTO"}})
        assert status == 200 and disabled_research["data"]["research"]["reason_code"] == "FEATURE_DISABLED" and len(fake.calls) == before
        status, disabled_office = request(port, "POST", "/api/office/assets", multipart, headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
        assert status == 409 and disabled_office["error"]["code"] == "FEATURE_DISABLED"
        assert not list(server.OFFICE_UPLOAD_STAGING_DIR.glob("*.part"))
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)

print("PASS cross-domain smoke: rewrite, Office private pipeline, egress block, ACL and inspection isolation")
