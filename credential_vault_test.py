#!/usr/bin/env python3
"""Focused tests for encrypted integration credential storage."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from credential_vault import CredentialVault
import server
from server import ApiError, integration_setup_request, parse_integration_credentials, redact_integration_message


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        key_path = Path(temp_dir) / ".master_key"
        vault = CredentialVault(key_path)
        credential = {"app_key": "example-app-key", "app_secret": "example-app-secret-value"}
        encrypted = vault.encrypt(credential)
        assert "example-app-key" not in encrypted
        assert "example-app-secret-value" not in encrypted
        assert vault.decrypt(encrypted) == credential
        assert os.stat(key_path).st_mode & 0o777 == 0o600
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE users(user_id TEXT PRIMARY KEY, name TEXT, role TEXT, tenant_id TEXT, allowed_org_ids TEXT);
            CREATE TABLE tenant_integrations(
              integration_id TEXT PRIMARY KEY, tenant_code TEXT, tenant_name TEXT, app_key_masked TEXT,
              encrypted_credentials TEXT, credential_fingerprint TEXT, source TEXT, status TEXT,
              store_count INTEGER, last_synced_at TEXT, last_error TEXT, created_by TEXT,
              created_at TEXT, updated_at TEXT
            );
            CREATE TABLE service_configs(
              config_key TEXT PRIMARY KEY,
              encrypted_value TEXT NOT NULL,
              public_metadata TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE web_search_usage(
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
            """
        )
        conn.execute("INSERT INTO users VALUES (?,?,?,?,?)", ("u_admin", "管理员", "tenant_admin", "fixture", '["*"]'))
        conn.execute(
            "INSERT INTO tenant_integrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "int_two", "tenant_two", "租户二", "exam********-key", encrypted, "fp-two",
                "CHAT_SECURE_FORM", "CONNECTED", 1, None, None, "u_admin", "now", "now",
            ),
        )
        server._CREDENTIAL_VAULT = vault
        server._TENANT_AGENT_CACHE.clear()
        tenant_agent = server.online_agent_for_tenant(conn, "tenant_two", required=True)
        assert tenant_agent.tenant_code == "tenant_two"
        model_api_key = "visual-model-key-must-remain-encrypted"
        saved_model = server.save_model_runtime_config(
            conn,
            {
                "api_key": model_api_key,
                "model": "Qwen3-VL-8B-Instruct-FP8",
                "chat_completions_url": "http://model.example.test/v1/chat/completions",
                "auth_scheme": "raw",
                "max_images": 8,
            },
        )
        conn.commit()
        assert saved_model["configured"] is True
        stored_model = conn.execute(
            "SELECT encrypted_value, public_metadata FROM service_configs WHERE config_key='visual_model'"
        ).fetchone()
        assert model_api_key not in stored_model["encrypted_value"]
        assert model_api_key not in stored_model["public_metadata"]
        runtime_config, cache_token = server.model_runtime_config(conn)
        assert cache_token != "unconfigured"
        assert runtime_config["api_key"] == model_api_key
        assert runtime_config["model"] == "Qwen3-VL-8B-Instruct-FP8"
        assert runtime_config["auth_scheme"] == "raw"
        search_api_key = "public-search-key-must-remain-encrypted"
        saved_search = server.save_web_search_runtime_config(
            conn,
            {
                "provider": "tavily",
                "api_key": search_api_key,
                "max_results": 4,
                "country": "CN",
                "search_lang": "zh-hans",
                "timeout_seconds": 8,
            },
        )
        conn.commit()
        assert saved_search["configured"] is True
        stored_search = conn.execute(
            "SELECT encrypted_value, public_metadata FROM service_configs WHERE config_key='web_search'"
        ).fetchone()
        assert search_api_key not in stored_search["encrypted_value"]
        assert search_api_key not in stored_search["public_metadata"]
        search_config, search_cache_token = server.web_search_runtime_config(conn)
        assert search_cache_token != "unconfigured"
        assert search_config["api_key"] == search_api_key
        assert search_config["provider"] == "tavily"
        configured_agent = server.online_agent_for_tenant(conn, "tenant_two", required=True)
        assert configured_agent.analyzer.configured is True
        assert configured_agent.visual_reasoner.configured is True
        assert configured_agent.visual_reasoner.model == "Qwen3-VL-8B-Instruct-FP8"
        assert configured_agent.open_responder.web_search_client.configured is True
        low_balance_response = {
            "assistant_content": "已返回公开来源。",
            "agent": {"tool_calls": ["web.search"]},
            "web_search": {
                "provider": "tavily",
                "request_id": "usage-contract-1",
                "usage": {"credits": 3},
                "usage_events": [
                    {"provider": "tavily", "request_id": "usage-contract-1", "status": "SUCCEEDED", "credits": 1},
                    {"provider": "tavily", "request_id": "usage-contract-2", "status": "SUCCEEDED", "credits": 1},
                    {"provider": "tavily", "request_id": "usage-contract-3", "status": "SUCCEEDED", "credits": 1},
                ],
                "account_usage": {"used_credits": 901, "credit_limit": 1000, "remaining_credits": 99},
            },
        }
        low_balance_summary = server.apply_web_search_usage_to_response(
            conn,
            {"tenant_id": "tenant_two"},
            "conv_usage_contract",
            low_balance_response,
        )
        assert low_balance_summary["calls_this_month"] == 3
        assert low_balance_summary["credits_this_month"] == 3
        assert low_balance_summary["remaining_credits"] == 99
        assert low_balance_summary["low_balance"] is True
        assert "额度提醒：公共网页检索可用额度剩余 99/1000 Credits" in low_balance_response["assistant_content"]
        public_search_config = server.public_web_search_config(conn, "tenant_two")
        assert public_search_config["usage"]["remaining_credits"] == 99
        assert search_api_key not in str(public_search_config)
        selected_user = server.user_from_request(
            SimpleNamespace(headers={"X-User-Id": "u_admin", "X-Tenant-Code": "tenant_two"}),
            conn,
        )
        assert selected_user["tenant_id"] == "tenant_two"
        assert selected_user["name"] == "租户二 租户管理员"
        # Bootstrap persists a user's local tenant and the SPA sends it back
        # on subsequent calls.  That must remain an ACL-scoped local tenant,
        # not be forced through the external-integration lookup.
        original_online_lookup = server.online_agent_for_tenant
        own_tenant_calls = []
        try:
            def own_tenant_lookup(_conn, tenant_code, required=False):
                own_tenant_calls.append((tenant_code, required))
                return None

            server.online_agent_for_tenant = own_tenant_lookup
            own_tenant_user = server.user_from_request(
                SimpleNamespace(headers={"X-User-Id": "u_admin", "X-Tenant-Code": "fixture"}),
                conn,
            )
        finally:
            server.online_agent_for_tenant = original_online_lookup
        assert own_tenant_user["tenant_id"] == "fixture"
        assert own_tenant_calls == [("fixture", False)]
        try:
            server.online_agent_for_tenant(conn, "tenant_missing", required=True)
            raise AssertionError("unknown tenant must be rejected")
        except ApiError as exc:
            assert exc.code == "TENANT_SCOPE_DENIED"
    credential_message = "AppKey: sample-key-value AppSecret: sample-secret-value-123 tenantCode: sample_tenant"
    assert integration_setup_request(credential_message)
    assert integration_setup_request("帮我连接下这个租户")
    parsed = parse_integration_credentials(credential_message)
    assert parsed["tenant_code"] == "sample_tenant"
    assert parsed["tenant_name"] == "sample_tenant"
    assert parsed["app_key"] == "sample-key-value"
    assert parsed["app_secret"] == "sample-secret-value-123"
    redacted = redact_integration_message(credential_message)
    assert "sample-key-value" not in redacted
    assert "sample-secret-value-123" not in redacted
    print("PASS credential vault tests: encrypted round-trip, plaintext redaction, key permissions")


if __name__ == "__main__":
    main()
