"""Alarm rows from `Alarm*` DataChanges, keyed by (machine, AlarmId) (PLAN section 9).

Identity is the AlarmId, never the code or the order of arrival. The server groups one publish by monitored item, so the values
of one alarm arrive scattered and a clear of an old warning can be delivered after the AlarmId of the fault that replaced it.
Each value is therefore attributed to the AlarmId that was current at its SourceTimestamp. A warning's clear cannot close a fault,
because the clear belongs to the warning's id.

The address space has no acknowledged flag: `acked` is set by `mark_acked` once `AckAlarm` has returned ok.
"""
from __future__ import annotations

import inspect
import logging
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .opcua_client import DataChange

logger = logging.getLogger(__name__)

SEVERITY_WARNING = 500
SEVERITY_FAULT = 900

# Values of one fast block may carry slightly different timestamps, in any order. Separate alarms are at least a plant tick apart.
_BLOCK_SKEW_S = 0.001
_ROW_LIMIT = 2000
_ALARM_FIELDS = frozenset({"AlarmActive", "AlarmCode", "AlarmText", "AlarmSeverity"})


@dataclass(slots=True)
class _Row:
    machine: str
    alarm_id: int
    source_ts: float
    received_ts: float
    t: float
    latency_ms: float | None
    code: str = ""
    text: str = ""
    severity: int = 0
    active: bool = True
    acked: bool = False
    cleared_ts: float | None = None
    cleared_t: float | None = None
    status_linked: bool = False

    @property
    def row_id(self) -> str:
        return f"{self.machine}:{self.alarm_id}"

    def to_dict(self) -> dict:
        return {
            "id": self.row_id,
            "machine": self.machine,
            "alarm_id": self.alarm_id,
            "code": self.code,
            "text": self.text,
            "severity": self.severity,
            "t": round(self.t, 3),
            "source_ts": self.source_ts,
            "detected_wall_ts": self.received_ts,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "active": self.active,
            "acked": self.acked,
            "cleared_t": None if self.cleared_t is None else round(self.cleared_t, 3),
            "cleared_ts": self.cleared_ts,
        }


@dataclass(slots=True)
class _MachineTimeline:
    current_id: int = 0
    history: deque = field(default_factory=lambda: deque(maxlen=32))  # (source_ts, alarm_id), oldest first
    code: str = ""
    text: str = ""
    severity: int = 0

    def owner(self, source_ts: float) -> int:
        """The AlarmId current at `source_ts`, allowing for skew inside one fast block. 0 means no alarm."""
        owner = 0
        for ts, alarm_id in self.history:
            if ts <= source_ts + _BLOCK_SKEW_S:
                owner = alarm_id
            else:
                break
        return owner


class AlarmTracker:
    """Keeps the alarm rows of one run.

    Normally fed through `Shadow.attach(client, alarms=tracker)`, which also resets it when the run changes.

    `sim_clock(wall_ts)` converts a wall-clock time to sim minutes; pass `Shadow.sim_time_at`. `on_change(row: dict)` runs once for every
    row a batch created or changed, for the WebSocket `alarm` message and the database mirror; it may be sync or async. Row JSON:

        {"id":"PR2:3","machine":"PR2","alarm_id":3,"code":"P-204","text":"...","severity":900,"t":1230.0,"source_ts":1793000000.05,
         "detected_wall_ts":1793000000.11,"latency_ms":62.0,"active":true,"acked":false,"cleared_t":null,"cleared_ts":null}

    For a fault, `source_ts`, `detected_wall_ts`, `latency_ms` and `t` come from the `Status`=FAULT DataValue, so the alarm's timestamp is the
    one the failure chain measures. `latency_ms` is None for a row first seen in a baseline, whose age says nothing about the transport.
    """

    def __init__(self, sim_clock: Callable[[float], float] | None = None,
                 on_change: Callable[[dict], Any] | None = None, *, row_limit: int = _ROW_LIMIT) -> None:
        self._sim_clock = sim_clock or (lambda _wall_ts: 0.0)
        self.on_change = on_change
        self._row_limit = row_limit
        self._rows: dict[str, _Row] = {}
        self._timelines: dict[str, _MachineTimeline] = {}

    async def apply_batch(self, changes: Iterable["DataChange"]) -> list[dict]:
        """Apply one batch of DataChanges, ignoring everything but `Alarm*` and `Status`. Returns the rows it changed."""
        by_machine: dict[str, list[DataChange]] = defaultdict(list)
        for change in changes:
            if change.machine and (change.name == "AlarmId" or change.name == "Status" or change.name in _ALARM_FIELDS):
                by_machine[change.machine].append(change)
        changed: dict[str, _Row] = {}
        for machine, records in by_machine.items():
            self._apply_machine(machine, records, changed)
        rows = [row.to_dict() for row in changed.values()]
        self._prune()
        if self.on_change is not None:
            for row in rows:
                try:
                    result = self.on_change(row)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("alarm on_change failed")
        return rows

    def _apply_machine(self, machine: str, records: list["DataChange"], changed: dict[str, _Row]) -> None:
        timeline = self._timelines.setdefault(machine, _MachineTimeline())
        # Stable sort: per-node order survives, and timestamps restore the order across nodes that the publish lost.
        records = sorted(records, key=lambda r: r.source_ts)

        for record in records:
            if record.name == "AlarmId":
                self._on_alarm_id(machine, timeline, record, changed)
        for record in records:
            if record.name in _ALARM_FIELDS:
                self._on_field(machine, timeline, record, changed)
        self._inherit_unchanged_fields(machine, timeline)
        for record in records:
            if record.name == "Status" and record.value == "FAULT" and not record.baseline:
                self._link_status(machine, timeline, record, changed)

    def _on_alarm_id(self, machine: str, timeline: _MachineTimeline, record: "DataChange", changed: dict[str, _Row]) -> None:
        new_id = int(record.value or 0)
        if new_id == timeline.current_id:
            return
        previous = self._rows.get(f"{machine}:{timeline.current_id}")
        if previous is not None and previous.active:
            # A fault replaces a warning without touching AlarmActive, because the value stays true.
            self._close(previous, record.source_ts, changed)
        timeline.current_id = new_id
        timeline.history.append((record.source_ts, new_id))
        if new_id == 0:
            return
        row_id = f"{machine}:{new_id}"
        if row_id not in self._rows:
            self._rows[row_id] = _Row(
                machine=machine,
                alarm_id=new_id,
                source_ts=record.source_ts,
                received_ts=record.received_ts,
                t=self._sim_clock(record.source_ts),
                latency_ms=None if record.baseline else record.latency_ms,
            )
            changed[row_id] = self._rows[row_id]

    def _on_field(self, machine: str, timeline: _MachineTimeline, record: "DataChange", changed: dict[str, _Row]) -> None:
        row = self._rows.get(f"{machine}:{timeline.owner(record.source_ts)}")
        if row is None:
            return
        name, value = record.name, record.value
        if name == "AlarmActive":
            if not value:
                self._close(row, record.source_ts, changed)
            elif not row.active and row.cleared_ts is not None and record.source_ts > row.cleared_ts:
                row.active = True
                row.cleared_ts = None
                row.cleared_t = None
                changed[row.row_id] = row
            return
        # A cleared alarm may blank these variables. The row keeps what the alarm said while it was active.
        if name == "AlarmCode" and value:
            row.code = timeline.code = value
        elif name == "AlarmText" and value:
            row.text = timeline.text = value
        elif name == "AlarmSeverity" and value:
            row.severity = timeline.severity = int(value)
        else:
            return
        changed[row.row_id] = row

    def _inherit_unchanged_fields(self, machine: str, timeline: _MachineTimeline) -> None:
        # The server reports only changes: a second alarm with the same code as the last one sends no AlarmCode at all.
        row = self._rows.get(f"{machine}:{timeline.current_id}")
        if row is None:
            return
        row.code = row.code or timeline.code
        row.text = row.text or timeline.text
        row.severity = row.severity or timeline.severity

    def _link_status(self, machine: str, timeline: _MachineTimeline, record: "DataChange", changed: dict[str, _Row]) -> None:
        row = self._rows.get(f"{machine}:{timeline.owner(record.source_ts)}")
        if row is None or row.status_linked or row.severity == SEVERITY_WARNING:
            return
        row.status_linked = True
        row.source_ts = record.source_ts
        row.received_ts = record.received_ts
        row.latency_ms = record.latency_ms
        row.t = self._sim_clock(record.source_ts)
        changed[row.row_id] = row

    def _close(self, row: _Row, source_ts: float, changed: dict[str, _Row]) -> None:
        if row.cleared_ts is not None and row.cleared_ts <= source_ts:
            return
        row.active = False
        row.cleared_ts = source_ts
        row.cleared_t = self._sim_clock(source_ts)
        changed[row.row_id] = row

    def mark_acked(self, row_id: str) -> dict | None:
        """Mark a row acknowledged after `AckAlarm` returned ok. Returns the row, or None when it is unknown."""
        row = self._rows.get(row_id)
        if row is None:
            return None
        row.acked = True
        return row.to_dict()

    def get(self, row_id: str) -> dict | None:
        """One row by id (`"PR2:3"`), or None."""
        row = self._rows.get(row_id)
        return row.to_dict() if row is not None else None

    def active(self) -> list[dict]:
        """Rows still active, newest first, for `alarms_active` in `/api/state`."""
        rows = [r for r in self._rows.values() if r.active]
        rows.sort(key=lambda r: r.source_ts, reverse=True)
        return [r.to_dict() for r in rows]

    def rows(self, limit: int = 200) -> list[dict]:
        """All rows, newest first, for `GET /api/alarms`."""
        rows = sorted(self._rows.values(), key=lambda r: r.source_ts, reverse=True)
        return [r.to_dict() for r in rows[:limit]]

    def reset(self) -> None:
        """Forget everything, for a new plant run."""
        self._rows.clear()
        self._timelines.clear()

    def _prune(self) -> None:
        if len(self._rows) <= self._row_limit:
            return
        cleared = sorted((r for r in self._rows.values() if not r.active), key=lambda r: r.source_ts)
        for row in cleared[:len(self._rows) - self._row_limit]:
            del self._rows[row.row_id]


__all__ = ["AlarmTracker", "SEVERITY_FAULT", "SEVERITY_WARNING"]
