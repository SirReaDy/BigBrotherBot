"""initial schema

The v2.0 baseline schema. Column names mirror the legacy B3 schema (so a legacy database can be
imported 1:1 by b3.legacy.importer), with two deliberate modernizations over the old MyISAM layout:
real FOREIGN KEY constraints and IPv6-width (45) IP columns.

Revision ID: 0001
Revises:
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guid", sa.String(length=36), nullable=False),
        sa.Column("pbid", sa.String(length=32), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("connections", sa.Integer(), nullable=False),
        sa.Column("group_bits", sa.Integer(), nullable=False),
        sa.Column("mask_level", sa.Integer(), nullable=False),
        sa.Column("auto_login", sa.Integer(), nullable=False),
        sa.Column("greeting", sa.String(length=128), nullable=False),
        sa.Column("password", sa.String(length=32), nullable=True),
        sa.Column("login", sa.String(length=255), nullable=True),
        sa.Column("time_add", sa.Integer(), nullable=False),
        sa.Column("time_edit", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_clients_group_bits"), ["group_bits"], unique=False)
        batch_op.create_index(batch_op.f("ix_clients_guid"), ["guid"], unique=True)
        batch_op.create_index(batch_op.f("ix_clients_name"), ["name"], unique=False)

    op.create_table(
        "data",
        sa.Column("data_key", sa.String(length=255), nullable=False),
        sa.Column("data_value", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("data_key"),
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("keyword", sa.String(length=32), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("time_add", sa.Integer(), nullable=False),
        sa.Column("time_edit", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("keyword"),
    )

    op.create_table(
        "aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alias", sa.String(length=32), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("num_used", sa.Integer(), nullable=False),
        sa.Column("time_add", sa.Integer(), nullable=False),
        sa.Column("time_edit", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias", "client_id", name="uq_alias_client"),
    )
    with op.batch_alter_table("aliases", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_aliases_client_id"), ["client_id"], unique=False)

    op.create_table(
        "ipaliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("num_used", sa.Integer(), nullable=False),
        sa.Column("time_add", sa.Integer(), nullable=False),
        sa.Column("time_edit", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip", "client_id", name="uq_ipalias_client"),
    )
    with op.batch_alter_table("ipaliases", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ipaliases_client_id"), ["client_id"], unique=False)

    op.create_table(
        "penalties",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=False),
        sa.Column("inactive", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("data", sa.String(length=255), nullable=False),
        sa.Column("time_add", sa.Integer(), nullable=False),
        sa.Column("time_edit", sa.Integer(), nullable=False),
        sa.Column("time_expire", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("penalties", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_penalties_admin_id"), ["admin_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_penalties_client_id"), ["client_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_penalties_inactive"), ["inactive"], unique=False)
        batch_op.create_index(batch_op.f("ix_penalties_keyword"), ["keyword"], unique=False)
        batch_op.create_index(batch_op.f("ix_penalties_time_add"), ["time_add"], unique=False)
        batch_op.create_index(batch_op.f("ix_penalties_time_expire"), ["time_expire"], unique=False)
        batch_op.create_index(batch_op.f("ix_penalties_type"), ["type"], unique=False)


def downgrade() -> None:
    op.drop_table("penalties")
    op.drop_table("ipaliases")
    op.drop_table("aliases")
    op.drop_table("groups")
    op.drop_table("data")
    op.drop_table("clients")
