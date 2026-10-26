"""Диалог регистрации (ТЗ 6.1): /start <код> -> имя -> почта -> подтверждение -> POST /registrations.

Состояние (код, имя, почта) лежит в FSM-хранилище и после рестарта бота не теряется. Тексты - только через t();
parse_mode не задан, поэтому имя и почта в сообщениях остаются обычным текстом."""

import contextlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tollwire_bot.panel import Conflict, PanelClient, PanelError, RateLimited, Rejected
from tollwire_bot.texts import t
from tollwire_bot.validation import clean_email, clean_name, valid_code
from tollwire_bot.watcher import Watcher

YES, EDIT = "reg:yes", "reg:edit"

# Статус приглашения от панели -> ключ текста. Всё незнакомое читается как «нет такого».
INVITE_PROBLEMS = {
    "used": "invite_used",
    "expired": "invite_expired",
    "revoked": "invite_revoked",
    "unknown": "invite_unknown",
}
# Причина отказа при регистрации -> ключ текста; диалог на этом заканчивается.
CONFLICT_ENDS = {
    "telegram_taken": "already_registered",
    "invite_used": "invite_used",
    "invite_expired": "invite_expired",
    "invite_invalid": "invite_unknown",
}


class Registration(StatesGroup):
    name = State()
    email = State()
    confirm = State()
    submitting = State()  # POST ушёл в панель: повторные «Да» в это время ничего не делают


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("btn_yes"), callback_data=YES),
                InlineKeyboardButton(text=t("btn_edit"), callback_data=EDIT),
            ]
        ]
    )


def problem_text(error: PanelError) -> str:
    """Что сказать человеку про сбой панели. Про ключ и сертификат ему знать незачем: это дело админа и лога."""
    return t("rate_limited") if isinstance(error, RateLimited) else t("panel_unavailable")


async def set_buttons(message: Message, markup: InlineKeyboardMarkup | None) -> None:
    with contextlib.suppress(TelegramBadRequest):  # сообщение уже без кнопок или удалено
        await message.edit_reply_markup(reply_markup=markup)


def register(router: Router) -> None:
    @router.message(Command("start"))
    async def start(message: Message, command: CommandObject, state: FSMContext, panel: PanelClient) -> None:
        assert message.from_user is not None
        await state.clear()  # /start посреди диалога начинает его заново
        code = (command.args or "").strip()
        if not code:
            await message.answer(t("no_code"))
            return
        if not valid_code(code):
            await message.answer(t("bad_link"))
            return
        try:
            if await panel.user_by_telegram(message.from_user.id) is not None:
                await message.answer(t("already_registered"))
                return
            status = await panel.check_invite(code)
        except PanelError as error:
            await message.answer(problem_text(error))
            return
        if status != "valid":
            await message.answer(t(INVITE_PROBLEMS.get(status, "invite_unknown")))
            return
        await state.set_data({"code": code})
        await state.set_state(Registration.name)
        await message.answer(t("ask_name"))

    @router.message(Registration.name)
    async def got_name(message: Message, state: FSMContext) -> None:
        text = message.text or ""
        if text.startswith("/"):
            await message.answer(t("unknown_input"))
            return
        name = clean_name(text)
        if name is None:
            await message.answer(t("bad_name"))
            return
        await state.update_data(name=name)
        await state.set_state(Registration.email)
        await message.answer(t("ask_email"))

    @router.message(Registration.email)
    async def got_email(message: Message, state: FSMContext) -> None:
        text = message.text or ""
        if text.startswith("/"):
            await message.answer(t("unknown_input"))
            return
        email = clean_email(text)
        if email is None:
            await message.answer(t("bad_email"))
            return
        data = await state.update_data(email=email)
        await state.set_state(Registration.confirm)
        await message.answer(t("confirm", name=data["name"], email=email), reply_markup=confirm_keyboard())

    @router.callback_query(F.data == EDIT)
    async def edit(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if await state.get_state() != Registration.confirm.state or not isinstance(callback.message, Message):
            return  # старая кнопка
        await state.set_state(Registration.name)
        await set_buttons(callback.message, None)
        await callback.message.answer(t("ask_name"))

    @router.callback_query(F.data == YES)
    async def confirmed(
        callback: CallbackQuery, state: FSMContext, panel: PanelClient, watcher: Watcher, bot: Bot
    ) -> None:
        await callback.answer()  # без этого клиент Telegram продолжает «крутить» кнопку
        message = callback.message
        if await state.get_state() != Registration.confirm.state or not isinstance(message, Message):
            return  # старая кнопка или повторное нажатие, пока первое в работе
        data = await state.get_data()
        await state.set_state(Registration.submitting)
        await set_buttons(message, None)  # пока панель думает, нажимать уже нечего
        try:
            registration = await panel.register(
                code=data["code"],
                telegram_id=callback.from_user.id,
                telegram_username=callback.from_user.username,
                name=data["name"],
                email=data["email"],
            )
        except Conflict as conflict:
            await refused(message, state, conflict.reason)
        except Rejected as rejected:
            await rejected_field(message, state, rejected.field)
        except PanelError as error:
            # Панель лежит или просит подождать: остаёмся в «подтверждении», «Да» можно нажать ещё раз.
            await state.set_state(Registration.confirm)
            await set_buttons(message, confirm_keyboard())
            await message.answer(problem_text(error))
        else:
            await watcher.add(bot, registration.registration_id, message.chat.id, callback.from_user.id)
            await state.clear()
            await message.answer(t("preparing"))


async def refused(message: Message, state: FSMContext, reason: str) -> None:
    if reason == "email_taken":
        await state.set_state(Registration.email)  # имя и код остаются
        await message.answer(t("email_taken"))
        return
    await state.clear()
    await message.answer(t(CONFLICT_ENDS.get(reason, "conflict_generic")))


async def rejected_field(message: Message, state: FSMContext, field: str) -> None:
    if field == "name":
        await state.set_state(Registration.name)
        await message.answer(t("bad_name"))
    elif field == "email":
        await state.set_state(Registration.email)
        await message.answer(t("bad_email"))
    else:
        await state.clear()
        await message.answer(t("conflict_generic"))
