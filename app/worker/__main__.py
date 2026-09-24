"""Worker process: `python -m app.worker`"""
import logging
import signal
import threading

from app.db import session_scope
from app.pipeline import queue
from app.worker.runner import Runner
from app.worker.scheduler import CampaignScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")
from app.redact import install_log_filter  # noqa: E402

install_log_filter()


def main() -> None:
    with session_scope() as db:
        # single worker process: anything still 'running' was interrupted by a restart
        n = queue.requeue_stale(db, all_running=True)
        if n:
            log.info("requeued %s interrupted jobs", n)
    runner = Runner()
    runner.start()
    sched = CampaignScheduler()
    sched.start()
    log.info("worker started (%s job threads)", runner.concurrency)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    runner.stop.set()
    sched.sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
