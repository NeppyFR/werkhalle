"""The digital shadow: a live mirror of the plant, written only by OPC UA DataChanges.

The shadow never reads a snapshot and never marks a machine as faulted on request. A machine shows FAULT here because a `Status`
DataChange said so. Fault detection runs at the end of a batch: the server groups a publish by monitored item, so `Status` can be
delivered ahead of the `Alarm*` values written before it, and only at the end of the batch is the record complete.
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..domain import catalog

if TYPE_CHECKING:
    from .alarms import AlarmTracker
    from .opcua_client import DataChange, PlantClient

logger = logging.getLogger(__name__)

TREND_SAMPLES = 120
TREND_INTERVAL_S = 1.0
# Beyond this the plant's clock is no longer running or no longer heard, so the sim time stops extrapolating.
_MAX_EXTRAPOLATION_S = 2.0

_COUNTER_VARS = {
    "run": "RunMinutes",
    "setup": "SetupMinutes",
    "fault": "FaultMinutes",
    "idle": "IdleMinutes",
    "ideal": "IdealMinutes",
    "total": "TotalCount",
    "good": "GoodCount",
    "scrap": "ScrapCount",
    "fault_count": "FaultCount",
}


@dataclass(frozen=True, slots=True)
class FaultEvent:
    """A fault the shadow detected. `latency_ms` runs from the SourceTimestamp of the triggering DataValue to its local receipt.

    `baseline` is True when it was found by comparing FaultCount after a reconnect: the fault happened while the MES was away, so the
    latency says nothing about the transport. `status` is the machine status at the end of the batch; a RUN, FAULT, RUN burst gives
    an event with `status` RUN. Alarm values are those at the end of the batch.
    """

    machine: str
    source_ts: float
    received_ts: float
    latency_ms: float
    t: float
    status: str
    alarm_id: int
    code: str
    text: str
    severity: int
    est_repair_min: float
    fault_count: int
    baseline: bool


class _Machine:
    """Latest values of one machine, by OPC UA variable name, with what cannot be read from a single value."""

    __slots__ = ("id", "values", "status_since", "temperature", "power", "operation", "fired_count", "unmatched_count_fires")

    def __init__(self, machine_id: str) -> None:
        self.id = machine_id
        self.values: dict[str, Any] = {}
        self.status_since: float | None = None
        self.temperature: deque[int] = deque(maxlen=TREND_SAMPLES)
        self.power: deque[int] = deque(maxlen=TREND_SAMPLES)
        self.operation: dict | None = None
        self.fired_count: int | None = None
        self.unmatched_count_fires = 0


def _number(value: Any, digits: int = 1) -> float:
    return round(float(value), digits) if value is not None else 0.0


def _parse_operation(raw: Any, status: str) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("CurrentOperation is not JSON: %r", raw)
        return None
    return {
        "op_ids": [], "kind": None, "lot_ids": [], "order_ids": [], "product_id": None, "phase": status, "batch_id": None,
        **parsed,
    }


class Shadow:
    """Live mirror of the plant, fed by `apply_batch`.

    `on_fault(event: FaultEvent)` may be sync or async and runs inside the OPC UA consumer: keep it short and start long work in a
    task. It fires once per fault: on a live `Status` change to FAULT, or when `FaultCount` rose without one, which is how faults
    during a disconnect are found. The first notifications after a (re)subscribe are a baseline and fire nothing unless FaultCount
    rose against the value known before.
    """

    def __init__(self, on_fault: Callable[[FaultEvent], Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.on_fault = on_fault
        self._clock = clock
        self._machines: dict[str, _Machine] = {}
        self._plant: dict[str, Any] = {}
        self._material: dict = {}
        self._sim_t: float | None = None
        self._sim_ts = 0.0
        self._last_trend_ts = 0.0
        self._alarms: AlarmTracker | None = None
        self.connected = False
        self.stale = False

    def attach(self, client: "PlantClient", alarms: "AlarmTracker | None" = None) -> None:
        """Feed this shadow from a client: data, connection state, heartbeat state, and a reset when the plant's run changes.

        With `alarms`, the tracker is fed from here too, between applying the values and firing fault callbacks. So its sim clock
        (`Shadow.sim_time_at`) already knows this batch's SimTimeMin, and the alarm row exists when `on_fault` runs.
        """
        self._alarms = alarms
        client.add_batch_callback(self.apply_batch)
        client.add_connection_callback(self._on_connection)
        client.add_stale_callback(self._on_stale)
        client.add_run_changed_callback(lambda _old, _new: self.reset())

    def _on_connection(self, connected: bool, _reason: str) -> None:
        self.connected = connected

    def _on_stale(self, stale: bool) -> None:
        self.stale = stale

    def reset(self) -> None:
        """Forget everything, for a new plant run. Fault counts start over, so the next run's history fires nothing."""
        self._machines.clear()
        self._plant.clear()
        self._material = {}
        self._sim_t = None
        self._sim_ts = 0.0
        self._last_trend_ts = 0.0
        if self._alarms is not None:
            self._alarms.reset()

    # --- feeding ---

    async def apply_batch(self, changes: Iterable["DataChange"]) -> None:
        """Apply one batch of DataChanges in delivery order, then fire fault callbacks and take a trend sample if one is due."""
        changes = list(changes)
        # Plant values first, so a status_since in this batch is computed from this batch's SimTimeMin.
        for change in changes:
            if change.machine is None:
                self._apply_plant(change)
        status_faults: dict[str, list[DataChange]] = {}
        fault_counts: dict[str, DataChange] = {}
        statuses: dict[str, DataChange] = {}
        for change in changes:
            if change.machine is None:
                continue
            machine = self._machine(change.machine)
            if change.name == "Status":
                self._apply_status(machine, change, status_faults)
                statuses[machine.id] = change
                continue
            machine.values[change.name] = change.value
            if change.name == "FaultCount":
                fault_counts[machine.id] = change
            elif change.name == "CurrentOperation":
                machine.operation = _parse_operation(change.value, machine.values.get("Status", "IDLE"))
        if self._alarms is not None:
            await self._alarms.apply_batch(changes)
        for machine_id in {*status_faults, *fault_counts}:
            await self._fire_faults(self._machines[machine_id], status_faults.get(machine_id, []),
                                    fault_counts.get(machine_id), statuses.get(machine_id))
        self.sample_trends()

    def _machine(self, machine_id: str) -> _Machine:
        machine = self._machines.get(machine_id)
        if machine is None:
            machine = self._machines[machine_id] = _Machine(machine_id)
        return machine

    def _apply_plant(self, change: "DataChange") -> None:
        self._plant[change.name] = change.value
        if change.name == "SimTimeMin":
            self._sim_t = float(change.value)
            self._sim_ts = change.source_ts
        elif change.name == "Material":
            try:
                self._material = json.loads(change.value) if change.value else {}
            except ValueError:
                logger.warning("Material is not JSON")

    def _apply_status(self, machine: _Machine, change: "DataChange", status_faults: dict[str, list["DataChange"]]) -> None:
        previous = machine.values.get("Status")
        machine.values["Status"] = change.value
        if change.value != previous or machine.status_since is None:
            machine.status_since = self.sim_time_at(change.source_ts)
        if machine.operation is not None and change.value in ("SETUP", "RUN", "FAULT"):
            machine.operation["phase"] = change.value
        if change.value == "FAULT" and previous != "FAULT" and not change.baseline:
            status_faults.setdefault(machine.id, []).append(change)

    async def _fire_faults(self, machine: _Machine, status_faults: list["DataChange"], count_change: "DataChange | None",
                           last_status: "DataChange | None") -> None:
        count = machine.values.get("FaultCount")
        count = int(count) if count is not None else None
        if machine.fired_count is None and count is not None:
            # The first FaultCount of a run is history that happened before we were here, except for a fault in this very batch.
            machine.fired_count = max(count - len(status_faults), 0)

        for change in status_faults:
            if machine.unmatched_count_fires > 0:
                # FaultCount already announced this fault in an earlier batch.
                machine.unmatched_count_fires -= 1
                continue
            machine.fired_count = (machine.fired_count or 0) + 1
            await self._emit(machine, change, baseline=False)

        if count is not None and count > (machine.fired_count or 0):
            trigger = count_change or (status_faults[-1] if status_faults else None)
            if trigger is None:
                return
            is_baseline = trigger.baseline
            if last_status is not None and last_status.value == "FAULT":
                trigger = last_status
            elif not is_baseline and machine.values.get("Status") != "FAULT":
                machine.unmatched_count_fires += 1
            machine.fired_count = count
            await self._emit(machine, trigger, baseline=is_baseline)

    async def _emit(self, machine: _Machine, change: "DataChange", *, baseline: bool) -> None:
        values = machine.values
        event = FaultEvent(
            machine=machine.id,
            source_ts=change.source_ts,
            received_ts=change.received_ts,
            latency_ms=change.latency_ms,
            t=self.sim_time_at(change.source_ts),
            status=values.get("Status", ""),
            alarm_id=int(values.get("AlarmId") or 0),
            code=values.get("AlarmCode") or "",
            text=values.get("AlarmText") or "",
            severity=int(values.get("AlarmSeverity") or 0),
            est_repair_min=float(values.get("EstimatedRepairMin") or 0.0),
            fault_count=int(values.get("FaultCount") or 0),
            baseline=baseline,
        )
        if self.on_fault is None:
            return
        try:
            result = self.on_fault(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("fault callback failed")

    def sample_trends(self, now: float | None = None) -> None:
        """Take a trend sample if a second has passed. `apply_batch` calls this, so trends stop when the data stops.

        Whole numbers on purpose: 7 machines x 2 trends x 120 samples must fit in a 12 KB state message at 4 Hz.
        """
        now = self._clock() if now is None else now
        if now - self._last_trend_ts < TREND_INTERVAL_S:
            return
        self._last_trend_ts = now
        for machine in self._machines.values():
            temperature = machine.values.get("Temperature")
            power = machine.values.get("PowerKW")
            if temperature is not None:
                machine.temperature.append(round(float(temperature)))
            if power is not None:
                machine.power.append(round(float(power)))

    # --- reading ---

    def sim_time_at(self, wall_ts: float | None = None) -> float:
        """Sim minutes at a wall-clock time, from the last SimTimeMin and the time factor. 0.0 before the first value."""
        if self._sim_t is None:
            return 0.0
        wall_ts = self._clock() if wall_ts is None else wall_ts
        elapsed = max(min(wall_ts - self._sim_ts, _MAX_EXTRAPOLATION_S), -_MAX_EXTRAPOLATION_S)
        return self._sim_t + elapsed * self.time_factor / 60.0

    @property
    def time_factor(self) -> float:
        """Current `TimeFactor`."""
        return float(self._plant.get("TimeFactor") or 0.0)

    @property
    def run_id(self) -> str | None:
        """`RunId` as mirrored."""
        return self._plant.get("RunId")

    @property
    def material(self) -> dict:
        """The parsed Material JSON (PLAN section 4), `{}` before the first value."""
        return self._material

    def sim_state(self) -> dict:
        """The `sim` object of `/api/state`, except `auto_supply`, which belongs to the runtime."""
        return {
            "t": round(self.sim_time_at(), 3),
            "factor": self.time_factor,
            "wall_ts": self._clock(),
            "random_failures": bool(self._plant.get("RandomFailures", False)),
        }

    def plant_totals(self) -> dict:
        """`{"power_kw","energy_kwh","snapshot_calls","event_seq","heartbeat"}` from the plant variables."""
        return {
            "power_kw": _number(self._plant.get("TotalPowerKW")),
            "energy_kwh": _number(self._plant.get("TotalEnergyKWh")),
            "snapshot_calls": int(self._plant.get("SnapshotCalls") or 0),
            "event_seq": int(self._plant.get("EventSeq") or 0),
            "heartbeat": int(self._plant.get("Heartbeat") or 0),
        }

    def machine_ids(self) -> list[str]:
        """Ids of machines with at least one value, in first-seen order."""
        return list(self._machines)

    def status(self, machine_id: str) -> str | None:
        """Current `Status` of a machine, or None when unknown. The dispatcher's machine view reads this."""
        machine = self._machines.get(machine_id)
        return machine.values.get("Status") if machine is not None else None

    def status_since(self, machine_id: str) -> float | None:
        """Sim minute of the machine's last status change, or None when unknown."""
        machine = self._machines.get(machine_id)
        return machine.status_since if machine is not None else None

    def values(self, machine_id: str) -> dict:
        """All latest values of a machine by OPC UA variable name. Treat as read-only."""
        machine = self._machines.get(machine_id)
        return machine.values if machine is not None else {}

    def counters(self, machine_id: str) -> dict:
        """Cumulative counters of a machine with the keys of `oee.CounterSample`, for `OeeTracker.update`."""
        values = self.values(machine_id)
        return {key: values.get(var) or 0 for key, var in _COUNTER_VARS.items()}

    def machines_meta(self) -> list[dict]:
        """`{"id","name","unit"}` per machine, in first-seen order, for `OeeTracker.response`."""
        return [
            {"id": m.id, "name": m.values.get("Name", m.id), "unit": m.values.get("Unit", "")}
            for m in self._machines.values()
        ]

    def to_state_machines(self, trend_samples: int = TREND_SAMPLES) -> list[dict]:
        """The `machines` array of `/api/state`. The runtime fills in `oee`, `next_op` and `waiting_on`, which start as None.

        `trend_samples` caps the trend length of this call. A full 120 samples on all machines take about 6 KB of the 12 KB budget.
        """
        return [self._machine_json(machine, trend_samples) for machine in self._machines.values()]

    def _machine_json(self, machine: _Machine, trend_samples: int) -> dict:
        values = machine.values
        spec = catalog.MACHINES.get(machine.id)
        operation = machine.operation
        return {
            "id": machine.id,
            "name": values.get("Name", machine.id),
            "kind": values.get("Kind", ""),
            "unit": values.get("Unit", ""),
            "capacity": spec.capacity if spec is not None else 1,  # static, and not in the address space
            "status": values.get("Status", "IDLE"),
            "status_since": None if machine.status_since is None else round(machine.status_since, 1),
            "temperature": _number(values.get("Temperature")),
            "setpoint": _number(values.get("TemperatureSetpoint")),
            "power_kw": _number(values.get("PowerKW")),
            "energy_kwh": _number(values.get("EnergyKWh")),
            "total": int(values.get("TotalCount") or 0),
            "good": int(values.get("GoodCount") or 0),
            "scrap": int(values.get("ScrapCount") or 0),
            "setup_class": values.get("SetupClass", ""),
            "op": dict(operation) if operation is not None else None,
            "progress": _number(values.get("Progress"), 3),
            "alarm": {
                "active": bool(values.get("AlarmActive", False)),
                "id": int(values.get("AlarmId") or 0),
                "code": values.get("AlarmCode") or "",
                "text": values.get("AlarmText") or "",
                "severity": int(values.get("AlarmSeverity") or 0),
            },
            "est_repair_min": _number(values.get("EstimatedRepairMin")),
            "fault_count": int(values.get("FaultCount") or 0),
            "oee": None,
            "next_op": None,
            "waiting_on": None,
            "trend": {
                "temperature": list(machine.temperature)[-trend_samples:] if trend_samples > 0 else [],
                "power_kw": list(machine.power)[-trend_samples:] if trend_samples > 0 else [],
            },
        }


__all__ = ["FaultEvent", "Shadow", "TREND_SAMPLES"]
