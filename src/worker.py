#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio
import signal
import sys
from collections.abc import Callable

from core.config import settings
from infrastructure.workers.scheduler import Scheduler, build_rules
from infrastructure.workers.worker import JobWorker
from modules.jobs.queue import JobQueue


def handle_signals(stop: Callable[[], None]) -> None:
    """Регистрирует обработчики сигналов для мягкого завершения."""
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            signal.signal(sig, lambda signum, frame: stop())


async def main() -> None:
    worker = JobWorker(JobQueue())
    # Планировщик живёт в том же процессе второй asyncio-задачей: пока одна ждёт SSH или очередь,
    # другая проверяет сроки. Дубли при нескольких воркерах отсекает Redis (см. Scheduler).
    scheduler = (
        Scheduler(build_rules(settings), settings.scheduler_tick_seconds) if settings.scheduler_enabled else None
    )

    def stop() -> None:
        worker.stop()
        if scheduler:
            scheduler.stop()

    handle_signals(stop)
    await asyncio.gather(worker.run(), *([scheduler.run()] if scheduler else []))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Main] Приложение завершено через KeyboardInterrupt.")
        sys.exit(0)
