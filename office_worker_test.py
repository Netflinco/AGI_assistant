#!/usr/bin/env python3
"""Durable Office queue adapter regression (GATE-504/505/507/508)."""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
from datetime import datetime, timedelta, timezone

from tests.fake_services import FakeScanner, fake_preview, fake_renderer


with tempfile.TemporaryDirectory(prefix="agi-office-worker-") as _tmp:
    os.environ["AGI_INSPECTION_DB"] = str(Path(_tmp) / "test.db")
    import server
    from office_agent.assets import OfficeAssetService
    from office_agent.jobs import OfficeJobService
    from office_agent.worker import OfficeJobWorker

    server.init_db(reset=True)
    conn = server.connect()
    from openpyxl import Workbook
    workbook = Workbook(); workbook.active.append(["指标", "数值"]); workbook.active.append(["收入", 9])
    source = io.BytesIO(); workbook.save(source)
    assets = OfficeAssetService(conn, Path(_tmp) / "assets", scanner=FakeScanner())
    asset = assets.create(tenant_id="tenant_jihu", user_id="u_admin", filename="worker.xlsx", content=source.getvalue())
    creator = OfficeJobService(conn, assets, Path(_tmp) / "artifacts", renderer=fake_renderer)
    creator._create_preview = fake_preview
    job = creator.create_ppt_job(tenant_id="tenant_jihu", user_id="u_admin", conversation_id="conv", asset_ids=[asset["asset_id"]], title="队列汇报")["job"]
    conn.commit()
    assert creator.create_ppt_job(tenant_id="tenant_jihu", user_id="u_admin", conversation_id="conv", asset_ids=[asset["asset_id"]], title="另一份重任务")["reason_code"] == "OFFICE_USER_CONCURRENCY_LIMIT"
    conn.commit()

    def service_for(worker_conn, _tenant_id, _user_id):
        worker_assets = OfficeAssetService(worker_conn, Path(_tmp) / "assets", scanner=FakeScanner())
        service = OfficeJobService(worker_conn, worker_assets, Path(_tmp) / "artifacts", renderer=fake_renderer)
        service._create_preview = fake_preview
        return service

    worker = OfficeJobWorker(server.connect, service_for)
    assert worker.run_once() is True
    assert creator.get_job(job_id=job["job_id"], tenant_id="tenant_jihu", user_id="u_admin")["status"] == "SUCCEEDED"
    assert worker.run_once() is False
    stale = creator.create_ppt_job(tenant_id="tenant_jihu", user_id="u_admin", conversation_id="stale", asset_ids=[asset["asset_id"]], title="崩溃恢复样例")["job"]
    conn.execute(
        "UPDATE office_jobs SET status='RUNNING', stage='GENERATING', updated_at=? WHERE job_id=?",
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"), stale["job_id"]),
    )
    conn.commit()
    recovery_worker = OfficeJobWorker(server.connect, service_for, stale_running_seconds=1)
    assert recovery_worker.run_once() is False
    recovered = creator.get_job(job_id=stale["job_id"], tenant_id="tenant_jihu", user_id="u_admin")
    assert recovered["status"] == "RETRYABLE_FAILED" and recovered["error_code"] == "OFFICE_WORKER_FAILED"
    assert creator.retry_job(job_id=stale["job_id"], tenant_id="tenant_jihu", user_id="u_admin")["status"] == "QUEUED"
    conn.commit()
    assert recovery_worker.run_once() is True
    assert creator.get_job(job_id=stale["job_id"], tenant_id="tenant_jihu", user_id="u_admin")["status"] == "SUCCEEDED"
    conn.close()

print("PASS office worker tests: durable queue claim, user concurrency and delivery")
