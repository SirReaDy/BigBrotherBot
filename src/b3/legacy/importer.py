"""Import a legacy B3 database into the B3 2.0 schema.

The legacy schema and the 2.0 schema share column names, so the mapping is largely 1:1 — but the
old MyISAM tables had **no foreign keys**, so real-world databases contain dangling references
(e.g. ``penalties.admin_id`` pointing at a client id that no longer exists, or the sentinel ``0``).
Loading those into the FK-enforcing 2.0 schema would fail, so this importer:

* preserves client ids (keeping every alias/penalty reference valid),
* nulls a penalty ``admin_id`` that doesn't resolve to an imported client,
* skips penalties/aliases whose ``client_id`` is orphaned (counting them in the report),
* upserts groups (a legacy install may have customized names/levels),
* copies the ``data`` key/value table (except the schema-version stamp).

All of this preserves the domain semantics documented in DESIGN.md: ``group_bits`` bitmask, epoch
timestamps, and penalty ``type``/``inactive``/``time_expire``/``duration`` are carried verbatim.

The source is any SQLAlchemy URL (``sqlite:///old.db``, ``mysql+pymysql://user:pw@host/b3``, …).
Import is idempotent for a given source (rows are merged by primary key).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, text

from b3.storage.models import AliasRow, ClientRow, DataRow, GroupRow, IpAliasRow, PenaltyRow
from b3.storage.store import SqlAlchemyStorage

log = logging.getLogger(__name__)


@dataclass
class ImportReport:
    clients: int = 0
    groups: int = 0
    penalties: int = 0
    penalties_skipped_orphan: int = 0
    penalties_admin_nulled: int = 0
    aliases: int = 0
    aliases_skipped_orphan: int = 0
    ipaliases: int = 0
    ipaliases_skipped_orphan: int = 0
    data: int = 0

    def summary(self) -> str:
        return (
            f"clients={self.clients} groups={self.groups} "
            f"penalties={self.penalties} (skipped_orphan={self.penalties_skipped_orphan}, "
            f"admin_nulled={self.penalties_admin_nulled}) "
            f"aliases={self.aliases} (skipped={self.aliases_skipped_orphan}) "
            f"ipaliases={self.ipaliases} (skipped={self.ipaliases_skipped_orphan}) "
            f"data={self.data}"
        )


def _get(row, key, default):  # noqa: ANN001
    """Read a column from a legacy row mapping, tolerating absent columns across B3 versions."""
    return row[key] if key in row else default


def import_legacy_database(source_url: str, target: SqlAlchemyStorage) -> ImportReport:
    """Import every supported table from ``source_url`` into ``target`` (2.0 storage)."""
    target.connect()  # ensure the 2.0 schema + seed groups exist
    source = create_engine(source_url)
    report = ImportReport()
    try:
        available = set(inspect(source).get_table_names())
        with source.connect() as src, target.session() as session:
            client_ids = _import_clients(src, session, available, report)
            _import_groups(src, session, available, report)
            _import_penalties(src, session, available, client_ids, report)
            _import_aliases(src, session, available, client_ids, report)
            _import_ipaliases(src, session, available, client_ids, report)
            _import_data(src, session, available, report)
            session.commit()
    finally:
        source.dispose()
    log.info("legacy import complete: %s", report.summary())
    return report


def _import_clients(src, session, available, report) -> set[int]:  # noqa: ANN001
    ids: set[int] = set()
    if "clients" not in available:
        return ids
    for row in src.execute(text("SELECT * FROM clients")).mappings():
        cid = int(row["id"])
        session.merge(
            ClientRow(
                id=cid,
                guid=_get(row, "guid", "") or "",
                pbid=_get(row, "pbid", "") or "",
                ip=_get(row, "ip", "") or "",
                name=_get(row, "name", "") or "",
                connections=int(_get(row, "connections", 0) or 0),
                group_bits=int(_get(row, "group_bits", 0) or 0),
                mask_level=int(_get(row, "mask_level", 0) or 0),
                auto_login=int(_get(row, "auto_login", 0) or 0),
                greeting=_get(row, "greeting", "") or "",
                password=_get(row, "password", None),
                login=_get(row, "login", None),
                time_add=int(_get(row, "time_add", 0) or 0),
                time_edit=int(_get(row, "time_edit", 0) or 0),
            )
        )
        ids.add(cid)
        report.clients += 1
    return ids


def _import_groups(src, session, available, report) -> None:  # noqa: ANN001
    if "groups" not in available:
        return
    for row in src.execute(text("SELECT * FROM groups")).mappings():
        session.merge(
            GroupRow(
                id=int(row["id"]),
                name=_get(row, "name", "") or "",
                keyword=_get(row, "keyword", "") or "",
                level=int(_get(row, "level", 0) or 0),
                time_add=int(_get(row, "time_add", 0) or 0),
                time_edit=int(_get(row, "time_edit", 0) or 0),
            )
        )
        report.groups += 1


def _import_penalties(src, session, available, client_ids, report) -> None:  # noqa: ANN001
    if "penalties" not in available:
        return
    for row in src.execute(text("SELECT * FROM penalties")).mappings():
        client_id = int(row["client_id"])
        if client_id not in client_ids:
            report.penalties_skipped_orphan += 1
            continue
        raw_admin = _get(row, "admin_id", None)
        admin_id = int(raw_admin) if raw_admin not in (None, 0) else None
        if admin_id is not None and admin_id not in client_ids:
            admin_id = None
            report.penalties_admin_nulled += 1
        session.merge(
            PenaltyRow(
                id=int(row["id"]),
                type=_get(row, "type", "Ban") or "Ban",
                client_id=client_id,
                admin_id=admin_id,
                duration=int(_get(row, "duration", 0) or 0),
                inactive=int(_get(row, "inactive", 0) or 0),
                keyword=_get(row, "keyword", "") or "",
                reason=_get(row, "reason", "") or "",
                data=_get(row, "data", "") or "",
                time_add=int(_get(row, "time_add", 0) or 0),
                time_edit=int(_get(row, "time_edit", 0) or 0),
                time_expire=int(_get(row, "time_expire", -1)),
            )
        )
        report.penalties += 1


def _import_aliases(src, session, available, client_ids, report) -> None:  # noqa: ANN001
    if "aliases" not in available:
        return
    for row in src.execute(text("SELECT * FROM aliases")).mappings():
        client_id = int(row["client_id"])
        if client_id not in client_ids:
            report.aliases_skipped_orphan += 1
            continue
        session.merge(
            AliasRow(
                id=int(row["id"]),
                alias=_get(row, "alias", "") or "",
                client_id=client_id,
                num_used=int(_get(row, "num_used", 1) or 1),
                time_add=int(_get(row, "time_add", 0) or 0),
                time_edit=int(_get(row, "time_edit", 0) or 0),
            )
        )
        report.aliases += 1


def _import_ipaliases(src, session, available, client_ids, report) -> None:  # noqa: ANN001
    if "ipaliases" not in available:
        return
    for row in src.execute(text("SELECT * FROM ipaliases")).mappings():
        client_id = int(row["client_id"])
        if client_id not in client_ids:
            report.ipaliases_skipped_orphan += 1
            continue
        session.merge(
            IpAliasRow(
                id=int(row["id"]),
                ip=_get(row, "ip", "") or "",
                client_id=client_id,
                num_used=int(_get(row, "num_used", 1) or 1),
                time_add=int(_get(row, "time_add", 0) or 0),
                time_edit=int(_get(row, "time_edit", 0) or 0),
            )
        )
        report.ipaliases += 1


def _import_data(src, session, available, report) -> None:  # noqa: ANN001
    if "data" not in available:
        return
    for row in src.execute(text("SELECT * FROM data")).mappings():
        key = row["data_key"]
        if key == "schema_version":  # keep the 2.0 stamp, don't overwrite with the legacy one
            continue
        session.merge(DataRow(data_key=key, data_value=_get(row, "data_value", "") or ""))
        report.data += 1
