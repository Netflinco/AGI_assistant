#!/usr/bin/env python3
"""Regression for LLM-grounded synthesis of complementary public evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from tests.fake_services import FakeTavilyGateway


with tempfile.TemporaryDirectory(prefix="agi-llm-evidence-") as _tmp:
    os.environ["AGI_INSPECTION_DB"] = str(Path(_tmp) / "test.db")
    import server
    from open_research.evidence_reasoner import EvidenceReasoner
    from open_research.orchestrator import OpenResearchService

    server.init_db(reset=True)
    conn = server.connect()
    now_value = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    now = lambda: now_value
    chinanews_url = "https://www.chinanews.com.cn/cul/2025/07-18/10450193.shtml"
    thepaper_url = "https://m.thepaper.cn/newsDetail_forward_31210447"
    chinanews_id = f"ev_{hashlib.sha256(chinanews_url.encode('utf-8')).hexdigest()[:16]}"
    thepaper_id = f"ev_{hashlib.sha256(thepaper_url.encode('utf-8')).hexdigest()[:16]}"
    citations = [
        {
            "title": "电影《长安的荔枝》正式上映 首映礼主创分享“荔枝”之路-中新网",
            "url": chinanews_url,
            "publisher": "中新网",
            # The URL supplies the year but this card does not name the market.
            "content": "电影《长安的荔枝》7月18日正式上映，主创在首映礼分享创作经历。",
        },
        {
            "title": "电影《长安的荔枝》导演大鹏：我更想展现的是“无人知”",
            "url": thepaper_url,
            "publisher": "澎湃新闻",
            # This is the live incident: Tavily did not provide published_at,
            # but the card itself directly supports the China-mainland market.
            "content": "该片此前曾宣布提档，已于7月18日在中国内地上映。",
        },
    ]
    model_calls = []

    def fake_model_urlopen(req, timeout):
        assert timeout == 25
        body = json.loads(req.data.decode("utf-8"))
        model_calls.append(body)
        content = json.dumps({
            "status": "VERIFIED",
            "summary": "中新网给出 7 月 18 日正式上映，澎湃新闻明确该片同日已在中国内地上映。",
            "claims": [{
                "value": "2025-07-18",
                "territory": "CN-MAINLAND",
                "date_role": "ACTUAL_RELEASE",
                "confidence": 0.96,
                "evidence_ids": [chinanews_id, thepaper_id],
            }],
        }, ensure_ascii=False)
        return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")

    reasoner = EvidenceReasoner(
        {
            "api_key": "test-model-key",
            "model": "test-model",
            "chat_completions_url": "https://model.example.test/v1/chat/completions",
        },
        urlopen=fake_model_urlopen,
    )
    result = OpenResearchService(
        conn,
        FakeTavilyGateway(citations),
        now=now,
        reasoner=reasoner,
    ).run(
        tenant_id="tenant_jihu",
        user_id="u_admin",
        conversation_id="conv_llm_evidence",
        question="《长安的离职》什么时候上映？",
    )
    assert result["status"] == "VERIFIED"
    claim = result["answer"]["claims"][0]
    assert claim["value"] == "2025-07-18"
    assert claim["territory"] == "CN-MAINLAND"
    assert claim["evidence_ids"] == [chinanews_id, thepaper_id]
    assert len(result["citations"]) == 2
    assert result["answer"]["evidence_synthesis"]["engine"] == "llm_evidence_synthesis"
    assert model_calls and model_calls[0]["temperature"] == 0
    sent = json.dumps(model_calls[0], ensure_ascii=False)
    assert "tenant_jihu" not in sent and "conv_llm_evidence" not in sent
    assert chinanews_id in sent and thepaper_id in sent
    admin = {"tenant_id": "tenant_jihu", "user_id": "u_admin", "role": "TENANT_ADMIN"}
    conversation = server.create_conversation(conn, admin, title="模型证据综合测试")
    server.add_open_research_message(conn, admin, conversation["conversation_id"], "《长安的离职》什么时候上映？", result)
    detail = server.get_open_research_history_record(conn, admin, result["run_id"])
    assert detail["answer"]["evidence_synthesis"]["evidence_count"] == 2
    conn.close()

print("PASS GATE-OR-234: LLM evidence synthesis joins complementary reviewed sources without a rule-only date merger")
