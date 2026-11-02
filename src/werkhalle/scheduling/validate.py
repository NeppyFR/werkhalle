"""The arbiter. Judges a schedule against its problem from first principles and shares no code with the solvers.

No plan failing this check is ever applied. Rules are listed on `model.Violation`; each has a negative test.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.catalog import TRANSPORT_MIN, setup_minutes
from .model import Assignment, MachineInfo, Op, Problem, Schedule, Violation


@dataclass
class _Unit:
    """What a machine does in one go: one op or one furnace load. A load's pending and frozen parts stay apart."""

    assignments: list[Assignment]
    start: int
    end: int
    frozen: bool
    setup_class: str
    kind: str

    @property
    def op_ids(self) -> tuple[str, ...]:
        return tuple(a.op_id for a in self.assignments)


def validate_schedule(problem: Problem, schedule: Schedule) -> list[Violation]:
    violations: list[Violation] = []

    def report(rule: str, op_ids: tuple[str, ...], message: str) -> None:
        violations.append(Violation(rule, op_ids, message))

    by_op: dict[str, Assignment] = {}
    for assignment in schedule.assignments:
        if not problem.has_op(assignment.op_id):
            report("unknown_op", (assignment.op_id,), f"{assignment.op_id} gehört nicht zum Problem.")
        elif assignment.op_id in by_op:
            report("unknown_op", (assignment.op_id,), f"{assignment.op_id} ist mehrfach zugewiesen.")
        else:
            by_op[assignment.op_id] = assignment
    for op in problem.ops:
        if op.id not in by_op:
            report("unassigned", (op.id,), f"{op.id} ist keiner Maschine zugewiesen.")

    machines = {m.id: m for m in problem.machines}
    load_setup: dict[str, int] = {}
    for assignment in by_op.values():
        if assignment.batch_id is not None:
            load_setup[assignment.batch_id] = load_setup.get(assignment.batch_id, 0) + assignment.setup_before
    for op_id, assignment in by_op.items():
        op = problem.op(op_id)
        # Billets and lots have to be there when the setup begins, for every member of a load: that is when the
        # plant takes them. A running op's setup is in the past.
        setup = assignment.setup_before if assignment.batch_id is None else load_setup[assignment.batch_id]
        begins = assignment.start if op.frozen is not None else assignment.start - setup
        _check_op(problem, op, assignment, begins, by_op, machines, report)
    for machine_id, units in _check_loads(problem, by_op, machines, report).items():
        _check_machine(problem, machines[machine_id], units, report)
    _check_billet_balance(problem, by_op, load_setup, report)
    return violations


def _check_op(problem: Problem, op: Op, a: Assignment, begins: int, by_op, machines, report) -> None:
    frozen = op.frozen
    if a.machine not in machines or a.machine not in op.durations:
        report("not_eligible", (op.id,), f"{op.id} kann nicht auf {a.machine} laufen.")
    elif frozen is None and a.end - a.start != op.durations[a.machine]:
        report(
            "duration",
            (op.id,),
            f"{op.id} dauert {a.end - a.start} min statt {op.durations[a.machine]} min auf {a.machine}.",
        )
    if frozen is not None:
        if (a.machine, a.start, a.end) != (frozen.machine, frozen.start, frozen.end):
            report("frozen", (op.id,), f"{op.id} läuft bereits und darf nicht verändert werden.")
        if a.batch_id != frozen.batch_id:
            report("frozen_batch", (op.id,), f"{op.id} gehört zur laufenden Charge {frozen.batch_id}, nicht {a.batch_id}.")
    else:
        # A release at or before now cannot be missed by anything but what the setup and start rules report anyway.
        if begins < op.release and op.release > problem.now:
            report("release", (op.id,), f"{op.id} beginnt mit Rüsten bei {begins} vor der Freigabe {op.release}.")
        machine = machines.get(a.machine)
        earliest = max(problem.now, machine.available_from if machine else problem.now)
        if a.start < earliest:
            report("start_before_now", (op.id,), f"{op.id} beginnt bei {a.start} vor dem frühesten Zeitpunkt {earliest}.")
    for pred in op.preds:
        before = by_op.get(pred.op_id)
        if before is not None and begins < before.end + pred.lag:
            report(
                "precedence",
                (pred.op_id, op.id),
                f"{op.id} beginnt mit Rüsten bei {begins}, Vorgänger {pred.op_id} endet bei {before.end} "
                f"(Abstand {pred.lag}).",
            )


def _unit_of(problem: Problem, members: list[Assignment], is_frozen: bool) -> _Unit:
    members = sorted(members, key=lambda a: a.op_id)
    head = problem.op(members[0].op_id)
    return _Unit(
        assignments=members,
        start=min(a.start for a in members),
        end=max(a.end for a in members),
        frozen=is_frozen,
        setup_class=head.setup_class,
        kind=head.kind,
    )


def _check_loads(problem: Problem, by_op, machines, report) -> dict[str, list[_Unit]]:
    """Check every load rule and form the units per machine for the sequence checks."""
    frozen_batches = {op.frozen.batch_id for op in problem.ops if op.frozen is not None and op.frozen.batch_id}
    loads: dict[tuple[str, bool], list[Assignment]] = {}
    units: dict[str, list[_Unit]] = {}
    for op_id, a in by_op.items():
        op = problem.op(op_id)
        machine = machines.get(a.machine)
        if machine is None or a.machine not in op.durations:
            continue  # already reported; its times say nothing about a machine it cannot run on
        is_frozen = op.frozen is not None
        if not is_frozen and a.batch_id in frozen_batches:
            report("frozen_batch", (op_id,), f"{op_id} darf der laufenden Charge {a.batch_id} nicht beitreten.")
        if machine.is_batch and a.batch_id is None:
            report("batch_sync", (op_id,), f"{op_id} auf {a.machine} hat keine Chargennummer.")
        if not machine.is_batch and a.batch_id is not None:
            report("batch_sync", (op_id,), f"{op_id} auf {a.machine} kann nicht in einer Charge laufen.")
        if machine.is_batch and a.batch_id is not None:
            loads.setdefault((a.batch_id, is_frozen), []).append(a)
        else:
            units.setdefault(a.machine, []).append(_unit_of(problem, [a], is_frozen))

    for (batch_id, is_frozen), members in loads.items():
        op_ids = tuple(a.op_id for a in members)
        machine_ids = {a.machine for a in members}
        # A frozen op that differs from its Frozen record is the `frozen` rule's to report.
        if not is_frozen and (len(machine_ids) > 1 or len({(a.start, a.end) for a in members}) > 1):
            report("batch_sync", op_ids, f"Charge {batch_id} läuft nicht auf einer Maschine von gleichem Start bis Ende.")
        families = {problem.op(a.op_id).batch_family for a in members}
        if len(members) > 1 and (None in families or len(families) > 1):
            report("batch_family", op_ids, f"Charge {batch_id} mischt nicht kompatible Vorgänge.")
        for machine_id in sorted(machine_ids):
            on_this = [a for a in members if a.machine == machine_id]
            load_size = sum(problem.op(a.op_id).size for a in on_this)
            capacity = machines[machine_id].capacity
            if load_size > capacity:
                report("capacity", op_ids, f"Charge {batch_id} hat {load_size} Körbe, {machine_id} fasst {capacity}.")
            units.setdefault(machine_id, []).append(_unit_of(problem, on_this, is_frozen))
    return units


def _check_machine(problem: Problem, machine: MachineInfo, units: list[_Unit], report) -> None:
    units = sorted(units, key=lambda u: (u.start, u.end, u.op_ids))
    mounted = machine.initial_setup_class
    free_from = max(problem.now, machine.available_from)
    furthest: _Unit | None = None  # the unit reaching latest so far, which need not be the one just before
    for unit in units:
        if furthest is not None and unit.start < furthest.end and not (unit.frozen and furthest.frozen):
            report("overlap", (*furthest.op_ids, *unit.op_ids), f"{machine.id}: Überlappung bei {unit.start}.")
        if not unit.frozen:
            setup = sum(a.setup_before for a in unit.assignments)
            required = setup_minutes(unit.kind, mounted, unit.setup_class)
            if setup != required:
                report("setup", unit.op_ids, f"{unit.op_ids[0]}: Rüstzeit {setup} min statt {required} min.")
            elif free_from <= unit.start < free_from + required:
                report("setup", unit.op_ids, f"{unit.op_ids[0]}: keine {required} min Zeit zum Rüsten vor {unit.start}.")
            for downtime in machine.downtimes:
                if unit.start - setup < downtime.end and unit.end > downtime.start:
                    report(
                        "downtime",
                        unit.op_ids,
                        f"{unit.op_ids[0]} auf {machine.id} kollidiert mit dem Stillstand "
                        f"{downtime.start} bis {downtime.end} (Rüsten eingeschlossen).",
                    )
        mounted = unit.setup_class
        free_from = max(free_from, unit.end)
        if furthest is None or unit.end > furthest.end:
            furthest = unit


def _check_billet_balance(problem: Problem, by_op, load_setup, report) -> None:
    """Replay every alloy's store in time order: supply at cast end + transport, demand when the press begins.

    With its setup, that is, because StartOperation takes the billets before the press starts to set up.
    """
    timelines: dict[str, list[tuple[int, int, int, str]]] = {}
    for supply in problem.billet_supply:
        timelines.setdefault(supply.alloy, []).append((supply.available_at, 0, supply.count, ""))
    for op in problem.ops:
        a = by_op.get(op.id)
        if a is None or op.alloy is None:
            continue
        if op.billets_out:
            timelines.setdefault(op.alloy, []).append((a.end + TRANSPORT_MIN, 0, op.billets_out, op.id))
        # A running press took its billets when it started, and the store is net of them.
        if op.billets_in and op.frozen is None:
            setup = a.setup_before if a.batch_id is None else load_setup[a.batch_id]
            timelines.setdefault(op.alloy, []).append((a.start - setup, 1, -op.billets_in, op.id))
    for alloy, timeline in timelines.items():
        stock = 0
        for time, _, change, op_id in sorted(timeline):
            stock += change
            if stock < 0:
                report("billet_balance", (op_id,), f"{op_id}: bei {time} fehlen {-stock} Bolzen der Legierung {alloy}.")
                stock = 0  # one report per shortage, not one for everything behind it
