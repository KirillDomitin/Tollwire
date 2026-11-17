"""The actual bot exit_client must agree with the actual device exit Bot API."""

from collections.abc import AsyncIterator
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from main import app
from tests.bot_client_support import Conflict, PanelClient, PanelUnexpected
from tests.bot_support import TELEGRAM_ID
from tests.integration.test_bot_device_exit_switch import SwitchScene, switch  # noqa: F401


@pytest.fixture
async def exit_client(switch: SwitchScene) -> AsyncIterator[PanelClient]:  # noqa: F811
    async def instant(_: float) -> None:
        pass

    result = PanelClient(
        "http://testserver", switch.bot.headers["x-bot-key"],
        transport=httpx.ASGITransport(app=app), sleep=instant,
    )
    try:
        yield result
    finally:
        await result.aclose()


async def test_real_client_pins_unpins_and_preserves_revision_cas(
    exit_client: PanelClient, switch: SwitchScene  # noqa: F811
) -> None:
    credential = str(switch.credential.id)
    before = await exit_client.device_exit_options(TELEGRAM_ID, credential)
    assert before is not None
    target = next(row["id"] for row in before["exits"] if row["id"] != before["current_exit"]["id"])
    changed = await exit_client.change_device_exit(TELEGRAM_ID, credential, target, before["revision"])
    assert changed["current_exit"]["id"] == changed["pinned_exit"]["id"] == target
    assert changed["mode"] == "pinned"
    with pytest.raises(Conflict) as stale:
        await exit_client.change_device_exit(TELEGRAM_ID, credential, target, before["revision"])
    assert stale.value.reason == "stale_confirmation"
    automatic = await exit_client.change_device_exit(TELEGRAM_ID, credential, None, changed["revision"])
    assert automatic["mode"] == "automatic" and automatic["pinned_exit"] is None
    assert automatic["current_exit"] == changed["current_exit"]


async def test_missing_device_maps_to_none_and_no_access(exit_client: PanelClient) -> None:
    credential = str(uuid4())
    assert await exit_client.device_exit_options(TELEGRAM_ID, credential) is None
    with pytest.raises(Conflict) as error:
        await exit_client.change_device_exit(TELEGRAM_ID, credential, None, None)
    assert error.value.reason == "no_access"


def safe_options() -> dict:
    target = {"id": str(uuid4()), "name": "Amsterdam"}
    return {
        "credential_id": str(uuid4()), "device_name": "Phone",
        "current_exit": target, "pinned_exit": None, "mode": "automatic",
        "revision": str(uuid4()),
        "exits": [{**target, "selectable": True, "reason": None}],
    }


@pytest.mark.parametrize("field,value", [
    ("credential_id", "secret-invalid-id"), ("device_name", 123),
    ("current_exit", {"id": str(uuid4()), "name": None}),
    ("pinned_exit", {"id": "invalid", "name": "Secret"}),
    ("mode", "secret-mode"), ("revision", "secret-revision"),
    ("exits", [None]),
    ("exits", [{"id": str(uuid4()), "name": "Exit", "selectable": 1, "reason": None}]),
    ("exits", [{"id": str(uuid4()), "name": "Exit", "selectable": False, "reason": "secret"}]),
])
@pytest.mark.parametrize("mutation", [False, True])
async def test_exit_dto_is_validated_before_handler_sees_it(field: str, value: object, mutation: bool) -> None:
    body = safe_options()
    body[field] = value
    exit_client = PanelClient("http://testserver", "test-key", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=deepcopy(body)),
    ))
    try:
        with pytest.raises(PanelUnexpected) as error:
            if mutation:
                await exit_client.change_device_exit(TELEGRAM_ID, body["credential_id"], None, None)
            else:
                await exit_client.device_exit_options(TELEGRAM_ID, body["credential_id"])
        assert "secret" not in str(error.value).lower()
    finally:
        await exit_client.aclose()


@pytest.mark.parametrize("reason", ["exit_unavailable", "exit_full", "gateway_busy", "stale_confirmation"])
async def test_exit_conflict_reasons_are_preserved(reason: str) -> None:
    exit_client = PanelClient("http://testserver", "test-key", transport=httpx.MockTransport(
        lambda _: httpx.Response(409, json={"reason": reason}),
    ))
    try:
        with pytest.raises(Conflict) as error:
            await exit_client.change_device_exit(TELEGRAM_ID, str(uuid4()), None, None)
        assert error.value.reason == reason
    finally:
        await exit_client.aclose()


async def test_lost_reply_retries_same_cas_body_and_reads_confirmed_result(switch: SwitchScene) -> None:  # noqa: F811
    transport = httpx.ASGITransport(app=app)
    requests: list[bytes] = []

    async def dispatch(request: httpx.Request) -> httpx.Response:
        response = await transport.handle_async_request(request)
        if request.method == "PATCH":
            requests.append(request.content)
            if len(requests) == 1:
                assert response.status_code == 200
                raise httpx.ReadTimeout("reply lost", request=request)
        return response

    async def instant(_: float) -> None:
        pass

    exit_client = PanelClient(
        "http://testserver", switch.bot.headers["x-bot-key"],
        transport=httpx.MockTransport(dispatch), sleep=instant,
    )
    credential = str(switch.credential.id)
    try:
        before = await exit_client.device_exit_options(TELEGRAM_ID, credential)
        assert before is not None
        target = next(row["id"] for row in before["exits"] if row["id"] != before["current_exit"]["id"])
        with pytest.raises(Conflict) as error:
            await exit_client.change_device_exit(TELEGRAM_ID, credential, target, before["revision"])
        assert error.value.reason == "stale_confirmation"
        assert len(requests) == 2 and requests[0] == requests[1]
        actual = await exit_client.device_exit_options(TELEGRAM_ID, credential)
        assert actual is not None and actual["current_exit"]["id"] == actual["pinned_exit"]["id"] == target
    finally:
        await exit_client.aclose()
        await transport.aclose()


async def test_exit_dto_strips_unexpected_secret_fields() -> None:
    body = safe_options()
    body["config"] = "PrivateKey=TOP-SECRET"
    body["current_exit"]["host"] = "private-host"
    body["exits"][0]["ssh_password"] = "TOP-SECRET"
    exit_client = PanelClient("http://testserver", "test-key", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=body),
    ))
    try:
        result = await exit_client.device_exit_options(TELEGRAM_ID, body["credential_id"])
        assert result is not None
        assert set(result) == {
            "credential_id", "device_name", "current_exit", "pinned_exit", "mode", "revision", "exits",
        }
        assert set(result["current_exit"]) == {"id", "name"}
        assert set(result["exits"][0]) == {"id", "name", "selectable", "reason"}
        assert "TOP-SECRET" not in str(result) and "private-host" not in str(result)
    finally:
        await exit_client.aclose()
