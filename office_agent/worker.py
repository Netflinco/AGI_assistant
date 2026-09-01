"""Small queue adapter for Office Jobs.

It is intentionally independent from the chat request path.  Production must
run this adapter in a separately resource-limited worker process/container;
the local thread implementation exists only to exercise the same durable job
state machine during development.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .jobs import OfficeJobService


class OfficeJobWorker:
    def __init__(self, connect: Callable[[], Any], service_for: Callable[[Any, str, str], OfficeJobService], *, poll_seconds: float = 1.0,
                 stale_running_seconds: float = 15 * 60):
        self.connect = connect
        self.service_for = service_for
        self.poll_seconds = max(0.1, poll_seconds)
        self.stale_running_seconds = max(1.0, stale_running_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="office-job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_seconds + 2)

    def run_once(self) -> bool:
        with self.connect() as conn:
            self._recover_stale_running(conn)
            row = conn.execute(
                """SELECT job_id, tenant_id, user_id FROM office_jobs
                   WHERE status='QUEUED' ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if not row:
                return False
            job_id, tenant_id, user_id = row["job_id"], row["tenant_id"], row["user_id"]
            claimed = conn.execute(
                """UPDATE office_jobs SET status='RUNNING', stage='EXTRACTING', updated_at=?
                   WHERE job_id=? AND status='QUEUED'""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id),
            ).rowcount
            conn.commit()
            if not claimed:
                return False
            service = self.service_for(conn, tenant_id, user_id)
            service.run_job(job_id=job_id, tenant_id=tenant_id, user_id=user_id)
            conn.commit()
            return True

    def _recover_stale_running(self, conn: Any) -> int:
        """Make orphaned work visible and explicitly retryable after a crash.

        A worker can die after it atomically claims a job but before it writes a
        terminal state.  Re-running blindly risks duplicate artifact creation,
        so a fresh worker marks such jobs ``RETRYABLE_FAILED``.  The ordinary
        ``retry_job`` gate then re-validates the asset/Brief and starts a new
        private artifact version only when the user (or a future bounded retry
        policy) explicitly requests it.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_running_seconds)
        recovered = 0
        rows = conn.execute("SELECT job_id, updated_at FROM office_jobs WHERE status='RUNNING'").fetchall()
        for row in rows:
            try:
                updated = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                updated = datetime.min.replace(tzinfo=timezone.utc)
            if updated < cutoff:
                changed = conn.execute(
                    """UPDATE office_jobs SET status='RETRYABLE_FAILED', stage='FAILED', error_code='OFFICE_WORKER_FAILED',
                           updated_at=?, completed_at=? WHERE job_id=? AND status='RUNNING'""",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), datetime.now(timezone.utc).isoformat(timespec="seconds"), row["job_id"]),
                ).rowcount
                recovered += changed
        if recovered:
            conn.commit()
        return recovered

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception:
                # The durable job stays observable; job-specific failures are
                # recorded by OfficeJobService, while a worker restart may pick
                # up later queued work.
                ran = False
            self._stop.wait(0.05 if ran else self.poll_seconds)
