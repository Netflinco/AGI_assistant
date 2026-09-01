#!/usr/bin/env python3
"""Development/external-process entry point for the durable Office queue.

Production deployment runs this process in a separate resource-limited
container.  The current SQLite/local-storage adapters remain development-only;
``server.run_server`` fails F0 readiness before they could be misrepresented as
a production Office runtime.
"""

from __future__ import annotations

import argparse
import signal
import threading

import server
from office_agent.readiness import production_readiness_errors
from office_agent.worker import OfficeJobWorker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    server.init_db()
    errors = production_readiness_errors()
    if errors:
        raise RuntimeError("Office F0 readiness failed: " + ", ".join(errors))
    worker = OfficeJobWorker(server.connect, server.office_job_service_for_worker, poll_seconds=args.poll_seconds)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    worker.start()
    try:
        stop.wait()
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
