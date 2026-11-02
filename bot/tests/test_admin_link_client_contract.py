"""The Bot API and its typed client expose the same admin_name field."""

from support import ADMIN_CODE, World
from tollwire_bot.panel import AdminLinkResult


async def test_admin_link_returns_typed_admin_name_without_logging_it(world: World) -> None:
    name = "Admin name for the binding response"
    world.panel.add_admin_code(ADMIN_CODE, name)
    result = await world.client.admin_link(ADMIN_CODE, 5550001000, None)
    assert isinstance(result, AdminLinkResult)
    assert result.admin_name == name
    assert name not in repr(result)
