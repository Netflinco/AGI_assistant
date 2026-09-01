#!/usr/bin/env python3
"""G0/G1/G3/G4/G7 unit regression for the shared P0 control plane."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

with tempfile.TemporaryDirectory(prefix="agi-governance-") as _tmp:
    os.environ["AGI_INSPECTION_DB"] = str(Path(_tmp) / "test.db")
    import server
    from agent_governance.contracts import GateContext, GateDecision, GateError
    from agent_governance.gate_engine import GateEngine
    from agent_governance.policy_registry import FeatureFlagPolicyError, apply_feature_updates, feature_enabled, set_feature
    from open_research.intent import DomainRouter, EntityResolver

    server.init_db(reset=True)
    conn = server.connect()
    user = server.one(conn, "SELECT * FROM users WHERE user_id='u_admin'")
    assert feature_enabled(conn, user["tenant_id"], "open_research_enabled") is True
    set_feature(conn, user["tenant_id"], "open_research_enabled", False, server.now_iso())
    assert feature_enabled(conn, user["tenant_id"], "open_research_enabled") is False
    context = GateContext("req_test", user["tenant_id"], user["user_id"], "conv", "OPEN_RESEARCH", "SEARCH", input_summary_hash="hash")
    engine = GateEngine(conn, now=server.now_iso())
    try:
        engine.evaluate(context, [("G0", lambda: GateDecision("G0", "BLOCK", "FEATURE_DISABLED"))])
        raise AssertionError("blocked gate must stop execution")
    except GateError as exc:
        assert exc.decision.reason_code == "FEATURE_DISABLED"
    assert server.one(conn, "SELECT decision FROM agent_gate_decisions WHERE request_id='req_test'")["decision"] == "BLOCK"
    set_feature(conn, user["tenant_id"], "open_research_enabled", True, server.now_iso())
    # Tenant switches must remain fail-closed: P0-locked flags cannot be
    # enabled, children require their parents, and disabling a parent closes
    # dependent capabilities in the same transaction.
    try:
        apply_feature_updates(conn, user["tenant_id"], {"office_to_research_egress_enabled": True}, server.now_iso())
        raise AssertionError("P0 locked egress switch must not be enabled")
    except FeatureFlagPolicyError as exc:
        assert exc.code == "FEATURE_FLAG_LOCKED_P0"
    set_feature(conn, user["tenant_id"], "office_enabled", False, server.now_iso())
    try:
        apply_feature_updates(conn, user["tenant_id"], {"research_to_office_enabled": True}, server.now_iso())
        raise AssertionError("child capability must require enabled parents")
    except FeatureFlagPolicyError as exc:
        assert exc.code == "FEATURE_FLAG_DEPENDENCY_REQUIRED"
    change = apply_feature_updates(conn, user["tenant_id"], {"office_enabled": True, "research_to_office_enabled": True}, server.now_iso())
    assert change["after"]["research_to_office_enabled"] is True
    change = apply_feature_updates(conn, user["tenant_id"], {"office_enabled": False}, server.now_iso())
    assert change["after"]["office_enabled"] is False
    assert change["after"]["research_to_office_enabled"] is False
    assert "research_to_office_enabled" in change["forced_disabled"]
    set_feature(conn, user["tenant_id"], "office_enabled", True, server.now_iso())
    set_feature(conn, user["tenant_id"], "research_to_office_enabled", True, server.now_iso())
    router = DomainRouter()
    assert router.classify("广州门店最新告警怎样？")["domain"] == "INSPECTION"
    event_route = router.classify("《长安的离职》什么时候上映？")
    assert event_route["domain"] == "OPEN_RESEARCH"
    assert event_route["task_type"] == "EVENT_STATUS" and event_route["evidence_required"] is True
    assert router.classify("今天的天气如何？")["domain"] == "FALLBACK"
    assert router.classify("请联网核验今天的天气如何？")["domain"] == "OPEN_RESEARCH"
    assert router.classify("把这份周报做成 PPT", attachment_ids=["asset_a"])["domain"] == "OFFICE"
    assert router.classify("查最新政策并做 PPT")["domain"] == "HYBRID"
    assert router.classify("《长安的离职》什么时候上映？", mode_override="INSPECTION")["domain"] == "INSPECTION"
    rewrite = EntityResolver().resolve("《长安的离职》什么时候上映？")
    assert rewrite.applied and rewrite.rewritten_query == "《长安的荔枝》什么时候上映？" and rewrite.reason == "HOMOPHONIC_TYPO"
    governed = EntityResolver({"暮光之诚": ("暮光之城", 0.97, "COMMON_TYPO")}).resolve("《暮光之诚》什么时候上映？")
    assert governed.applied and governed.rewritten_query == "《暮光之城》什么时候上映？" and governed.reason == "COMMON_TYPO"
    conn.execute(
        """INSERT INTO open_research_entity_aliases VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("eralias_test", user["tenant_id"], "冰与火之哥", "冰与火之歌", 0.96, "COMMON_TYPO", "ACTIVE", user["user_id"], server.now_iso(), server.now_iso()),
    )
    runtime_resolver = server.open_research_service_for_request(conn, user).resolver
    registry_rewrite = runtime_resolver.resolve("《冰与火之哥》什么时候上映？")
    assert registry_rewrite.applied and registry_rewrite.rewritten_query == "《冰与火之歌》什么时候上映？"
    low_confidence = EntityResolver({"某片子": ("某片", 0.75, "ENTITY_RESOLUTION")}).resolve("某片子什么时候上映？")
    assert not low_confidence.applied and low_confidence.reason == "ENTITY_LOW_CONFIDENCE"
    ambiguous = EntityResolver().resolve("某个标题", [("候选 A", 0.91), ("候选 B", 0.90)])
    assert not ambiguous.applied and ambiguous.reason == "ENTITY_AMBIGUOUS"
    conn.close()

print("PASS agent governance tests: gates, flags, routing, query rewrite, audit persistence")
