#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio
import logging
import signal
import sys
from collections.abc import Callable

from core.config import settings
from core.log import setup_logging
from infrastructure.workers.scheduler import Scheduler, build_rules
from infrastructure.workers.worker import JobWorker
from modules.jobs.queue import JobQueue

logger = logging.getLogger("worker")


def handle_signals(stop: Callable[[], None]) -> None:
    """Регистрирует обработчики сигналов для мягкого завершения."""
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            signal.signal(sig, lambda signum, frame: stop())


async def main() -> None:
    setup_logging(settings.log_level, settings.log_format)
    logger.info("worker started", extra={"scheduler": settings.scheduler_enabled})
    worker = JobWorker(JobQueue())
    # Планировщик живёт в том же процессе второй asyncio-задачей: пока одна ждёт SSH или очередь,
    # другая проверяет сроки. Дубли при нескольких воркерах отсекает Redis (см. Scheduler).
    scheduler = (
        Scheduler(build_rules(settings), settings.scheduler_tick_seconds) if settings.scheduler_enabled else None
    )

    def stop() -> None:
        logger.info("worker stopping: finishing current job")
        worker.stop()
        if scheduler:
            scheduler.stop()

    handle_signals(stop)
    await asyncio.gather(worker.run(), *([scheduler.run()] if scheduler else []))
    logger.info("worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("worker interrupted")
        sys.exit(0)
