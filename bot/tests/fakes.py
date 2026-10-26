"""Фейки для тестов бота (этап 25.2): время, Telegram и панель. Ничего из `tollwire_bot` здесь не импортируется:
фейки не зависят от имён кода, которого ещё нет. Всё, что от этих имён зависит, собрано в `support.py`.

- `FakeClock` - виртуальное время. `sleep()` не ждёт по-настоящему: фоновый `drive()` будит спящих, когда бот и тест
  затихли (нет вызовов Telegram и панели 20 мс настоящего времени). Так «3 минуты опроса» занимают доли секунды, а
  порядок событий - тот же, что был бы в жизни.
- `FakeTelegram` - перехват методов Bot API через `BaseRequestMiddleware` сессии. Сети нет вообще: до
  `make_request` дело не доходит. Журнал: что бот отправил, какие кнопки у сообщений видны сейчас, какие callback
  ответил. Апдейты в бота подаёт `dp.feed_raw_update(bot, update)`; сборщики апдейтов - ниже.
- `FakePanel` - `httpx.MockTransport` с состоянием по `docs/bot/api.md`: приглашения, регистрации, пользователи.
  Режимы сбоев задаются `override(route, фабрика)`: 401/409/422/429/503/обрыв/таймаут отдаются по очереди, потом панель
  снова ведёт себя как настоящая. `hold(route)` останавливает запрос на сервере (для проверки гонок без `gather`).
"""

import asyncio
import heapq
import json
import re
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, InlineKeyboardMarkup, Message, User

BOT_ID = 42
BOT_TOKEN = "42:TEST"  # Bot("42:TEST"): настоящий токен тестам не нужен, в Telegram они не ходят
BOT_USERNAME = "tollwire_test_bot"
API_PREFIX = "/api/v1/bot"

# --- время ---------------------------------------------------------------------------------------------------------


class FakeClock:
    """Виртуальное время бота. `time()` - секунды с начала теста, `wall()` - «настенные» (для срока жизни записей)."""

    WALL_START = 1_800_000_000.0  # 2027-01-15: после любых дат в тестах, чтобы «срок вышел» было видно сразу

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []  # какие паузы бот просил, по порядку
        self.activity = 0  # растёт при каждом вызове Telegram/панели/sleep: по нему видно, что всё затихло
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = 0

    def time(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.WALL_START + self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.activity += 1
        if delay <= 0:
            await asyncio.sleep(0)
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._order += 1
        heapq.heappush(self._sleepers, (self.now + delay, self._order, waiter))
        await waiter

    def wake_next(self) -> bool:
        """Время переходит к ближайшему спящему. False, если спящих нет."""
        while self._sleepers:
            at, _, waiter = heapq.heappop(self._sleepers)
            if waiter.done():  # задачу отменили, пока она спала
                continue
            self.now = max(self.now, at)
            self.activity += 1
            waiter.set_result(None)
            return True
        return False

    async def drive(self, quiet: float = 0.02) -> None:
        """Фоновая задача теста: будит спящих, когда `quiet` секунд настоящего времени ничего не происходило."""
        last, still = -1, 0.0
        while True:
            await asyncio.sleep(0.004)
            if self.activity != last:
                last, still = self.activity, 0.0
                continue
            still += 0.004
            if still >= quiet and self.wake_next():
                still = 0.0

    def advance(self, seconds: float) -> None:
        """Время идёт само (например, прошла ночь между остановкой и запуском бота)."""
        self.now += seconds


# --- Telegram ------------------------------------------------------------------------------------------------------


@dataclass
class Button:
    message_id: int
    text: str
    data: str


@dataclass
class Sent:
    """Сообщение бота в чате: что в нём написано и какие кнопки у него есть сейчас."""

    message_id: int
    chat_id: int
    text: str
    parse_mode: str | None
    buttons: list[Button] = field(default_factory=list)


class FakeTelegram(BaseRequestMiddleware):
    """Подключается к сессии бота: `bot.session.middleware(fake)`. Методы не уходят в сеть."""

    def __init__(self, username: str = BOT_USERNAME) -> None:
        self.username = username
        self.methods: list[TelegramMethod[Any]] = []  # всё, что бот вызывал, по порядку
        self.messages: dict[tuple[int, int], Sent] = {}
        self.said: list[tuple[int, str]] = []  # (chat_id, текст) для каждого send/edit-текста, по порядку
        self.said_at: list[float] = []  # виртуальное время каждой записи из `said`
        self.answers: list[AnswerCallbackQuery] = []
        self.clock: FakeClock | None = None
        self._next_id = 100
        self._failures: list[tuple[type[TelegramMethod[Any]], deque[Exception]]] = []

    # --- что видно снаружи
    def texts(self, chat_id: int) -> list[str]:
        return [text for chat, text in self.said if chat == chat_id]

    def when(self, chat_id: int, text: str) -> list[float]:
        """Когда (виртуальные секунды) бот сказал этот текст в этом чате."""
        return [at for (chat, said), at in zip(self.said, self.said_at) if chat == chat_id and said == text]

    def count(self, method: type[TelegramMethod[Any]]) -> int:
        return sum(isinstance(call, method) for call in self.methods)

    def live_buttons(self, chat_id: int) -> list[Button]:
        """Кнопки, которые пользователь может нажать прямо сейчас (у сообщений с непустой клавиатурой)."""
        return [button for (chat, _), sent in self.messages.items() if chat == chat_id for button in sent.buttons]

    def parse_modes(self) -> set[str | None]:
        return {sent.parse_mode for sent in self.messages.values()}

    # --- сбои
    def fail(self, method: type[TelegramMethod[Any]], error: Exception, times: int = 1) -> None:
        """Следующие `times` вызовов этого метода бросают `error` (пользователь заблокировал бота, сеть упала)."""
        self._failures.append((method, deque([error] * times)))

    # --- перехват
    async def __call__(self, make_request: Any, bot: Bot, method: TelegramMethod[Any]) -> Any:
        self.methods.append(method)
        if self.clock is not None:
            self.clock.activity += 1
        for target, errors in self._failures:
            if isinstance(method, target) and errors:
                raise errors.popleft()
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Tollwire", username=self.username)
        if isinstance(method, SendMessage):
            return self._store(bot, method, new=True)
        if isinstance(method, EditMessageText):
            return self._store(bot, method, new=False)
        if isinstance(method, EditMessageReplyMarkup):
            sent = self.messages[(int(str(method.chat_id)), int(str(method.message_id)))]
            sent.buttons = self._buttons(sent.message_id, method.reply_markup)
            return self._message(sent)
        if isinstance(method, DeleteMessage):
            self.messages.pop((int(str(method.chat_id)), method.message_id), None)
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
            return True
        return True  # SetMyCommands, DeleteWebhook и прочее служебное

    def _store(self, bot: Bot, method: SendMessage | EditMessageText, new: bool) -> Message:
        chat_id = int(str(method.chat_id))
        parse_mode = method.parse_mode
        if isinstance(parse_mode, Default):  # значение по умолчанию берётся у бота; у нашего оно None
            parse_mode = getattr(bot.default, parse_mode.name)
        if new:
            self._next_id += 1
            sent = Sent(self._next_id, chat_id, method.text, parse_mode)
            self.messages[(chat_id, sent.message_id)] = sent
        else:
            sent = self.messages[(chat_id, int(str(method.message_id)))]
            sent.text, sent.parse_mode = method.text, parse_mode
        # Как в Telegram: правка текста без reply_markup снимает клавиатуру.
        sent.buttons = self._buttons(sent.message_id, method.reply_markup)
        self.said.append((chat_id, method.text))
        self.said_at.append(self.clock.now if self.clock else 0.0)
        return self._message(sent)

    @staticmethod
    def _buttons(message_id: int, markup: Any) -> list[Button]:
        if not isinstance(markup, InlineKeyboardMarkup):
            return []
        return [
            Button(message_id, button.text, button.callback_data or "")
            for row in markup.inline_keyboard
            for button in row
        ]

    @staticmethod
    def _message(sent: Sent) -> Message:
        return Message(
            message_id=sent.message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=sent.chat_id, type="private"),
            text=sent.text,
            from_user=User(id=BOT_ID, is_bot=True, first_name="Tollwire", username=BOT_USERNAME),
        )


# --- апдейты -------------------------------------------------------------------------------------------------------

DATE = 1_700_000_000  # не 0: нулевая дата делает сообщение «недоступным» для aiogram


class Updates:
    """Сборщик сырых апдейтов Telegram для `dp.feed_raw_update`."""

    def __init__(self) -> None:
        self._id = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def message(
        self,
        user_id: int,
        text: str,
        *,
        username: str | None = "ivan_p",
        first_name: str = "Иван",
        chat_id: int | None = None,
        chat_type: str = "private",
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": self.next_id() + 1000,
            "date": DATE,
            "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
            "from": self.user(user_id, username, first_name),
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return {"update_id": self.next_id(), "message": message}

    def sticker(self, user_id: int, *, username: str | None = "ivan_p", first_name: str = "Иван") -> dict[str, Any]:
        """Сообщение без текста (стикер): человек ответил не словами."""
        message = {
            "message_id": self.next_id() + 1000,
            "date": DATE,
            "chat": {"id": user_id, "type": "private"},
            "from": self.user(user_id, username, first_name),
            "sticker": {
                "file_id": "sticker-file",
                "file_unique_id": "sticker-unique",
                "type": "regular",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
            },
        }
        return {"update_id": self.next_id(), "message": message}

    def callback(
        self, user_id: int, button: Button, text: str, *, username: str | None = "ivan_p", first_name: str = "Иван"
    ) -> dict[str, Any]:
        return {
            "update_id": self.next_id(),
            "callback_query": {
                "id": f"cb{self.next_id()}",
                "from": self.user(user_id, username, first_name),
                "chat_instance": "test",
                "data": button.data,
                "message": {
                    "message_id": button.message_id,
                    "date": DATE,
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Tollwire", "username": BOT_USERNAME},
                    "text": text,
                },
            },
        }

    @staticmethod
    def user(user_id: int, username: str | None, first_name: str) -> dict[str, Any]:
        user: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": first_name}
        if username:
            user["username"] = username
        return user


# --- панель --------------------------------------------------------------------------------------------------------

Outcome = Callable[[], httpx.Response | Exception]  # фабрика: каждый раз новый ответ (или исключение транспорта)


def unavailable(status: int = 503) -> Outcome:
    return lambda: httpx.Response(status, json={"detail": "Service unavailable"})


def dropped() -> Outcome:
    """Обрыв: соединение закрыто посреди ответа."""
    return lambda: httpx.RemoteProtocolError("server disconnected without sending a response")


def timed_out() -> Outcome:
    return lambda: httpx.ReadTimeout("timed out")


def unauthorized() -> Outcome:
    return lambda: httpx.Response(401, json={"detail": "Invalid bot key"})


def conflict(reason: str | None) -> Outcome:
    body: dict[str, Any] = {"detail": "Conflict"}
    if reason is not None:
        body["reason"] = reason
    return lambda: httpx.Response(409, json=body)


def rejected(field_name: str, echo: str = "") -> Outcome:
    """422 как у панели: loc = ["body", поле]. В msg лежит введённое значение - бот не должен его логировать."""
    detail = [{"type": "value_error", "loc": ["body", field_name], "msg": f"Value error, {echo}", "ctx": {}}]
    return lambda: httpx.Response(422, json={"detail": detail})


def rate_limited(retry_after: int | None = 3) -> Outcome:
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return lambda: httpx.Response(429, json={"detail": "Too many requests"}, headers=headers)


def redirect(location: str) -> Outcome:
    return lambda: httpx.Response(307, headers={"Location": location})


@dataclass
class Call:
    method: str
    path: str  # полный путь запроса, например /api/v1/bot/heartbeat
    route: str  # "POST /registrations", "GET /registrations/*": без префикса и с id вместо значений
    body: Any
    headers: dict[str, str]
    at: float  # виртуальное время запроса
    query: str = ""


class Hold:
    """Запрос остановлен на стороне панели, пока тест не вызовет `release()`."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self._open = asyncio.Event()
        self.requests = 0

    def release(self) -> None:
        self._open.set()


_FORBIDDEN_NAME = ("Cc", "Cf", "Cs", "Zl", "Zp")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FAR_DATE = "2026-11-25T12:00:00Z"


class FakePanel:
    """Bot API панели (docs/bot/api.md) в памяти. Состояние - обычные словари: тест читает и меняет их напрямую."""

    def __init__(self, clock: FakeClock, key: str = "twb_" + "k" * 43) -> None:
        self.clock = clock
        self.key = key
        self.invites: dict[str, str] = {}  # код -> valid / used / expired / revoked
        self.invite_users: dict[str, int] = {}  # код -> telegram_id, который его занял
        self.users: dict[int, dict[str, Any]] = {}  # telegram_id -> {name, valid_until, protocol, config_url}
        self.registrations: dict[str, dict[str, Any]] = {}
        self.calls: list[Call] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)
        self._overrides: list[tuple[str, deque[Outcome]]] = []
        self._holds: dict[str, Hold] = {}
        self._scheduled: list[tuple[float, Callable[[], None]]] = []

    # --- подготовка состояния
    def add_invite(self, code: str = "INVITE-1", status: str = "valid") -> str:
        self.invites[code] = status
        return code

    def add_user(self, telegram_id: int, name: str = "Иван Петров", config_url: str | None = None) -> None:
        self.users[telegram_id] = {
            "name": name,
            "valid_until": _FAR_DATE,
            "protocol": "amneziawg",
            "config_url": config_url,
        }

    def finish(self, registration_id: str, status: str = "active", config_url: str | None = None) -> None:
        """Выдача закончилась: статус меняется, при `active` появляется пользователь (его покажет /status)."""
        registration = self.registrations[registration_id]
        registration["status"], registration["config_url"] = status, config_url
        if status == "active":
            self.add_user(registration["telegram_id"], registration["name"], config_url)

    def only_registration(self) -> str:
        assert len(self.registrations) == 1, f"expected one registration, there are {len(self.registrations)}"
        return next(iter(self.registrations))

    # --- сбои, остановки, расписание
    def override(self, route: str, *outcomes: Outcome, times: int = 1) -> None:
        """Следующие запросы по маршруту получат эти исходы по очереди (каждый - `times` раз)."""
        self._overrides.append((route, deque(outcome for outcome in outcomes for _ in range(times))))

    def recover(self) -> None:
        """Панель снова отвечает как обычно: оставшиеся в очереди сбои отменены."""
        self._overrides.clear()

    def hold(self, route: str) -> Hold:
        hold = self._holds[route] = Hold()
        return hold

    def at(self, seconds: float, action: Callable[[], None]) -> None:
        """Через `seconds` виртуальных секунд (считая от сейчас) выполнить `action` - перед ближайшим запросом."""
        self._scheduled.append((self.clock.now + seconds, action))

    # --- журнал
    def routes(self) -> list[str]:
        return [call.route for call in self.calls]

    def count(self, route: str) -> int:
        return self.routes().count(route)

    def bodies(self, route: str) -> list[Any]:
        return [call.body for call in self.calls if call.route == route]

    # --- обработка
    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.clock.activity += 1
        self._run_scheduled()
        path = request.url.path
        route = self._route(request.method, path)
        body = json.loads(request.content) if request.content else None
        self.calls.append(
            Call(
                request.method,
                path,
                route,
                body,
                {name.lower(): value for name, value in request.headers.items()},
                self.clock.now,
                request.url.query.decode(),
            )
        )
        hold = self._holds.get(route)
        if hold is not None:
            hold.requests += 1
            hold.reached.set()
            await hold._open.wait()
        for pattern, queue in self._overrides:
            if pattern == route and queue:
                outcome = queue.popleft()()
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if request.headers.get("X-Bot-Key") != self.key:
            return httpx.Response(401, json={"detail": "Invalid bot key"})
        return self._dispatch(route, path, body)

    def _run_scheduled(self) -> None:
        due = [item for item in self._scheduled if item[0] <= self.clock.now]
        self._scheduled = [item for item in self._scheduled if item not in due]
        for _, action in due:
            action()

    @staticmethod
    def _route(method: str, path: str) -> str:
        tail = path.removeprefix(API_PREFIX)
        for prefix in ("/registrations/", "/users/by-telegram/"):
            if tail.startswith(prefix):
                tail = prefix + "*"
        return f"{method} {tail}"

    def _dispatch(self, route: str, path: str, body: Any) -> httpx.Response:
        if not path.startswith(API_PREFIX + "/"):
            return httpx.Response(404, json={"detail": "Not Found"})
        last = path.rsplit("/", 1)[-1]
        if route == "POST /heartbeat":
            return self._heartbeat(body)
        if route == "POST /invites/check":
            status = self.invites.get(str(body.get("code", "")), "unknown")
            return httpx.Response(200, json={"status": status})
        if route == "POST /registrations":
            return self._register(body)
        if route == "GET /registrations/*":
            if not _UUID.match(last):
                return self._invalid("registration_id")
            registration = self.registrations.get(last)
            if registration is None:
                return httpx.Response(404, json={"detail": "Registration not found"})
            return httpx.Response(200, json=self._registration_json(last))
        if route == "GET /users/by-telegram/*":
            if not last.isdigit() or int(last) < 1:
                return self._invalid("telegram_id")
            user = self.users.get(int(last))
            if user is None:
                return httpx.Response(404, json={"detail": "User not found"})
            return httpx.Response(200, json=user)
        return httpx.Response(404, json={"detail": "Not Found"})

    def _heartbeat(self, body: Any) -> httpx.Response:
        username, version = str(body.get("bot_username", "")), str(body.get("version", ""))
        if not re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", username):
            return self._invalid("bot_username")
        if not 1 <= len(version) <= 32:
            return self._invalid("version")
        self.heartbeats.append({"bot_username": username, "version": version, "at": self.clock.now})
        return httpx.Response(200, json={})

    def _register(self, body: Any) -> httpx.Response:
        name = str(body.get("name", "")).strip()
        if not 1 <= len(name) <= 64 or any(unicodedata.category(c) in _FORBIDDEN_NAME for c in name):
            return self._invalid("name")
        email = str(body.get("email", ""))
        if not _EMAIL.match(email):
            return self._invalid("email")
        code, telegram_id = str(body.get("code", "")), body.get("telegram_id")
        if not isinstance(telegram_id, int) or telegram_id < 1:
            return self._invalid("telegram_id")
        status = self.invites.get(code)
        if status == "used":
            same = next(
                (rid for rid, r in self.registrations.items() if r["code"] == code and r["telegram_id"] == telegram_id),
                None,
            )
            if same is not None:  # повтор того же человека: та же регистрация, ничего не создаётся
                return httpx.Response(201, json=self._registration_json(same))
            return self._conflict("invite_used")
        if status is None or status == "revoked":
            return self._conflict("invite_invalid")
        if status == "expired":
            return self._conflict("invite_expired")
        open_registration = any(
            r["telegram_id"] == telegram_id and r["status"] in ("payment_pending", "provisioning")
            for r in self.registrations.values()
        )
        if telegram_id in self.users or open_registration:
            return self._conflict("telegram_taken")
        emails = {r["email"].lower() for r in self.registrations.values()}
        if email.lower() in emails:
            return self._conflict("email_taken")
        registration_id = str(uuid.uuid4())
        self.registrations[registration_id] = {
            "status": "provisioning",
            "config_url": None,
            "telegram_id": telegram_id,
            "name": name,
            "email": email,
            "code": code,
        }
        self.invites[code] = "used"
        return httpx.Response(201, json=self._registration_json(registration_id))

    def _registration_json(self, registration_id: str) -> dict[str, Any]:
        registration = self.registrations[registration_id]
        return {
            "registration_id": registration_id,
            "status": registration["status"],
            "config_url": registration["config_url"],
        }

    @staticmethod
    def _conflict(reason: str) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Conflict", "reason": reason})

    @staticmethod
    def _invalid(field_name: str) -> httpx.Response:
        detail = [{"type": "value_error", "loc": ["body", field_name], "msg": "Invalid value", "ctx": {}}]
        return httpx.Response(422, json={"detail": detail})
