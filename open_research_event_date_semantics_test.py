#!/usr/bin/env python3
"""Regression for the 2026-08-24 release-date false-conflict incident.

The production Tavily result cited an article saying that the film announced
its move on 7/13, moved its release to 7/18 and was originally scheduled for
7/25.  Only 7/18 is a release-related target value; 7/13 is an announcement
date and 7/25 is a superseded schedule.  The fixture intentionally keeps all
three values in one provider fragment so this class of mistake cannot return.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile

from tests.fake_services import FakeDetailFetcher, FakeTavilyGateway


with tempfile.TemporaryDirectory(prefix="agi-event-date-semantics-") as _tmp:
    os.environ["AGI_INSPECTION_DB"] = str(Path(_tmp) / "test.db")
    import server
    from open_research.claims import SCHEDULED_RELEASE, extract_event_date_claims
    from open_research.evidence import Evidence
    from open_research.orchestrator import OpenResearchService
    from open_research.retention import NO_MEMORY
    from open_research.source_policy import load_active_source_policies

    server.init_db(reset=True)
    conn = server.connect()
    now_value = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    now = lambda: now_value
    policies = load_active_source_policies(conn, now=now_value)
    ctdsb_url = "https://www.ctdsb.net/c1742_202507/2483072.html"
    reschedule_text = (
        "据悉，7月13日，电影《长安的荔枝》官宣提档至7月18日全国上映。"
        "影片原定于7月25日全国上映。"
    )
    reschedule = {
        "title": "陈佩斯黄渤首次合作电影《戏台》官宣改档7月25日，避开与《长安的荔枝》撞档",
        "url": ctdsb_url,
        "publisher": "极目新闻",
        "published_at": "2025-07-14T11:17:51+00:00",
        "content": reschedule_text,
    }

    # GATE-OR-230: a date at the front of an announcement sentence is not
    # allowed to borrow the later "上映" predicate.  The replacement schedule
    # is retained as a non-final schedule for trusted corroboration only.
    evidence = Evidence(
        "ev_reschedule", reschedule["title"], ctdsb_url, "极目新闻",
        reschedule["published_at"], now_value.isoformat(), "PUBLISHER", reschedule_text,
        source_policy_id="rsp_seed_ctdsb_net",
    )
    extracted = extract_event_date_claims(
        [evidence], query="《长安的荔枝》什么时候上映？", policies=policies, now=now_value,
    )
    assert [(item.value, item.date_role) for item in extracted] == [("2025-07-18", SCHEDULED_RELEASE)]

    # A historical schedule cannot be upgraded to an actual release based on
    # retrieval time, cannot enter permanent memory, and cannot manufacture a
    # conflict.  Its publisher page is still eligible for bounded detail read
    # and one exact corroboration query.
    details = FakeDetailFetcher({ctdsb_url: reschedule_text})
    historical_only = OpenResearchService(
        conn, FakeTavilyGateway([reschedule]), now=now, detail_fetcher=details,
    ).run(
        tenant_id="tenant_jihu", user_id="u_admin", conversation_id="conv_date_role",
        question="《长安的离职》什么时候上映？",
    )
    assert historical_only["status"] == "NO_AUTHORITATIVE_SOURCE"
    assert not historical_only["answer"]["claims"]
    assert historical_only["retention_class"] == NO_MEMORY
    assert details.calls and details.calls[0]["url"] == ctdsb_url
    assert any(item["purpose"] == "verify_historical_scheduled_date" for item in historical_only["plan"])

    # GATE-OR-231: an actual release statement wins over the historical
    # schedule; the result is one direct China-mainland answer, not a conflict.
    actual = {
        "title": "本周五（7月18日），董成鹏执导并主演的《长安的荔枝》正式上映",
        "url": "https://www.chinafilmnews.cn/2025/07/changan-actual-release",
        "publisher": "中国电影报",
        "published_at": "2025-07-19T09:00:00+00:00",
        "content": "本周五（7月18日），董成鹏执导并主演的电影《长安的荔枝》正式上映。",
    }
    resolved = OpenResearchService(conn, FakeTavilyGateway([reschedule, actual]), now=now).run(
        tenant_id="tenant_jihu", user_id="u_store", conversation_id="conv_actual",
        question="《长安的离职》什么时候上映？",
    )
    assert resolved["status"] == "VERIFIED"
    assert [(item["value"], item["date_role"]) for item in resolved["answer"]["claims"]] == [
        ("2025-07-18", "ACTUAL_RELEASE"),
    ]
    assert resolved["answer"]["claims"][0]["territory"] == "CN-MAINLAND"
    assert resolved["answer"]["claims"][0]["evidence_type"] == "CORROBORATED_EVENT_EVIDENCE"
    assert "2025 年 7 月 18 日" in resolved["answer"]["text"]

    # An actual date without an explicit territory stays useful to the current
    # conversation, but must not be worded as a different-region answer or be
    # retained as cross-conversation reusable knowledge.
    unregional = OpenResearchService(conn, FakeTavilyGateway([actual]), now=now).run(
        tenant_id="tenant_jihu", user_id="u_frontline", conversation_id="conv_actual_unregional",
        question="《长安的离职》什么时候上映？",
    )
    assert unregional["status"] == "PARTIALLY_VERIFIED"
    assert unregional["retention_class"] == NO_MEMORY
    assert "地区待确认" in unregional["answer"]["text"]
    assert "其他地区信息" not in unregional["answer"]["text"]

    # GATE-OR-232: superseded schedules and programme broadcast dates must not
    # create claims merely because the same work title and the word "上映" occur.
    non_release_evidence = [
        Evidence(
            "ev_original", "电影《长安的荔枝》原定于2025年7月25日全国上映",
            "https://www.news.cn/2025/07/changan-original", "新华网", "2025-07-01T00:00:00+00:00",
            now_value.isoformat(), "PUBLISHER", "电影《长安的荔枝》原定于2025年7月25日全国上映。",
            source_policy_id="rsp_seed_news_cn",
        ),
        Evidence(
            "ev_program", "《今日影评》 2025年7月23日 《长安的荔枝》",
            "https://tv.cctv.com/2025/07/23/changan-program.html", "央视网", "2025-07-23T00:00:00+00:00",
            now_value.isoformat(), "PUBLISHER", "《今日影评》于2025年7月23日播出，讨论电影《长安的荔枝》上映。",
            source_policy_id="rsp_seed_cctv_com",
        ),
    ]
    assert not extract_event_date_claims(
        non_release_evidence, query="《长安的荔枝》什么时候上映？", policies=policies, now=now_value,
    )

    # GATE-OR-233: candidates in a conflicting result remain available to the
    # audit payload, but must never be rendered as a user-facing conclusion.
    ui = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert 'const hasDeliverableClaims = ["VERIFIED", "PARTIALLY_VERIFIED"].includes(status);' in ui
    assert 'status === "CONFLICTING" ? "待进一步核验的来源（未形成结论）"' in ui
    conn.close()

print("PASS GATE-OR-230..233: release-date role, finality, detail fallback and conflict UI")
