"""What is known about the plant's progress: one consistent state, kept current by trace events.

The live MES and the twin both read the plant only through this. Status and alarms are not in here on purpose: those
come from `machines`, because the trace stream deliberately carries no faults.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain import catalog
from ..domain.catalog import AGE, CAST, MAX_BILLETS_PER_LOT, PACK, PRESS, SAW, TRANSPORT_MIN
from ..domain.orders import Lot
from ..domain.state import MeltInTransit, PlanningState

PENDING = "pending"
RUNNING = "running"
DONE = "done"

_CAST_SUFFIX = f":{CAST}"
_PACK_SUFFIX = f":{PACK}"
SHIP = "ship"
_NEXT_STAGE = {PRESS: AGE, AGE: SAW, SAW: PACK, PACK: SHIP}


@dataclass(frozen=True)
class RunningOp:
    """An op or furnace load on a machine, also while a fault pauses it."""

    machine: str
    op_ids: tuple[str, ...]
    kind: str | None
    lot_ids: tuple[str, ...]
    batch_id: str | None
    started_min: float | None
    setup_min: float = 0.0
    est_end_min: float | None = None  # only known when the op comes from a state, which says how much work is left


class Tracker:
    """Build with `Tracker.from_state`, feed with `apply`. Nothing in here ever suspends or touches I/O."""

    def __init__(self) -> None:
        self.last_seq = 0
        self.t = 0.0
        self.resync_needed = False
        self._started: set[str] = set()
        self._finished: set[str] = set()
        self._running: dict[str, RunningOp] = {}
        self._setup_class: dict[str, str | None] = {}
        self._lot_stage: dict[str, str] = {}
        self._lots_moving: dict[str, tuple[str, float]] = {}
        self._melts_arrived: set[str] = {catalog.INITIAL_MELT_PLAN_ID}
        self._melts_moving: dict[str, MeltInTransit] = {}
        self._billets: dict[str, int] = {alloy: 0 for alloy in catalog.ALLOYS}
        # Presses started after the state: the store figure above is not yet net of them. Done ones count by what they used.
        self._press_alloy: dict[str, str] = {}
        self._extruded: dict[str, int] = {}

    @classmethod
    def from_state(cls, state: PlanningState) -> "Tracker":
        self = cls()
        self.last_seq = state.seq
        self.t = state.now
        self._started = set(state.started_op_ids) | set(state.finished_op_ids)
        self._finished = set(state.finished_op_ids)
        for machine in state.machines:
            self._setup_class[machine.id] = machine.setup_class
            op_ids = tuple(op_id for op_id in machine.op_ids if op_id not in self._finished)
            if op_ids:
                self._running[machine.id] = RunningOp(
                    machine.id, op_ids, machine.kind, machine.lot_ids, machine.batch_id, machine.started_min,
                    est_end_min=state.now + machine.remaining_work_min,
                )
        for melt in state.melts_in_transit:
            self._melts_moving[melt.plan_id] = melt
        for op_id in self._finished:
            if op_id.endswith(_CAST_SUFFIX):
                plan_id = op_id[: -len(_CAST_SUFFIX)]
                if plan_id not in self._melts_moving:
                    self._melts_arrived.add(plan_id)
            elif op_id.endswith(_PACK_SUFFIX):
                self._lot_stage[op_id[: -len(_PACK_SUFFIX)]] = SHIP
        for lot in state.lots_in_transit:
            self._lots_moving[lot.lot_id] = (lot.stage, lot.arrives_min)
            self._lot_stage.pop(lot.lot_id, None)
        self._lot_stage.update(state.lots_buffered)
        self._billets.update(state.billet_store)
        return self

    # ------------------------------------------------------------------ events

    def apply(self, event: Mapping) -> None:
        """Apply one trace event. Repeats are ignored. A gap sets `resync_needed`: the tracker can no longer be trusted."""
        seq = event["seq"]
        if seq <= self.last_seq:
            return
        if seq != self.last_seq + 1:
            self.resync_needed = True
        self.last_seq = seq
        if event["t"] > self.t:
            self.t = event["t"]
        kind = event["type"]
        if kind == "op_started":
            self._on_started(event)
        elif kind == "op_finished":
            self._on_finished(event)
        elif kind == "billet_extruded":
            lot_id = event["lot_id"]
            if lot_id in self._press_alloy:
                self._extruded[lot_id] = self._extruded.get(lot_id, 0) + 1
        elif kind == "lot_arrived":
            self._lots_moving.pop(event["lot_id"], None)
            self._lot_stage[event["lot_id"]] = event["stage"]
        elif kind == "melt_cast":
            self._melts_moving[event["plan_id"]] = MeltInTransit(
                event["melt_id"], event["plan_id"], event["alloy"], len(event["billets"]), event["t"] + TRANSPORT_MIN
            )
        elif kind == "melt_arrived":
            self._melts_moving.pop(event["plan_id"], None)
            self._melts_arrived.add(event["plan_id"])
            self._billets[event["alloy"]] = self._billets.get(event["alloy"], 0) + event["n_billets"]

    def _on_started(self, event: Mapping) -> None:
        machine = event["machine"]
        op_ids = tuple(event["op_ids"])
        lot_ids = tuple(event["lot_ids"])
        kind = event["kind"]
        self._started.update(op_ids)
        self._running[machine] = RunningOp(
            machine, op_ids, kind, lot_ids, event.get("batch_id"), event["t"], setup_min=event.get("setup_min", 0.0)
        )
        self._setup_class[machine] = event.get("setup_class", self._setup_class.get(machine))
        if kind == PRESS:
            alloy = str(event.get("setup_class", "")).split("|")[0]
            for lot_id in lot_ids:
                self._press_alloy[lot_id] = alloy
                self._extruded[lot_id] = 0
        else:
            for lot_id in lot_ids:
                self._lot_stage.pop(lot_id, None)

    def _on_finished(self, event: Mapping) -> None:
        machine = event["machine"]
        op_ids = tuple(event["op_ids"])
        self._started.update(op_ids)
        self._finished.update(op_ids)
        self._running.pop(machine, None)
        kind = event["kind"]
        for lot_id in event["lot_ids"]:
            if kind == PRESS and lot_id in self._press_alloy:
                alloy = self._press_alloy.pop(lot_id)
                self._billets[alloy] = self._billets.get(alloy, 0) - self._extruded.pop(lot_id, 0)
            if kind in _NEXT_STAGE:
                self._lots_moving[lot_id] = (_NEXT_STAGE[kind], event["t"] + TRANSPORT_MIN)

    # ------------------------------------------------------------------ queries

    def op_state(self, op_id: str) -> str:
        if op_id in self._finished:
            return DONE
        return RUNNING if op_id in self._started else PENDING

    def lot_at_stage(self, lot_id: str, stage: str) -> bool:
        """Whether the lot sits in that stage's buffer right now. `ship` stays true, as nothing leaves shipping."""
        return self._lot_stage.get(lot_id) == stage

    def lot_arrival(self, lot_id: str) -> tuple[str, float] | None:
        """(stage, arrival) of a lot on a transport, else None."""
        return self._lots_moving.get(lot_id)

    def melt_arrived(self, plan_id: str) -> bool:
        return plan_id in self._melts_arrived

    def melts_in_transit(self) -> tuple[MeltInTransit, ...]:
        return tuple(self._melts_moving.values())

    def transports_pending(self) -> bool:
        return bool(self._melts_moving or self._lots_moving)

    def running_on(self, machine_id: str) -> RunningOp | None:
        return self._running.get(machine_id)

    def running(self) -> tuple[RunningOp, ...]:
        return tuple(self._running.values())

    def setup_class(self, machine_id: str) -> str | None:
        return self._setup_class.get(machine_id)

    def free_billets(self, alloy: str, lots: Mapping[str, Lot] = {}) -> int:
        """Billets of an alloy in the store, net of running presses.

        `op_started` does not say how many billets a press took, so running presses started since the state are looked up
        in `lots`. Unknown ones count as a full lot: better a press waiting a little than a command the plant refuses.
        """
        free = self._billets.get(alloy, 0)
        for lot_id, press_alloy in self._press_alloy.items():
            if press_alloy == alloy:
                lot = lots.get(lot_id)
                free -= lot.n_billets if lot is not None else MAX_BILLETS_PER_LOT
        return max(free, 0)

    def __repr__(self) -> str:
        return f"Tracker(last_seq={self.last_seq}, t={self.t}, started={len(self._started)}, done={len(self._finished)})"


__all__ = ["DONE", "PENDING", "RUNNING", "RunningOp", "SHIP", "Tracker"]
