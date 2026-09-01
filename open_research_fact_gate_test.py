#!/usr/bin/env python3
"""Deterministic GATE-OR-201..206 regression for fact-quality Open Research."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from tests.fake_services import FakeTavilyGateway


with tempfile.TemporaryDirectory(prefix="agi-research-facts-") as _tmp:
    os.environ["AGI_INSPECTION_DB"] = str(Path(_tmp) / "test.db")
    import server
    from open_research.orchestrator import OpenResearchService

    server.init_db(reset=True)
    conn = server.connect()
    tenant, user = "tenant_jihu", "u_admin"
    now = lambda: datetime(2026, 8, 18, 12, tzinfo=timezone.utc)

    mainland = {
        "title": "电影《长安的荔枝》已于2025年7月18日在中国大陆上映",
        "url": "https://www.news.cn/ent/changan-release",
        "publisher": "新华网",
        "published_at": "2026-08-18T10:00:00+00:00",
        "content": "电影《长安的荔枝》已于2025年7月18日在中国大陆上映。",
    }
    result = OpenResearchService(conn, FakeTavilyGateway([mainland]), now=now).run(
        tenant_id=tenant, user_id=user, conversation_id="conv", question="《长安的离职》什么时候上映？",
    )
    # GATE-OR-201/202: typo → event date, no day time range, direct claim.
    assert result["rewrite"]["rewritten_query"] == "《长安的荔枝》什么时候上映？"
    assert result["fact_intent"] == "EVENT_DATE" and result["plan"][0]["freshness"] == "general"
    assert result["status"] == "VERIFIED"
    assert len(result["answer"]["claims"]) == 1 and result["answer"]["claims"][0]["value"] == "2025-07-18"
    assert result["answer"]["claims"][0]["territory"] == "CN-MAINLAND"
    assert "2025 年 7 月 18 日" in result["answer"]["text"]

    # GATE-OR-203: an unlisted direct source is not discarded.  Its neutral
    # reputation yields a citation-bound partial result instead of a false
    # no-search/no-answer state.
    aggregate = {**mainland, "url": "https://baike.example.org/changan", "publisher": "聚合站"}
    untrusted = OpenResearchService(conn, FakeTavilyGateway([aggregate]), now=now).run(
        tenant_id=tenant, user_id="u_region", conversation_id="conv", question="《长安的离职》什么时候上映？",
    )
    assert untrusted["status"] == "PARTIALLY_VERIFIED" and untrusted["answer"]["claims"]
    assert untrusted["citations"][0]["source_policy_id"] is None

    # GATE-OR-204: different territories are not substituted for one another.
    united_states = {
        "title": "电影《长安的荔枝》已于2025年7月25日在美国上映",
        "url": "https://www.xinhuanet.com/ent/changan-us-release",
        "publisher": "新华网",
        "published_at": "2026-08-18T10:00:00+00:00",
        "content": "电影《长安的荔枝》已于2025年7月25日在美国上映。",
    }
    regions = OpenResearchService(conn, FakeTavilyGateway([mainland, united_states]), now=now).run(
        tenant_id=tenant, user_id="u_store", conversation_id="conv", question="《长安的离职》什么时候上映？",
    )
    assert regions["status"] == "VERIFIED"
    assert all(item["territory"] == "CN-MAINLAND" for item in regions["answer"]["claims"])

    # GATE-OR-209: Tavily can return an old "定档" report, page-chrome dates
    # and the post-release article together.  A date with no market is useful
    # partial evidence but must not conflict with the explicit China-mainland
    # release fact.  The year-less correct result inherits only the reviewed
    # source URL year; it must never inherit retrieval time (2026).
    old_schedule = {
        "title": "[中国电影报道]新闻速览电影《长安的荔枝》定档7月25日 - CCTV",
        "url": "https://tv.cctv.com/2025/03/03/VIDEUS62NMhXjr7F4CXhhj07250303.shtml",
        "publisher": "CCTV",
        "content": "电影《长安的荔枝》定档7月25日，首映礼信息随后公布。",
    }
    page_chrome = {
        "title": "电影《长安的荔枝》正式上映 首映礼主创分享 - 中新网",
        "url": "https://www.chinanews.com.cn/cul/2025/07-18/10450193.shtml",
        "publisher": "中新网",
        "content": "推荐阅读 2026年8月6日更新，影片《长安的荔枝》已于7月18日在中国内地上映。",
    }
    mixed = OpenResearchService(conn, FakeTavilyGateway([old_schedule, page_chrome]), now=now).run(
        tenant_id=tenant, user_id="u_tavily_mixed", conversation_id="conv", question="《长安的离职》什么时候上映？",
    )
    assert mixed["status"] == "VERIFIED"
    assert len(mixed["answer"]["claims"]) == 1
    assert mixed["answer"]["claims"][0]["value"] == "2025-07-18"
    assert mixed["answer"]["claims"][0]["territory"] == "CN-MAINLAND"

    # GATE-OR-205: conflicting dates for the same territory remain a conflict.
    conflict = {**united_states, "title": "电影《长安的荔枝》已于2025年7月19日在中国大陆上映", "content": "电影《长安的荔枝》已于2025年7月19日在中国大陆上映。"}
    conflicting = OpenResearchService(conn, FakeTavilyGateway([mainland, conflict]), now=now).run(
        tenant_id=tenant, user_id="u_frontline", conversation_id="conv", question="《长安的离职》什么时候上映？",
    )
    assert conflicting["status"] == "CONFLICTING" and conflicting["answer"]["claim_status"] == "CONFLICTING"

    # GATE-OR-206: the durable boundary contains only facts, IDs and hashes.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(open_research_evidence)").fetchall()}
    assert "snippet" not in columns and "snippet_hash" in columns
    memory = conn.execute("SELECT memory_json FROM open_research_memory_index WHERE tenant_id=? AND user_id=?", (tenant, user)).fetchone()
    stored_memory = json.loads(memory["memory_json"])
    assert "snippet" not in json.dumps(stored_memory, ensure_ascii=False)
    run = conn.execute("SELECT provider_requests_json FROM open_research_runs WHERE run_id=?", (result["run_id"],)).fetchone()
    assert "tenant_jihu" not in run["provider_requests_json"]

    # Stable, verified historical date is user-private and reuses citations
    # without a second Tavily call (the 60-day TTL is separately tested in
    # open_research_test.py).
    memory_gateway = FakeTavilyGateway([aggregate])
    hit = OpenResearchService(conn, memory_gateway, now=now).run(
        tenant_id=tenant, user_id=user, conversation_id="conv", question="《长安的离职》什么时候上映？",
    )
    assert hit["memory_hit"] and not memory_gateway.calls and hit["answer"]["claims"][0]["value"] == "2025-07-18"
    assert OpenResearchService(conn, memory_gateway, now=now).get_run(run_id=result["run_id"], tenant_id=tenant, user_id="u_region") is None
    conn.close()

print("PASS GATE-OR-201..206,209: fact intent, source policy, claims, territory, conflict, privacy and memory")
