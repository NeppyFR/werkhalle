"""Data model of the scheduling problem. Normative for the builder, both solvers, the validator and the MES.

All planning times are integer simulated minutes. A schedule is judged only by `validate_schedule` and
`compute_metrics`, which never trust the solver that produced it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import cached_property

from ..domain.orders import Lot


@dataclass(frozen=True)
class Pred:
    op_id: str
    lag: int = 0  # min minutes between predecessor end and the begin of this op's setup (its start if it has none)


@dataclass(frozen=True)
class Frozen:
    """An op already on a machine. Solvers copy exactly this assignment, including the batch id.

    `end` is an estimate pushed through every overlapping downtime, never in the past. Frozen ops are exempt from
    the duration, release, start_before_now, downtime and setup checks. A pending op may never join their load.
    """

    machine: str
    start: int
    end: int
    batch_id: str | None = None


@dataclass(frozen=True)
class Op:
    id: str  # "L-00012:press", "M-6060-00007:cast"
    kind: str  # catalog.CAST/PRESS/AGE/SAW/PACK
    lot_id: str | None  # None for cast ops
    order_id: str | None  # None for cast ops
    durations: dict[str, int]  # eligible machine -> duration in minutes
    setup_class: str
    preds: tuple[Pred, ...] = ()
    release: int = 0  # earliest begin of the op's setup; also carries arrival of in-transit lots and melts
    size: int = 1  # baskets on batch machines, 1 otherwise
    batch_family: str | None = None  # ops of one family may share a load on a batch machine
    frozen: Frozen | None = None
    alloy: str | None = None
    billets_in: int = 0  # taken from the billet store when the op starts (press)
    billets_out: int = 0  # added to the billet store at end + transport (cast)


@dataclass(frozen=True)
class Downtime:
    start: int
    end: int
    reason: str = ""


@dataclass(frozen=True)
class MachineInfo:
    id: str
    kind: str
    capacity: int = 1  # batch capacity; only meaningful when is_batch
    is_batch: bool = False
    available_from: int = 0
    initial_setup_class: str | None = None
    downtimes: tuple[Downtime, ...] = ()


@dataclass(frozen=True)
class OrderInfo:
    id: str
    due: int
    weight: int = 1
    # Latest finish of lots of this order that are already complete and no longer in `ops`.
    completed_lots_end: int | None = None


@dataclass(frozen=True)
class BilletSupply:
    """Billets already cast: free store (`available_at` = now) and melts in transit."""

    alloy: str
    count: int
    available_at: int


@dataclass(frozen=True)
class Weights:
    tardiness: int = 10
    makespan: int = 1
    setup: int = 2


@dataclass(frozen=True)
class Problem:
    now: int
    ops: tuple[Op, ...]
    machines: tuple[MachineInfo, ...]
    orders: tuple[OrderInfo, ...]
    weights: Weights = field(default_factory=Weights)
    billet_supply: tuple[BilletSupply, ...] = ()
    batch_prefix: str = "B"  # solvers mint new load ids as f"{batch_prefix}-{n}", so they cannot collide with frozen ones

    @cached_property
    def _ops_by_id(self) -> dict[str, Op]:
        return {o.id: o for o in self.ops}

    @cached_property
    def _machines_by_id(self) -> dict[str, MachineInfo]:
        return {m.id: m for m in self.machines}

    def op(self, op_id: str) -> Op:
        try:
            return self._ops_by_id[op_id]
        except KeyError:
            raise KeyError(op_id) from None

    def has_op(self, op_id: str) -> bool:
        return op_id in self._ops_by_id

    def machine(self, machine_id: str) -> MachineInfo:
        try:
            return self._machines_by_id[machine_id]
        except KeyError:
            raise KeyError(machine_id) from None


@dataclass(frozen=True)
class Assignment:
    op_id: str
    machine: str
    start: int
    end: int
    setup_before: int = 0  # setup minutes immediately before start, 0 for a later op in the same furnace load
    batch_id: str | None = None  # same id = same furnace load


@dataclass(frozen=True)
class Metrics:
    makespan: int  # latest end minus problem.now
    total_tardiness: int
    weighted_tardiness: int
    late_orders: int
    max_tardiness: int
    setups: int
    setup_minutes: int
    objective: int
    order_completion: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Schedule:
    solver: str  # "cpsat" | "anneal" | "edd" | "keep"
    status: str  # "optimal" | "feasible" | "no_solution"
    assignments: tuple[Assignment, ...]
    solve_seconds: float = 0.0
    best_bound: float | None = None  # CP-SAT only
    iterations: int | None = None  # annealing only
    note: str = ""
    solver_objective: int | None = None  # the objective CP-SAT itself reported, to compare with compute_metrics


@dataclass(frozen=True)
class Violation:
    rule: str  # unassigned, unknown_op, not_eligible, duration, precedence, release, overlap, setup, capacity,
    #            batch_family, batch_sync, downtime, frozen, frozen_batch, start_before_now, billet_balance
    op_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Unit:
    """What one machine does next: one op, or several ops that form one furnace load."""

    op_ids: tuple[str, ...]
    batch_id: str | None = None


@dataclass(frozen=True)
class Plan:
    """What the dispatcher executes. Plan times are advisory, machine sequences are binding."""

    revision: int
    problem: Problem
    schedule: Schedule
    lots: tuple[Lot, ...]
    created_min: float = 0.0
    source: str = "startup"  # startup | manual | proposal | twin | rolling
    source_proposal_id: str | None = None

    @cached_property
    def lots_by_id(self) -> dict[str, Lot]:
        return {lot.id: lot for lot in self.lots}

    @cached_property
    def sequences(self) -> dict[str, tuple[Unit, ...]]:
        """Per machine, the units in planned order. Ops sharing a batch id become one unit."""
        by_machine: dict[str, list[Assignment]] = {}
        for assignment in self.schedule.assignments:
            by_machine.setdefault(assignment.machine, []).append(assignment)
        result: dict[str, tuple[Unit, ...]] = {}
        for machine, assignments in by_machine.items():
            assignments.sort(key=lambda a: (a.start, a.op_id))
            units: list[Unit] = []
            for assignment in assignments:
                if assignment.batch_id and units and units[-1].batch_id == assignment.batch_id:
                    last = units[-1]
                    units[-1] = Unit(last.op_ids + (assignment.op_id,), last.batch_id)
                else:
                    units.append(Unit((assignment.op_id,), assignment.batch_id))
            result[machine] = tuple(units)
        return result
