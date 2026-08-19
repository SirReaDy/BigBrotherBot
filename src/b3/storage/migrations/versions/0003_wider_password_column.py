"""room for a password hash somebody cannot reverse

`clients.password` was `varchar(32)`, which is not a length chosen for passwords: it is the width of an
MD5 hex digest, because the classic bot hashed with bare MD5 and stored the result. An unsalted MD5 is
not a defensible way to keep a credential — a rainbow table answers it — so `login` stores a PBKDF2
hash with a per-password salt, and that does not fit in 32 characters.

255 is the new width, which holds the self-describing form (`pbkdf2_sha256$iterations$salt$hash`) with
room for a stronger one later. Nothing is rewritten by this migration: an imported classic database
keeps its MD5 digests, they still authenticate, and each one is replaced with a modern hash the first
time its owner logs in successfully — see `b3.plugins.login`.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so this works on SQLite, which cannot ALTER a column in place.
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.alter_column(
            "password",
            existing_type=sa.String(length=32),
            type_=sa.String(length=255),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Narrowing again truncates any modern hash to 32 characters, which silently destroys the
    # credential rather than restoring the old one. Refused outright: a downgrade that leaves accounts
    # unable to log in and nothing in the log to say why is worse than no downgrade.
    raise RuntimeError(
        "0003 cannot be reversed: narrowing clients.password to 32 characters would truncate every "
        "modern password hash. Restore a backup taken before the upgrade instead."
    )
