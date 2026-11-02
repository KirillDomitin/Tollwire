"""Команды /status и /help и ответ на всё прочее."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tollwire_bot.dialog import problem_text
from tollwire_bot.panel import Conflict, PanelClient, PanelError
from tollwire_bot.refusals import CONFIG_REFUSAL_TEXT
from tollwire_bot.texts import format_date, t
from tollwire_bot.validation import valid_code


async def link_admin(message: Message, code: str, state: FSMContext, panel: PanelClient) -> None:
    await state.clear()
    if len(code) != 32 or not valid_code(code):
        await message.answer(t("admin_link_usage"))
        return
    assert message.from_user is not None
    try:
        result = await panel.admin_link(code, message.from_user.id, message.from_user.username)
    except Conflict as error:
        key = {"link_expired": "admin_link_expired", "telegram_taken": "admin_link_taken"}.get(
            error.reason, "admin_link_invalid"
        )
        await message.answer(t(key))
    except PanelError as error:
        await message.answer(problem_text(error))
    else:
        await message.answer(t("admin_linked", name=result.admin_name), parse_mode=None)


def register_commands(router: Router) -> None:
    @router.message(Command("admin"))
    async def admin(message: Message, command: CommandObject, state: FSMContext, panel: PanelClient) -> None:
        await link_admin(message, (command.args or "").strip(), state, panel)

    @router.message(Command("status"))
    async def status(message: Message, panel: PanelClient) -> None:
        assert message.from_user is not None
        try:
            user = await panel.user_by_telegram(message.from_user.id)
        except PanelError as error:
            await message.answer(problem_text(error))
            return
        if user is None:
            await message.answer(t("not_registered"))
            return
        await message.answer(
            t("status", name=user.name, until=format_date(user.valid_until), protocol=user.protocol or "-")
        )

    @router.message(Command("config"))
    async def config(message: Message, panel: PanelClient) -> None:
        """Новая ссылка на страницу конфига; прежняя перестаёт открываться. В чат уходит только адрес страницы."""
        assert message.from_user is not None
        try:
            link = await panel.config_link(message.from_user.id)
        except Conflict as refusal:
            await message.answer(t(CONFIG_REFUSAL_TEXT.get(refusal.reason, "conflict_generic")))
            return
        except PanelError as error:
            await message.answer(problem_text(error))
            return
        if link is None:
            await message.answer(t("not_registered"))
            return
        await message.answer(t("config_link", url=link.config_url, until=format_date(link.valid_until)))

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(t("help"))


def register_fallback(router: Router) -> None:
    """Регистрируется последним: всё, что не поймал диалог и команды."""

    @router.callback_query(F.data)
    async def other_button(callback: CallbackQuery) -> None:
        await callback.answer()  # нажатие подтверждено, больше ничего

    @router.message()
    async def anything_else(message: Message) -> None:
        await message.answer(t("unknown_input"))
