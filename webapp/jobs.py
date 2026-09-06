"""Minimal in-memory, thread-based job manager for asynchronous simulation
execution.

Design choice, explicitly: this is a single-process, in-memory job store
using a background `threading.Thread` per job -- not Celery, not a task
queue, not multiple processes. The brief this was built for explicitly
asked for "a practical local solution" and warned against "an unnecessarily
complicated distributed architecture." A discrete-event simulation run in
this project is CPU-bound Python and takes low single-digit seconds even
at the web demo's own upper limits (see tests/test_jobs.py) -- there is no
real workload here that would justify a message broker or a process pool.
If SimN ever needs multi-worker horizontal scaling, this module is the
seam to replace; nothing outside it needs to change.

Jobs are NOT persisted across process restarts (in-memory only) and are
capped in count (oldest completed jobs are evicted) -- this is a demo/
research tool's job store, not a production job queue.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Job:
    id: str
    status: str = 'queued'  # queued | running | completed | failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    stage: str = 'queued'
    combos_done: int = 0
    combos_total: int = 0
    result: Optional[dict] = None
    error: Optional[str] = None
    traces: dict = field(default_factory=dict)
    args: object = None

    def as_status_dict(self) -> dict:
        elapsed = None
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            elapsed = round(end - self.started_at, 3)
        return {
            'id': self.id,
            'status': self.status,
            'stage': self.stage,
            'combos_done': self.combos_done,
            'combos_total': self.combos_total,
            'elapsed': elapsed,
            'error': self.error,
        }


class JobManager:
    """Thread-safe store of :class:`Job` objects, each backed by one
    background thread running the actual simulation work.
    """

    def __init__(self, max_jobs: int = 200):
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._max_jobs:
                oldest = self._order.pop(0)
                self._jobs.pop(oldest, None)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def run_in_background(self, job: Job, target: Callable[[Job], None]) -> None:
        """Start `target(job)` on a daemon thread. `target` is responsible
        for mutating `job.status`/`job.stage`/`job.result`/`job.error` as
        it progresses -- this class only owns storage and thread startup,
        not simulation logic (that stays in webapp/app.py and ndn_sim).
        """
        def _runner():
            job.status = 'running'
            job.started_at = time.time()
            try:
                target(job)
                job.status = 'completed'
            except Exception as exc:  # surfaced to the poller, not lost
                job.status = 'failed'
                job.error = f'{type(exc).__name__}: {exc}'
            finally:
                job.finished_at = time.time()

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()


# Module-level singleton -- one job store per process, matching the
# single-process deployment model this app already uses (see
# webapp/app.py / wsgi.py / Deployment section in the README).
job_manager = JobManager()
