"""gateway protocol

Revision ID: a8c4e1f7b925
Revises: f6b2d8a41c73
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a8c4e1f7b925"
down_revision: Union[str, Sequence[str], None] = "f6b2d8a41c73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROTOCOL_TYPES_BEFORE = ("AMNEZIA_WG", "AMNEZIA_APP_AWG", "AMNEZIA_APP_AWG2")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE protocoltype ADD VALUE IF NOT EXISTS 'AMNEZIA_GATEWAY'")


def downgrade() -> None:
    # Значение из enum Postgres не убрать, пока есть строки с ним: протоколы шлюза удаляются (каскадом уходят их
    # доступы и credential'ы), затем тип пересоздаётся без нового значения.
    op.execute("DELETE FROM protocols WHERE type = 'AMNEZIA_GATEWAY'")
    op.execute("ALTER TYPE protocoltype RENAME TO protocoltype_old")
    values = ", ".join(f"'{value}'" for value in _PROTOCOL_TYPES_BEFORE)
    op.execute(f"CREATE TYPE protocoltype AS ENUM ({values})")
    op.execute("ALTER TABLE protocols ALTER COLUMN type TYPE protocoltype USING type::text::protocoltype")
    op.execute("DROP TYPE protocoltype_old")
