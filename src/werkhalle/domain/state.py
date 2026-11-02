"""Neutral view of the plant at one instant, for planning and for seeding a twin.

`simcore` produces it from an atomic state snapshot; `scheduling.builder` and `execution.Tracker` consume it.
It lives in `domain` so that those packages do not need to know each other.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Remaining repair is estimate minus elapsed, never below this floor, so an overrun repair still blocks the machine.
FAULT_REMAINING_FLOOR_MIN = 15.0


@dataclass(frozen=True)
class MachineState:
    id: str
    status: str  # IDLE | SETUP | RUN | FAULT
    setup_class: str | None  # class currently mounted; while setting up, the class being set up to
    op_ids: tuple[str, ...] = ()  # the op on the machine, also while paused by a fault
    kind: str | None = None
    lot_ids: tuple[str, ...] = ()
    batch_id: str | None = None
    started_min: float | None = None
    remaining_work_min: float = 0.0  # setup + run work left at planning rate, excluding any fault pause
    fault_code: str = ""
    fault_estimate_min: float = 0.0  # the code's mean, or the injected repairMin
    fault_elapsed_min: float = 0.0
    fault_remaining_min: float = 0.0  # max(estimate - elapsed, FAULT_REMAINING_FLOOR_MIN) while in FAULT


@dataclass(frozen=True)
class MeltInTransit:
    melt_id: str  # S-00012
    plan_id: str  # M-6060-00007
    alloy: str
    n_billets: int
    arrives_min: float


@dataclass(frozen=True)
class LotInTransit:
    lot_id: str
    stage: str  # age | saw | pack | ship
    arrives_min: float


@dataclass(frozen=True)
class PlanningState:
    run_id: str
    seq: int  # last trace event included
    now: float
    machines: tuple[MachineState, ...]
    billet_store: dict[str, int]  # free billets per alloy, net of lots already on a press
    melts_in_transit: tuple[MeltInTransit, ...] = ()
    lots_in_transit: tuple[LotInTransit, ...] = ()
    lots_buffered: dict[str, str] = field(default_factory=dict)  # lot id -> stage whose buffer it sits in
    started_op_ids: frozenset[str] = frozenset()  # running or finished
    finished_op_ids: frozenset[str] = frozenset()

    def machine(self, machine_id: str) -> MachineState:
        for machine in self.machines:
            if machine.id == machine_id:
                return machine
        raise KeyError(machine_id)
