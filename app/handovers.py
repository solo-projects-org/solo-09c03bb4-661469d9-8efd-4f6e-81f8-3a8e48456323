"""Transfer-handover sheets and the cross-carrier waybill mapping.

A *valid* handover sheet links two endpoints ``(carrier, waybill_no)``.  All
endpoints reachable from one another along valid sheets belong to the same
*ticket*.  A ticket may contain at most one waybill number per carrier.

``waybill_mapping`` is the materialised view of those tickets.  It only
contains endpoints that still occur on at least one valid sheet: revoking the
last sheet touching a number removes it from the mapping.  Sheets and mapping
are written in one SQLite transaction, and only the tickets reachable from the
sheets changed by a batch are rebuilt.

Concurrent batches are serialised with ``BEGIN IMMEDIATE``: a batch reads the
database only after it has obtained the write lock, so the outcome of two
concurrent batches follows their actual commit order.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_DB_PATH = "data/handovers.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS handover_sheets (
    settlement_month TEXT NOT NULL,
    handover_no      TEXT NOT NULL,
    carrier_a        TEXT NOT NULL,
    waybill_no_a     TEXT NOT NULL,
    carrier_b        TEXT NOT NULL,
    waybill_no_b     TEXT NOT NULL,
    PRIMARY KEY (settlement_month, handover_no)
);

CREATE TABLE IF NOT EXISTS waybill_mapping (
    settlement_month TEXT NOT NULL,
    ticket_id        TEXT NOT NULL,
    carrier          TEXT NOT NULL,
    waybill_no       TEXT NOT NULL,
    PRIMARY KEY (settlement_month, carrier, waybill_no)
);

CREATE INDEX IF NOT EXISTS ix_waybill_mapping_ticket
    ON waybill_mapping (settlement_month, ticket_id);
"""

@dataclass(frozen=True, order=True)
class Endpoint:
    """One carrier's spelling of one parcel's waybill number."""

    carrier: str
    waybill_no: str


@dataclass(frozen=True)
class Sheet:
    handover_no: str
    carrier_a: str
    waybill_no_a: str
    carrier_b: str
    waybill_no_b: str

    @classmethod
    def from_row(cls, row: tuple) -> "Sheet":
        return cls(*row)

    def endpoints(self) -> tuple[Endpoint, Endpoint]:
        return (
            Endpoint(self.carrier_a, self.waybill_no_a),
            Endpoint(self.carrier_b, self.waybill_no_b),
        )

    def as_row(self, settlement_month: str) -> tuple:
        return (
            settlement_month,
            self.handover_no,
            self.carrier_a,
            self.waybill_no_a,
            self.carrier_b,
            self.waybill_no_b,
        )


@dataclass(frozen=True)
class Change:
    """A batch operation: ``sheet`` is None for a revocation."""

    handover_no: str
    sheet: Optional[Sheet]


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    endpoints: tuple[Endpoint, ...]


@dataclass(frozen=True)
class TicketConflict:
    """One carrier carrying more than one waybill number inside one ticket."""

    carrier: str
    waybill_nos: tuple[str, ...]
    handover_nos: tuple[str, ...]


@dataclass(frozen=True)
class BatchResult:
    settlement_month: str
    sheets_upserted: int
    sheets_revoked: int
    rebuilt_tickets: tuple[Ticket, ...]


class HandoverConflict(Exception):
    """A ticket ended up with more than one waybill number for one carrier."""

    def __init__(self, conflicts: Iterable[TicketConflict]):
        self.conflicts = tuple(conflicts)
        rendered = "; ".join(
            f"{conflict.carrier}: {', '.join(conflict.waybill_nos)}"
            for conflict in self.conflicts
        )
        super().__init__(f"同票同承运商存在多个运单号: {rendered}")


def ticket_id_for(settlement_month: str, endpoints: Iterable[Endpoint]) -> str:
    """Deterministic id of one ticket (stable when its endpoint set is stable)."""
    payload = json.dumps(
        [settlement_month, [[e.carrier, e.waybill_no] for e in sorted(endpoints)]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _components(
    adjacency: dict[Endpoint, set[Endpoint]], seeds: Optional[Iterable[Endpoint]] = None
) -> list[tuple[Endpoint, ...]]:
    """Connected components of ``adjacency``.

    With ``seeds`` only the components that contain a seed are returned; this
    is how a batch limits its rebuild to the tickets it actually touched.
    """
    seen: set[Endpoint] = set()
    components: list[tuple[Endpoint, ...]] = []
    starts = sorted(seeds) if seeds is not None else sorted(adjacency)
    for seed in starts:
        if seed in seen or seed not in adjacency:
            # endpoints without any valid sheet are not part of the mapping
            continue
        stack = [seed]
        seen.add(seed)
        members: list[Endpoint] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbour in adjacency.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        components.append(tuple(sorted(members)))
    components.sort()
    return components


def _ticket_conflicts(
    components: Iterable[tuple[Endpoint, ...]],
    sheets: Iterable[Sheet],
) -> list[TicketConflict]:
    """Every carrier with several waybill numbers in one ticket component."""
    sheets = list(sheets)
    conflicts: list[TicketConflict] = []
    for component in components:
        members = set(component)
        by_carrier: dict[str, set[str]] = defaultdict(set)
        for endpoint in component:
            by_carrier[endpoint.carrier].add(endpoint.waybill_no)
        numbers_by_carrier = {
            carrier: tuple(sorted(numbers))
            for carrier, numbers in by_carrier.items()
        }
        if not any(len(numbers) > 1 for numbers in numbers_by_carrier.values()):
            continue
        handover_nos = tuple(sorted(
            sheet.handover_no for sheet in sheets
            if all(endpoint in members for endpoint in sheet.endpoints())
        ))
        for carrier in sorted(numbers_by_carrier):
            numbers = numbers_by_carrier[carrier]
            if len(numbers) > 1:
                conflicts.append(TicketConflict(
                    carrier=carrier,
                    waybill_nos=numbers,
                    handover_nos=handover_nos,
                ))
    return conflicts


class HandoverStore:
    """SQLite-backed handover sheets and read-only waybill mapping."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    # ------------------------------------------------------------- connections
    @contextmanager
    def _connection(self, write: bool = False):
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            self._ensure_schema(conn)
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
        except BaseException:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._schema_ready = True

    # ----------------------------------------------------------------- reading
    def lookup(
        self, settlement_month: str, carrier: str, waybill_no: str
    ) -> Optional[Ticket]:
        """Read the ticket containing an endpoint, from the mapping only."""
        with self._connection() as conn:
            # One statement, so the lookup cannot straddle a concurrent commit;
            # only the mapping table is read, never the sheets.
            rows = conn.execute(
                "SELECT m.ticket_id, m.carrier, m.waybill_no"
                " FROM waybill_mapping m"
                " JOIN waybill_mapping t"
                "   ON t.settlement_month = m.settlement_month"
                "  AND t.ticket_id = m.ticket_id"
                " WHERE t.settlement_month = ? AND t.carrier = ?"
                "   AND t.waybill_no = ?"
                " ORDER BY m.carrier, m.waybill_no",
                (settlement_month, carrier, waybill_no),
            ).fetchall()
            if not rows:
                return None
            return Ticket(
                ticket_id=rows[0][0],
                endpoints=tuple(Endpoint(carrier, waybill_no)
                                for _, carrier, waybill_no in rows),
            )

    # ----------------------------------------------------------------- writing
    def apply_batch(
        self, settlement_month: str, changes: Iterable[Change]
    ) -> BatchResult:
        """Apply all changes atomically; conflicts roll the whole batch back."""
        changes = list(changes)
        touched = {change.handover_no for change in changes}
        with self._connection(write=True) as conn:
            rows = conn.execute(
                "SELECT handover_no, carrier_a, waybill_no_a,"
                " carrier_b, waybill_no_b"
                " FROM handover_sheets WHERE settlement_month = ?",
                (settlement_month,),
            ).fetchall()
            before = {row[0]: Sheet.from_row(row) for row in rows}
            after = dict(before)
            for change in changes:
                if change.sheet is None:
                    after.pop(change.handover_no, None)
                else:
                    after[change.handover_no] = change.sheet

            changed = [
                handover_no for handover_no in sorted(touched)
                if before.get(handover_no) != after.get(handover_no)
            ]
            if not changed:
                conn.execute("COMMIT")
                return BatchResult(settlement_month, 0, 0, ())

            # 1. Collect the endpoints whose ticket membership may have
            #    changed: both the old and the new endpoints of every changed
            #    sheet.  Any conflict created by the batch necessarily lives in
            #    a component reachable from one of them.
            affected: set[Endpoint] = set()
            for handover_no in changed:
                old_sheet = before.get(handover_no)
                new_sheet = after.get(handover_no)
                if old_sheet is not None:
                    affected.update(old_sheet.endpoints())
                if new_sheet is not None:
                    affected.update(new_sheet.endpoints())

            adjacency: dict[Endpoint, set[Endpoint]] = defaultdict(set)
            for sheet in after.values():
                left, right = sheet.endpoints()
                adjacency[left].add(right)
                adjacency[right].add(left)

            # Validate the full post-batch result: a ticket is a connected
            # component of valid sheets, and every ticket must carry at most
            # one waybill number per carrier.  A conflict anywhere rejects the
            # whole batch, so nothing is written.
            components = _components(adjacency)
            conflicts = _ticket_conflicts(components, after.values())
            if conflicts:
                raise HandoverConflict(conflicts)
            # ...but only tickets that actually changed are rebuilt.
            affected_components = [
                component for component in components
                if any(endpoint in affected for endpoint in component)
            ]

            # 2. Persist only the changed sheets.
            conn.executemany(
                "DELETE FROM handover_sheets"
                " WHERE settlement_month = ? AND handover_no = ?",
                [(settlement_month, handover_no) for handover_no in changed],
            )
            conn.executemany(
                "INSERT INTO handover_sheets"
                " (settlement_month, handover_no, carrier_a, waybill_no_a,"
                "  carrier_b, waybill_no_b) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    after[handover_no].as_row(settlement_month)
                    for handover_no in changed
                    if handover_no in after
                ],
            )

            # 3. Rebuild only the tickets reachable from those sheets.  The old
            #    ticket ids come from the mapping rows of the affected endpoints,
            #    so a split removes stale rows while untouched tickets stay put.
            old_ticket_ids: set[str] = set()
            endpoints = sorted(affected)
            for start in range(0, len(endpoints), 200):
                chunk = endpoints[start:start + 200]
                clause = " OR ".join(
                    ["(carrier = ? AND waybill_no = ?)"] * len(chunk))
                params: list[str] = [settlement_month]
                for endpoint in chunk:
                    params.extend([endpoint.carrier, endpoint.waybill_no])
                old_ticket_ids.update(
                    row[0] for row in conn.execute(
                        "SELECT DISTINCT ticket_id FROM waybill_mapping"
                        f" WHERE settlement_month = ? AND ({clause})",
                        params,
                    )
                )

            ticket_ids = sorted(old_ticket_ids)
            for start in range(0, len(ticket_ids), 200):
                chunk = ticket_ids[start:start + 200]
                placeholders = ", ".join(["?"] * len(chunk))
                conn.execute(
                    "DELETE FROM waybill_mapping"
                    f" WHERE settlement_month = ? AND ticket_id IN ({placeholders})",
                    [settlement_month, *chunk],
                )

            mapping_rows: list[tuple] = []
            rebuilt: list[Ticket] = []
            for component in affected_components:
                new_ticket_id = ticket_id_for(settlement_month, component)
                rebuilt.append(Ticket(new_ticket_id, component))
                mapping_rows.extend(
                    (settlement_month, new_ticket_id, endpoint.carrier,
                     endpoint.waybill_no)
                    for endpoint in component
                )
            conn.executemany(
                "INSERT INTO waybill_mapping"
                " (settlement_month, ticket_id, carrier, waybill_no)"
                " VALUES (?, ?, ?, ?)",
                mapping_rows,
            )

            conn.execute("COMMIT")
            upserted = sum(
                1 for handover_no in changed
                if handover_no in after and before.get(handover_no) != after[handover_no]
            )
            revoked = sum(
                1 for handover_no in changed
                if handover_no in before and handover_no not in after
            )
            return BatchResult(
                settlement_month=settlement_month,
                sheets_upserted=upserted,
                sheets_revoked=revoked,
                rebuilt_tickets=tuple(rebuilt),
            )


_store_lock = threading.Lock()
_store: Optional[HandoverStore] = None


def get_store() -> HandoverStore:
    """Process-wide store; the database path comes from ``HANDOVERS_DB``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = HandoverStore(
                os.environ.get("HANDOVERS_DB") or DEFAULT_DB_PATH)
        return _store


def reset_store() -> None:
    """Drop the cached store (used by tests and after configuration changes)."""
    global _store
    with _store_lock:
        _store = None
