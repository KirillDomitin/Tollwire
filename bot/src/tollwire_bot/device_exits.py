"""Versioned, single-device exit selection persisted in the existing SQLite FSM."""

from collections.abc import Callable
from secrets import token_hex
from typing import Any
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tollwire_bot.devices import keyboard, refusal
from tollwire_bot.panel import Conflict, PanelClient, PanelError, PanelUnavailable, PanelUnexpected
from tollwire_bot.texts import t

TTL = 600


def description(data: dict[str, Any]) -> str:
    current, pinned = data["current_exit"], data["pinned_exit"]
    lines = [t("device_exit_heading", name=data["device_name"], current=current["name"] if current else "—")]
    if pinned is not None:
        lines.append(t("device_exit_pinned", name=pinned["name"]))
    else:
        lines.append(t("device_exit_automatic"))
    return "\n".join(lines)


def exit_refusal(error: PanelError) -> str:
    reasons = {
        "exit_unavailable": "device_exit_unavailable",
        "exit_full": "device_exit_full",
        "gateway_busy": "device_exit_busy",
        "stale_confirmation": "device_exit_stale",
    }
    if isinstance(error, Conflict) and error.reason in reasons:
        return t(reasons[error.reason])
    return refusal(error)


def register(router: Router, clock: Callable[[], float]) -> None:
    async def display(message: Message, state: FSMContext, telegram_id: int, data: dict[str, Any]) -> None:
        nonce = token_hex(8)
        operation = {
            "nonce": nonce,
            "user": telegram_id,
            "chat": message.chat.id,
            "deadline": clock() + TTL,
            "data": data,
            "phase": "select",
        }
        rows = [
            [(item["name"], f"devx:{nonce}:pick:{index}")]
            for index, item in enumerate(data["exits"])
            if item["selectable"]
        ]
        if data["current_exit"] is not None:
            rows.append([(t("device_exit_auto_button"), f"devx:{nonce}:auto")])
        rows.extend([[(t("device_refresh"), f"devx:{nonce}:refresh")], [(t("device_cancel"), f"devx:{nonce}:cancel")]])
        text = description(data)
        if not any(item["selectable"] for item in data["exits"]):
            text += "\n" + t("device_exit_empty")
        sent = await message.answer(text, reply_markup=keyboard(rows), parse_mode=None)
        operation["message"] = sent.message_id
        await state.set_data({"exit_operation": operation})

    @router.callback_query(F.data.startswith("dev:exit:"))
    async def begin(callback: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
        await callback.answer()
        if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
            return
        await state.clear()
        try:
            credential = str(UUID((callback.data or "").split(":")[2]))
            data = await panel.device_exit_options(callback.from_user.id, credential)
            if data is None:
                raise Conflict("no_access")
            await display(callback.message, state, callback.from_user.id, data)
        except (ValueError, IndexError):
            return
        except PanelError as error:
            await callback.message.answer(exit_refusal(error))

    @router.callback_query(F.data.startswith("devx:"))
    async def action(callback: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
        await callback.answer()
        if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
            return
        message, telegram_id = callback.message, callback.from_user.id
        parts = (callback.data or "").split(":")
        op = (await state.get_data()).get("exit_operation")
        if (
            not isinstance(op, dict)
            or len(parts) < 3
            or parts[1] != op.get("nonce")
            or op.get("user") != telegram_id
            or op.get("chat") != message.chat.id
            or op.get("message") != message.message_id
            or clock() >= op.get("deadline", 0)
        ):
            await message.answer(t("device_exit_stale"))
            return
        data, command = op["data"], parts[2]
        result: dict[str, Any] | None
        try:
            if command == "cancel":
                await state.clear()
                await message.answer(t("device_exit_cancelled"))
                return
            if command in ("refresh", "back"):
                await state.clear()
                fresh = await panel.device_exit_options(telegram_id, data["credential_id"])
                if fresh is None:
                    raise Conflict("no_access")
                await display(message, state, telegram_id, fresh)
                return
            if command in ("pick", "auto") and op["phase"] == "select":
                target = None
                if command == "pick":
                    if len(parts) != 4 or not parts[3].isdigit():
                        return
                    index = int(parts[3])
                    if index >= len(data["exits"]) or not data["exits"][index]["selectable"]:
                        return
                    target = data["exits"][index]
                elif data["current_exit"] is None:
                    return
                op.update(nonce=token_hex(8), target=target, phase="confirm")
                nonce = op["nonce"]
                current = data["current_exit"]
                text = t(
                    "device_exit_confirm",
                    name=data["device_name"],
                    current=current["name"] if current else "—",
                    target=target["name"] if target else t("device_exit_auto_button"),
                )
                sent = await message.answer(
                    text,
                    reply_markup=keyboard(
                        [
                            [(t("device_exit_confirm_button"), f"devx:{nonce}:confirm")],
                            [
                                (t("device_exit_back"), f"devx:{nonce}:back"),
                                (t("device_cancel"), f"devx:{nonce}:cancel"),
                            ],
                        ]
                    ),
                    parse_mode=None,
                )
                op["message"] = sent.message_id
                await state.set_data({"exit_operation": op})
            elif command == "confirm" and op["phase"] == "confirm":
                # Persist consumption BEFORE requesting: a restart cannot replay an ambiguous mutation.
                op["phase"] = "consumed"
                await state.set_data({"exit_operation": op})
                try:
                    result = await panel.change_device_exit(
                        telegram_id,
                        data["credential_id"],
                        op["target"]["id"] if op["target"] else None,
                        data["revision"],
                    )
                    target = op["target"]
                    if target is None:
                        verified = result["pinned_exit"] is None and result["mode"] == "automatic"
                    else:
                        verified = (
                            result["current_exit"] is not None
                            and result["pinned_exit"] is not None
                            and result["current_exit"]["id"] == target["id"]
                            and result["pinned_exit"]["id"] == target["id"]
                        )
                    if not verified:
                        raise PanelUnexpected("unconfirmed exit change")
                except (PanelUnavailable, PanelUnexpected, Conflict) as error:
                    if isinstance(error, Conflict) and error.reason != "stale_confirmation":
                        raise
                    await message.answer(t("device_exit_verify"))
                    result = await panel.device_exit_options(telegram_id, data["credential_id"])
                    if result is None:
                        raise Conflict("no_access")
                else:
                    await message.answer(t("device_exit_applied") if op["target"] else t("device_exit_unpinned"))
                await display(message, state, telegram_id, result)
            elif command == "confirm" and op["phase"] == "consumed":
                await message.answer(t("device_exit_verify"))
                result = await panel.device_exit_options(telegram_id, data["credential_id"])
                if result is None:
                    raise Conflict("no_access")
                await display(message, state, telegram_id, result)
        except PanelError as error:
            sent = await message.answer(
                exit_refusal(error),
                reply_markup=keyboard(
                    [
                        [(t("device_refresh"), f"devx:{op['nonce']}:refresh")],
                        [(t("device_cancel"), f"devx:{op['nonce']}:cancel")],
                    ]
                ),
            )
            op["message"] = sent.message_id
            await state.set_data({"exit_operation": op})
