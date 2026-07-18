"""Run the Job Discovery Worker.

Usage:
    python scripts/run_job_discovery_worker.py          # poll forever
    python scripts/run_job_discovery_worker.py --once    # process one task and exit
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.db.session import SessionLocal
from backend.app.services.job_discovery.worker import JobDiscoveryWorker


def main() -> None:
    settings = get_settings()

    if not settings.job_discovery_enabled:
        print("Job discovery is disabled (job_discovery_enabled=False). Exiting.")
        return

    worker = JobDiscoveryWorker(SessionLocal, settings)

    if "--once" in sys.argv:
        result = worker.run_once()
        print(f"Processed {result} task(s).")
        return

    print(f"Job Discovery Worker started (worker_id={worker.worker_id})")
    try:
        worker.run_loop(poll_interval=10.0)
    except KeyboardInterrupt:
        print("Worker stopped.")


if __name__ == "__main__":
    main()
