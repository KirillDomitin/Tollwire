"""Отправитель очереди: забирает исходящие сообщения у панели, шлёт их в Telegram, сам соблюдая лимиты, и сообщает итог.

Лимит Telegram (около 30 сообщений в секунду на токен и не чаще раза в секунду в один чат) принадлежит токену, а
токен только у бота, поэтому темп держит бот: не больше `rate_per_second` в любом окне в секунду (запас остаётся на
ответы людям в диалогах), не чаще секунды в один чат, 429 останавливает отправителя целиком на `retry_after`. Панель
выдаёт пачки в аренду: за 5 секунд до конца аренды бот не отправляет и возвращает остаток (`retry`).

Журнал: итог, который Telegram уже принял, а панель ещё нет, лежит в SQLite (без текста, адреса и chat id). Новость,
вернувшаяся в новой аренде после потери подтверждения, не отправляется второй раз, а подтверждается из журнала. Ссылка
на конфиг отправляется всегда: каждая выдача несёт новую ссылку, прежняя к тому времени уже не работает."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

from tollwire_bot.panel import Claim, OutboxItem, OutboxResult, PanelClient, PanelError, Rejected
from tollwire_bot.storage import JournalRow, SqliteStorage
from tollwire_bot.texts import format_date, t

logger = logging.getLogger(__name__)

CLAIM_PAUSE = 5.0  # пустая очередь (или неполная пачка): следующий опрос панели через столько секунд
LEASE_MARGIN = 5.0  # за столько секунд до конца аренды бот уже не отправляет
CHAT_INTERVAL = 1.0  # в один чат не чаще раза в секунду
ACK_EVERY = 25  # столько итогов - сразу в панель
ACK_AFTER = 2.0  # или через столько секунд после первого накопленного итога
UNKNOWN_KIND_PAUSE = 300  # неизвестный вид сообщения: вернуть панели, спросит снова не скоро
UNAUTHORIZED_PAUSE = 300  # токен не принят: пачку вернуть, повторять не раньше чем через столько
MAX_RETRY_AFTER = 86400  # больше панель не принимает (422 на весь чанк)
ACK_BACKOFF = 5.0  # панель не приняла итоги: следующая попытка не раньше
_TOLERANCE = 1e-7  # погрешность виртуальных и настенных секунд
_FINAL = ("sent", "blocked", "failed")  # итоги, которые лежат в журнале; retry в журнал не пишется


class _FloodTooLong(Exception):
    """429 с паузой, которая не умещается в аренду: пачку нужно вернуть панели."""

    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


class OutboxSender:
    def __init__(
        self,
        bot: Bot,
        panel: PanelClient,
        storage: SqliteStorage,
        *,
        rate_per_second: int = 20,
        batch: int = 50,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bot = bot
        self._panel = panel
        self._storage = storage
        self._rate = rate_per_second
        self._batch = batch
        self._sleep = sleep
        self._clock = clock
        self._window: deque[float] = deque()  # моменты последних отправок: не больше rate_per_second в секунду
        self._last_to_chat: dict[int, float] = {}
        self._buffer: list[tuple[str, OutboxResult]] = []  # (lease_id, итог), ещё не отправленные панели
        self._buffer_since = 0.0
        self._flush_not_before = 0.0

    # --- жизнь -------------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Бесконечный цикл. Отмена задачи (SIGTERM) досылает накопленные подтверждения."""
        try:
            await self._load_journal()
            while True:
                try:
                    more = await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - диск, SQLite, сбой в разборе: цикл не должен умирать молча
                    logger.error("the outbox cycle failed: %s", type(error).__name__)
                    more = False
                if not more:
                    await self._idle(CLAIM_PAUSE)
        except asyncio.CancelledError:
            await self._flush(force=True)
            raise

    async def _load_journal(self) -> None:
        """Итоги, которые бот не успел передать до остановки: уйдут первым же подтверждением под прежней арендой."""
        for row in await self._storage.journal_all():
            self._buffer.append((row.lease_id, OutboxResult(row.id, row.status, row.error)))
        if self._buffer:
            self._buffer_since = self._clock() - ACK_AFTER  # передать сразу

    async def _cycle(self) -> bool:
        """Один опрос панели и отправка пачки. True - пачка полная, следующий опрос без паузы."""
        await self._flush()
        try:
            claim = await self._panel.claim(self._batch)
        except PanelError as error:
            logger.warning("the panel did not give the queue: %s", type(error).__name__)
            return False
        if not claim.items:
            return False
        await self._handle(claim)
        # Панель отсеивает часть строк при выдаче (пропущенные, заблокировавшие), поэтому в пачке может быть меньше
        # batch, хотя очередь не пуста: пока что-то приходит, следующий опрос без паузы.
        return True

    # --- пачка -------------------------------------------------------------------------------------------------------

    async def _handle(self, claim: Claim) -> None:
        journal = await self._storage.journal_by_ids([item.id for item in claim.items])
        queue: list[OutboxItem] = []
        for item in claim.items:
            seen = journal.get(item.id)
            if seen is not None and item.kind == "news" and seen.status in _FINAL:
                # Telegram это уже принял, подтверждение потерялось: подтверждаем из журнала, заново не отправляем.
                self._add_result(claim.lease_id, OutboxResult(item.id, seen.status, seen.error))
            else:
                queue.append(item)
        lease_until = claim.lease_until.timestamp()
        try:
            await self._send_queue(claim, queue, lease_until)
        except asyncio.CancelledError:
            # Остановка бота: что не отправлено, возвращается панели сразу, а не ждёт конца аренды.
            self._return_all(claim, queue, "flood", 0)
            raise

    async def _send_queue(self, claim: Claim, queue: list[OutboxItem], lease_until: float) -> None:
        while queue:
            now = self._clock()
            if lease_until - now < LEASE_MARGIN - 1e-3:
                self._return_all(claim, queue, "flood", 0)
                return
            item, wait = self._pick(queue, now)
            if wait > 0:
                if now + wait > lease_until - LEASE_MARGIN + 1e-3:
                    self._return_all(claim, queue, "flood", 0)  # до конца аренды не дожить: остаток панели
                    return
                await self._pause(wait)
                continue
            queue.remove(item)
            try:
                result = await self._send(claim, item, lease_until)
            except _FloodTooLong as flood:
                seconds = min(flood.seconds, MAX_RETRY_AFTER)
                self._add_result(claim.lease_id, OutboxResult(item.id, "retry", "flood", seconds))
                self._return_all(claim, queue, "flood", seconds)
                return
            await self._record(claim, item, result)
            if self._flush_due(self._clock()):
                await self._flush()

    def _return_all(self, claim: Claim, queue: list[OutboxItem], error: str, retry_after: int) -> None:
        """Остаток пачки возвращается панели: она отдаст его снова не раньше чем через несколько секунд."""
        for item in queue:
            self._add_result(claim.lease_id, OutboxResult(item.id, "retry", error, retry_after or None))
        queue.clear()

    # --- темп --------------------------------------------------------------------------------------------------------

    def _pick(self, queue: list[OutboxItem], now: float) -> tuple[OutboxItem, float]:
        """Какое сообщение уйдёт следующим и сколько до него ждать: из готовых по правилам берётся первое по порядку, а
        сообщение в чат, куда только что писали, пропускает других вперёд."""
        while self._window and self._window[0] + 1.0 <= now + _TOLERANCE:
            self._window.popleft()
        window_wait = 0.0
        if len(self._window) >= self._rate:
            window_wait = self._window[0] + 1.0 - now
        best: tuple[OutboxItem, float] | None = None
        for item in queue:
            last = self._last_to_chat.get(item.telegram_id)
            chat_wait = 0.0 if last is None else max(0.0, last + CHAT_INTERVAL - now)
            wait = max(window_wait, chat_wait)
            if wait <= _TOLERANCE:
                return item, 0.0
            if best is None or wait < best[1]:
                best = (item, wait)
        assert best is not None
        return best

    async def _pause(self, seconds: float) -> None:
        """Пауза отправителя; подтверждения при этом уходят в панель вовремя."""
        end = self._clock() + seconds
        while True:
            now = self._clock()
            remaining = end - now
            if remaining <= _TOLERANCE:
                return
            deadline = self._flush_deadline()
            step = remaining if deadline is None or deadline >= end else max(deadline - now, 0.0)
            if step > 0:
                await self._sleep(step)
            if self._flush_due(self._clock()):
                await self._flush()
            elif step <= 0:
                await self._sleep(min(remaining, ACK_BACKOFF))  # панель не принимает итоги: не крутимся впустую

    async def _idle(self, seconds: float) -> None:
        await self._pause(seconds)

    # --- отправка ----------------------------------------------------------------------------------------------------

    async def _send(self, claim: Claim, item: OutboxItem, lease_until: float) -> OutboxResult:
        """Одно сообщение в Telegram. 429 - пауза всего отправителя и тот же вызов ещё раз."""
        if item.kind == "config":
            if not item.config_url:
                return OutboxResult(item.id, "failed", "bad_request")  # ссылки нет: отправлять нечего
            text = t("broadcast_config", url=item.config_url, until=format_date(item.valid_until))
            parse_mode = None
        elif item.kind == "news":
            text = claim.texts.get(item.broadcast_id or "", "")
            parse_mode = "HTML"
        else:
            # Вид сообщения, которого этот бот не знает (панель новее): не отправлять и не закрывать, а вернуть.
            return OutboxResult(item.id, "retry", "flood", UNKNOWN_KIND_PAUSE)
        while True:
            try:
                await self._bot.send_message(item.telegram_id, text, parse_mode=parse_mode)
            except TelegramRetryAfter as flood:
                seconds = int(flood.retry_after)
                if self._clock() + seconds > lease_until - LEASE_MARGIN + 1e-3:
                    raise _FloodTooLong(seconds) from None
                self._window.clear()  # после паузы окно в секунду пустое: в паузу не уходило ничего
                await self._pause(seconds)
                continue
            except TelegramForbiddenError:
                return OutboxResult(item.id, "blocked", "blocked")
            except TelegramBadRequest as error:
                code = "chat_not_found" if "chat not found" in str(error).lower() else "bad_request"
                return OutboxResult(item.id, "failed", code)
            except TelegramUnauthorizedError:
                # Токен отозван или заменён: это не вина сообщений. Пачка возвращается панели, рассылка не сгорает.
                raise _FloodTooLong(UNAUTHORIZED_PAUSE) from None
            except TelegramNetworkError:
                return OutboxResult(item.id, "retry", "network")
            except TelegramServerError:
                return OutboxResult(item.id, "retry", "telegram_5xx")
            except TelegramAPIError:
                return OutboxResult(item.id, "retry", "network")  # неизвестное: повторим, а не закроем навсегда
            except Exception as error:  # noqa: BLE001 - сообщение исключения может нести текст: пишем только тип
                logger.warning("sending failed: %s", type(error).__name__)
                return OutboxResult(item.id, "retry", "network")
            sent_at = self._clock()
            self._window.append(sent_at)
            self._last_to_chat[item.telegram_id] = sent_at
            return OutboxResult(item.id, "sent")

    # --- итоги -------------------------------------------------------------------------------------------------------

    async def _record(self, claim: Claim, item: OutboxItem, result: OutboxResult) -> None:
        """Итог одного сообщения: в журнал (если он окончательный) и в очередь подтверждений."""
        self._add_result(claim.lease_id, result)  # раньше журнала: отмена посреди записи не должна терять итог
        if result.status in _FINAL:
            await self._storage.journal_put(
                JournalRow(result.id, claim.lease_id, item.kind, result.status, result.error), self._clock()
            )

    def _add_result(self, lease_id: str, result: OutboxResult) -> None:
        if not self._buffer:
            self._buffer_since = self._clock()
        self._buffer.append((lease_id, result))

    def _flush_deadline(self) -> float | None:
        if not self._buffer:
            return None
        return max(self._buffer_since + ACK_AFTER, self._flush_not_before)

    def _flush_due(self, now: float) -> bool:
        if not self._buffer or now < self._flush_not_before:
            return False
        return len(self._buffer) >= ACK_EVERY or now >= self._buffer_since + ACK_AFTER - _TOLERANCE

    async def _flush(self, *, force: bool = False) -> None:
        """Итоги - панели, по аренде и не больше 100 за раз. Принятые (applied) уходят из журнала; не принятые из-за
        чужой аренды (ignored) остаются в журнале: вдруг эта новость выдастся снова. Панель недоступна - позже."""
        if not self._buffer or (not force and not self._flush_due(self._clock())):
            return
        pending = self._buffer
        self._buffer = []
        failed = False
        for lease_id in dict.fromkeys(lease for lease, _ in pending):
            results = [result for lease, result in pending if lease == lease_id]
            for start in range(0, len(results), 100):
                chunk = results[start : start + 100]
                if failed:
                    self._buffer.extend((lease_id, result) for result in chunk)
                    continue
                try:
                    outcome = await self._panel.ack(lease_id, chunk)
                except Rejected as error:
                    # Панель отвергла сами итоги (422): повтор тот же ответ - чанк отбрасывается, иначе цепочка встанет.
                    logger.error("the panel refused the results: %s", type(error).__name__)
                    continue
                except PanelError as error:
                    logger.warning("the panel did not take the results: %s", type(error).__name__)
                    failed = True
                    self._buffer.extend((lease_id, result) for result in chunk)
                    continue
                await self._storage.journal_remove(outcome.applied)
        if failed:
            self._flush_not_before = self._clock() + ACK_BACKOFF
            if self._buffer:
                self._buffer_since = min(self._buffer_since, self._clock() - ACK_AFTER)
        else:
            self._flush_not_before = 0.0
