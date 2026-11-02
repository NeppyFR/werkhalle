"""Which op each machine starts next: strict head of line over the plan's machine sequences.

`next_commands` is pure. It relies only on what the plan says and on what the tracker and the machine states have actually
seen, so it holds up when those lag behind the plant. `Dispatcher` adds what a live MES needs around it: the set of commanded
ops, written before the OPC UA call and cleared only on a definite refusal, and a back-off.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from ..domain.catalog import AGE, BILLETS_PER_MELT, CAST, PACK, PRESS, SAW
from ..scheduling.model import Op, Plan, Unit
from .tracker import DONE, PENDING, RUNNING, Tracker

IDLE = "IDLE"
FAULT = "FAULT"

# `waiting_on` of docs/API.md.
WAITING_MATERIAL = "Material"
WAITING_PRED = "Vorgänger"
WAITING_FAULT = "Störung"
WAITING_NO_WORK = "Kein Auftrag"

_STAGE_OF = {AGE: "age", SAW: "saw", PACK: "pack"}
_CAST_SUFFIX = f":{CAST}"

# Answers that say the command did nothing. ALREADY_STARTED is not one: the op is on a machine and the events will say so.
_DEFINITE_REFUSALS = frozenset(
    {"MACHINE_BUSY", "MACHINE_FAULT", "NO_MATERIAL", "NOT_ELIGIBLE", "BAD_COMMAND", "STALE_RUN"}
)


def machine_status(view: Mapping) -> str | None:
    """Status from a machine view: the OPC UA name of `engine.machine_view` and the shadow, or the API name."""
    status = view.get("Status")
    return status if status is not None else view.get("status")


def plan_finished(plan: Plan, tracker: Tracker) -> bool:
    return all(tracker.op_state(op.id) == DONE for op in plan.problem.ops)


def _is_pending(tracker: Tracker, op: Op) -> bool:
    # A frozen op is on a machine whatever a tracker that lags behind the plan's snapshot says, so it is never commanded.
    return op.frozen is None and tracker.op_state(op.id) == PENDING


def _head_unit(plan: Plan, tracker: Tracker, machine_id: str, commanded: Collection[str]) -> Unit | None:
    """The first unit with anything left to start, reduced to those members. A unit fully on its way is skipped."""
    problem = plan.problem
    for unit in plan.sequences.get(machine_id, ()):
        pending = tuple(op_id for op_id in unit.op_ids if _is_pending(tracker, problem.op(op_id)))
        members = tuple(op_id for op_id in pending if op_id not in commanded)
        if members:
            return Unit(members, unit.batch_id)
    return None


def _billets_reserved(plan: Plan, tracker: Tracker, commanded: Collection[str]) -> dict[str, int]:
    """Billets of commanded presses the tracker has not seen starting yet: the plant has taken them, or is about to."""
    reserved: dict[str, int] = {}
    problem = plan.problem
    for op_id in commanded:
        if problem.has_op(op_id):
            op = problem.op(op_id)
            if op.billets_in and _is_pending(tracker, op):
                reserved[op.alloy] = reserved.get(op.alloy, 0) + op.billets_in
    return reserved


def _waiting_on(plan: Plan, tracker: Tracker, op_ids: tuple[str, ...], reserved: Mapping[str, int]) -> str | None:
    """None if the unit can start: all predecessors done and the material on site. Else what it waits for."""
    problem = plan.problem
    ops = [problem.op(op_id) for op_id in op_ids]
    for op in ops:
        for pred in op.preds:
            if tracker.op_state(pred.op_id) != DONE:
                return WAITING_PRED
    for op in ops:
        kind = op.kind
        if kind in _STAGE_OF:
            if not tracker.lot_at_stage(op.lot_id, _STAGE_OF[kind]):
                return WAITING_MATERIAL
        elif kind == PRESS:
            for pred in op.preds:
                if pred.op_id.endswith(_CAST_SUFFIX) and not tracker.melt_arrived(pred.op_id[: -len(_CAST_SUFFIX)]):
                    return WAITING_MATERIAL
            # A melt that was already on its way when the plan was built shows up as a release, not as a predecessor.
            for melt in tracker.melts_in_transit():
                if melt.alloy == op.alloy and math.ceil(melt.arrives_min - 1e-9) <= op.release:
                    return WAITING_MATERIAL
            if tracker.free_billets(op.alloy, plan.lots_by_id) - reserved.get(op.alloy, 0) < op.billets_in:
                return WAITING_MATERIAL
    return None


def _command(plan: Plan, machine: str, unit: Unit, run_id: str) -> dict | None:
    """The start command of PLAN section 4, plus the run id and the machine it goes to."""
    problem = plan.problem
    first = problem.op(unit.op_ids[0])
    command: dict = {"run_id": run_id, "machine": machine, "op_ids": list(unit.op_ids), "kind": first.kind}
    if first.kind == CAST:
        command.update(melt_plan_id=unit.op_ids[0][: -len(_CAST_SUFFIX)], alloy=first.alloy)
    elif first.kind == PRESS:
        lot = plan.lots_by_id.get(first.lot_id)
        if lot is None:
            return None
        command.update(lot_id=lot.id, order_id=lot.order_id, product_id=lot.product_id, n_billets=lot.n_billets)
    elif first.kind == AGE:
        command.update(
            batch_id=unit.batch_id, recipe=first.setup_class, lot_ids=[problem.op(op_id).lot_id for op_id in unit.op_ids]
        )
    else:
        command.update(lot_id=first.lot_id)
    return command


def _machines_in_flight(plan: Plan, tracker: Tracker, commanded: Collection[str]) -> set[str]:
    """Machines with a command out that the tracker has not seen starting yet."""
    if not commanded:
        return set()
    busy: set[str] = set()
    problem = plan.problem
    for machine, units in plan.sequences.items():
        for unit in units:
            if any(op_id in commanded and _is_pending(tracker, problem.op(op_id)) for op_id in unit.op_ids):
                busy.add(machine)
                break
    return busy


def next_commands(
    plan: Plan,
    tracker: Tracker,
    machines: Mapping[str, Mapping],
    commanded: Collection[str] = frozenset(),
    run_id: str = "",
) -> list[dict]:
    """One command per machine that is free: Status IDLE, nothing in flight, nothing the tracker has seen on it.

    The sequence is binding, so a head that cannot start blocks its machine. Pure: repeating a call repeats its answer.
    """
    in_flight = _machines_in_flight(plan, tracker, commanded)
    reserved = _billets_reserved(plan, tracker, commanded)
    commands: list[dict] = []
    for machine_id in plan.sequences:
        view = machines.get(machine_id)
        if view is None or machine_status(view) != IDLE or machine_id in in_flight:
            continue
        # The state view can lag the trace: a machine still showing IDLE may hold an op the events have reported.
        if tracker.running_on(machine_id) is not None:
            continue
        unit = _head_unit(plan, tracker, machine_id, commanded)
        if unit is None or _waiting_on(plan, tracker, unit.op_ids, reserved) is not None:
            continue
        command = _command(plan, machine_id, unit, run_id)
        if command is None:
            continue
        commands.append(command)
        first = plan.problem.op(unit.op_ids[0])
        if first.billets_in:
            reserved[first.alloy] = reserved.get(first.alloy, 0) + first.billets_in
    return commands


@dataclass(frozen=True)
class Head:
    """What a machine does next, for `next_op` and `waiting_on` in the state and for the starvation figure in OEE."""

    machine: str
    next_op: str | None
    waiting_on: str | None


def machine_heads(
    plan: Plan | None, tracker: Tracker, machines: Mapping[str, Mapping], commanded: Collection[str] = frozenset()
) -> dict[str, Head]:
    """Per machine, the next op and what it waits for. A machine at work waits for nothing, one in FAULT for its repair."""
    heads: dict[str, Head] = {}
    reserved = _billets_reserved(plan, tracker, commanded) if plan is not None else {}
    for machine_id, view in machines.items():
        unit = _head_unit(plan, tracker, machine_id, commanded) if plan is not None else None
        next_op = unit.op_ids[0] if unit else None
        status = machine_status(view)
        if status == FAULT:
            waiting = WAITING_FAULT
        elif status != IDLE:
            waiting = None
        elif unit is None:
            waiting = WAITING_NO_WORK
        else:
            waiting = _waiting_on(plan, tracker, unit.op_ids, reserved)
        heads[machine_id] = Head(machine_id, next_op, waiting)
    return heads


def stalled(plan: Plan, tracker: Tracker, machines: Mapping[str, Mapping], transports_active: bool) -> bool:
    """No op running, no transport, no FAULT, nothing that could start now, and the plan is unfinished.

    "Nothing that could start" is what keeps this from being true for the moment between an arrival and its dispatch.
    """
    if transports_active or tracker.transports_pending() or tracker.running():
        return False
    if any(machine_status(view) != IDLE for view in machines.values()):
        return False
    if plan_finished(plan, tracker):
        return False
    return not next_commands(plan, tracker, machines)


def billet_shortfall(plan: Plan, tracker: Tracker) -> dict[str, int]:
    """Billets missing per alloy if every pending press of the plan runs on what is here, on its way or planned to be cast."""
    problem = plan.problem
    demand: dict[str, int] = {}
    supply: dict[str, int] = {}
    for op in problem.ops:
        if op.billets_in and _is_pending(tracker, op):
            demand[op.alloy] = demand.get(op.alloy, 0) + op.billets_in
        elif op.billets_out and tracker.op_state(op.id) in (PENDING, RUNNING):
            supply[op.alloy] = supply.get(op.alloy, 0) + op.billets_out
    for melt in tracker.melts_in_transit():
        supply[melt.alloy] = supply.get(melt.alloy, 0) + melt.n_billets
    for running in tracker.running():
        # A cast of an older plan that this plan no longer lists still delivers.
        if running.kind == CAST and not problem.has_op(running.op_ids[0]):
            alloy = tracker.setup_class(running.machine)
            supply[alloy] = supply.get(alloy, 0) + BILLETS_PER_MELT
    missing: dict[str, int] = {}
    for alloy, needed in demand.items():
        short = needed - supply.get(alloy, 0) - tracker.free_billets(alloy, plan.lots_by_id)
        if short > 0:
            missing[alloy] = short
    return missing


class Dispatcher:
    """The live MES's side of `next_commands`. Times are whatever clock the caller passes, in the unit of `backoff`.

    Call `poll` for the commands to send: their op ids are recorded as commanded on the spot, before any OPC UA call. Report
    each answer with `on_result`. A lost answer is not a refusal, so it leaves the op commanded, and the plant's own duplicate
    protection covers a later resend after `forget_unconfirmed`, which a reconnect should call.
    """

    def __init__(self, run_id: str = "", backoff: float = 2.0) -> None:
        self.run_id = run_id
        self.backoff = backoff
        self.commanded: set[str] = set()
        self.last_refusal: dict | None = None
        self.replan_needed = False  # a NO_MATERIAL refusal with no planned supply for the alloy
        self._retry_at: dict[str, float] = {}

    def poll(self, plan: Plan, tracker: Tracker, machines: Mapping[str, Mapping], now: float) -> list[dict]:
        self.commanded = {op_id for op_id in self.commanded if tracker.op_state(op_id) == PENDING}
        ready = {
            machine_id: view for machine_id, view in machines.items() if now >= self._retry_at.get(machine_id, now)
        }
        commands = next_commands(plan, tracker, ready, self.commanded, self.run_id)
        for command in commands:
            self.commanded.update(command["op_ids"])
        return commands

    def on_result(self, plan: Plan, tracker: Tracker, command: Mapping, result: Mapping, now: float) -> None:
        """Clear the op ids only on a definite refusal, and leave that machine alone for a moment."""
        if result.get("ok"):
            return
        reason = result.get("reason", "")
        if reason not in _DEFINITE_REFUSALS:
            return
        self.commanded.difference_update(command["op_ids"])
        self._retry_at[command["machine"]] = now + self.backoff
        self.last_refusal = {"machine": command["machine"], "op_ids": list(command["op_ids"]), "reason": reason, "at": now}
        if reason == "NO_MATERIAL" and billet_shortfall(plan, tracker):
            self.replan_needed = True

    def forget_unconfirmed(self) -> None:
        """After a reconnect or a new plan: what is still commanded may have been lost, and a repeat is refused harmlessly."""
        self.commanded.clear()
        self._retry_at.clear()
        self.replan_needed = False


__all__ = [
    "Dispatcher",
    "Head",
    "WAITING_FAULT",
    "WAITING_MATERIAL",
    "WAITING_NO_WORK",
    "WAITING_PRED",
    "billet_shortfall",
    "machine_heads",
    "machine_status",
    "next_commands",
    "plan_finished",
    "stalled",
]
