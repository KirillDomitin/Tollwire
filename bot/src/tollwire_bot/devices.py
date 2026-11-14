"""Personal gateway devices, addressed by credential ID through the Bot API."""

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tollwire_bot.dialog import problem_text
from tollwire_bot.panel import Conflict, PanelClient, PanelError
from tollwire_bot.refusals import CONFIG_REFUSAL_TEXT
from tollwire_bot.texts import format_date, t
from tollwire_bot.validation import clean_name

_STATE_TEXT = {
    "reserved": "device_state_reserved",
    "provisioning": "device_state_provisioning",
    "active": "device_state_active",
    "revoking": "device_state_revoking",
    "revoked": "device_state_revoked",
}


class AddDevice(StatesGroup):
    name = State()


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


def refusal(error: PanelError) -> str:
    if isinstance(error, Conflict):
        if error.reason == "device_limit":
            return t("device_limit")
        return t(CONFIG_REFUSAL_TEXT.get(error.reason, "conflict_generic"))
    return problem_text(error)


async def show(message: Message, telegram_id: int, panel: PanelClient, data: dict[str, Any] | None = None) -> None:
    try:
        if data is None:
            data = await panel.devices(telegram_id)
    except PanelError as error:
        await message.answer(refusal(error))
        return
    if data is None:
        await message.answer(t("not_registered"))
        return
    rows: list[list[tuple[str, str]]] = []
    lines = [t("devices", occupied=data["occupied"], limit=data["limit"])]
    gateways = {item["grant_id"]: item["server_name"] for item in data["gateways"]}
    for device in data["devices"]:
        identifier = str(UUID(device["id"]))
        name = device.get("device_name") or t("device_unnamed", id=identifier[:8])
        state = device["state"]
        gateway = gateways.get(device["access_grant_id"])
        status = t("device_no_access") if gateway is None else t(_STATE_TEXT[state])
        heading = f"{name} — {gateway}" if gateway else name
        lines.append(f"{heading}: {status}")
        actions = []
        if gateway is not None and state == "active" and device.get("has_config"):
            actions.append((t("device_config", name=name), f"dev:config:{identifier}"))
        if state not in ("revoking", "revoked"):
            actions.append((t("device_revoke", name=name), f"dev:revoke:{identifier}"))
        if actions:
            rows.append(actions)
    if data["gateways"] and data["occupied"] < data["limit"]:
        rows.append([(t("device_add"), "dev:add")])
    rows.append([(t("device_refresh"), "dev:list")])
    await message.answer("\n".join(lines), reply_markup=keyboard(rows), parse_mode=None)


def register(router: Router) -> None:
    @router.message(Command("devices"))
    async def devices(message: Message, state: FSMContext, panel: PanelClient) -> None:
        assert message.from_user is not None
        await state.clear()
        await show(message, message.from_user.id, panel)

    @router.callback_query(F.data.startswith("dev:"))
    async def action(callback: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        message = callback.message
        telegram_id = callback.from_user.id
        parts = (callback.data or "").split(":")
        action = parts[1]
        try:
            if action == "list":
                await state.clear()
                await show(message, telegram_id, panel)
            elif action == "add":
                await state.clear()
                data = await panel.devices(telegram_id)
                if data is None:
                    await message.answer(t("not_registered"))
                elif data["occupied"] >= data["limit"]:
                    await message.answer(t("device_limit"))
                elif not data["gateways"]:
                    await message.answer(t("config_no_access"))
                else:
                    rows = [[(item["server_name"], f"dev:gateway:{item['grant_id']}")] for item in data["gateways"]]
                    await message.answer(t("device_choose_gateway"), reply_markup=keyboard(rows))
            elif action == "gateway" and len(parts) == 3:
                grant_id = str(UUID(parts[2]))
                data = await panel.devices(telegram_id)
                if data is None or not any(item["grant_id"] == grant_id for item in data["gateways"]):
                    await message.answer(t("config_no_access"))
                    return
                # Repeated gateway callback preserves the operation token and cannot allocate twice.
                request_id = uuid5(
                    NAMESPACE_URL, f"tollwire-device:{telegram_id}:{message.chat.id}:{message.message_id}:{grant_id}"
                )
                await state.update_data(device_grant_id=grant_id, device_request_id=str(request_id))
                await state.set_state(AddDevice.name)
                await message.answer(t("device_ask_name"))
            elif action == "config" and len(parts) == 3:
                link = await panel.device_config_link(telegram_id, str(UUID(parts[2])))
                await message.answer(
                    t("config_link", url=link.config_url, until=format_date(link.valid_until))
                    if link
                    else t("config_no_access")
                )
            elif action == "revoke" and len(parts) == 3:
                identifier = str(UUID(parts[2]))
                await message.answer(
                    t("device_revoke_confirm"),
                    reply_markup=keyboard(
                        [[(t("device_revoke_yes"), f"dev:remove:{identifier}"), (t("device_cancel"), "dev:list")]]
                    ),
                )
            elif action == "remove" and len(parts) == 3:
                await panel.revoke_device(telegram_id, str(UUID(parts[2])))
                await show(message, telegram_id, panel)
        except ValueError:
            return
        except PanelError as error:
            await message.answer(refusal(error))

    @router.message(AddDevice.name, ~F.text.startswith("/"))
    async def name(message: Message, state: FSMContext, panel: PanelClient) -> None:
        assert message.from_user is not None
        label = (message.text or "").strip()
        if clean_name(label) is None:
            await message.answer(t("bad_name"))
            return
        data = await state.get_data()
        try:
            await panel.add_device(message.from_user.id, data["device_grant_id"], label, data["device_request_id"])
        except PanelError as error:
            await message.answer(refusal(error))
            return
        await state.clear()
        await message.answer(t("device_preparing"))
        await show(message, message.from_user.id, panel)
