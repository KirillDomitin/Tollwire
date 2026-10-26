"""Команды /status и /help и ответ на всё прочее."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from tollwire_bot.dialog import problem_text
from tollwire_bot.panel import PanelClient, PanelError
from tollwire_bot.texts import format_date, t


def register_commands(router: Router) -> None:
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
