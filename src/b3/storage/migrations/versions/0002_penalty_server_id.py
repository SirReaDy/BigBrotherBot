"""record which server issued a penalty

Several bots can share one database — the classic bot supported it and this one keeps the option,
because a shared database is what makes a ban apply across an operator's whole set of servers. What
neither had was any record of *where* a penalty came from, so two servers' histories were
indistinguishable and a multi-server dashboard had nothing to attribute a ban to.

`penalties.server_id` fills that in, from the new `bot.server_id` config value. Existing rows get
"" — "not stated" — which is also what an imported legacy database reads as, and what a single-server
install leaves it as forever. It is attribution only: no query filters on it by default, so a shared
database keeps enforcing every ban on every server exactly as before.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so this works on SQLite, which cannot ALTER a column in place.
    with op.batch_alter_table("penalties", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("server_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.create_index(batch_op.f("ix_penalties_server_id"), ["server_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("penalties", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_penalties_server_id"))
        batch_op.drop_column("server_id")
