#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio
import signal
import sys

from infrastructure.workers.worker import JobWorker
from modules.jobs.queue import JobQueue


def handle_signals(worker: JobWorker) -> None:
    """Регистрирует обработчики сигналов для мягкого завершения."""
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            signal.signal(sig, lambda signum, frame: worker.stop())


async def main() -> None:
    job_queue = JobQueue()
    worker = JobWorker(job_queue)
    handle_signals(worker)
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Main] Приложение завершено через KeyboardInterrupt.")
        sys.exit(0)
